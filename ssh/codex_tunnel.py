"""SSH tunnel helper for reaching the codex app-server through a bastion.

When the codex WebSocket port (default 5432) isn't reachable from the
public internet, we open a local port-forward over a paramiko SSH
channel and point the WebSocket client at ``ws://127.0.0.1:<local>``.

The tunnel is implemented as a background thread that accepts local TCP
connections and pipes them through a paramiko channel to the remote
host:port. The thread model mirrors paramiko's own demo files.

Usage:

    from codex_tunnel import SSHTunnel

    with SSHTunnel("124.221.28.203", "root", "1qaZxsw@", 5432) as t:
        # t.local_url is e.g. ws://127.0.0.1:54321
        client = CodexClient(CodexConfig(ws_url=t.local_url))
        ...
"""

from __future__ import annotations

import logging
import select
import socket
import socketserver
import threading
from dataclasses import dataclass
from typing import Optional

import paramiko

logger = logging.getLogger("codex.tunnel")


@dataclass
class SSHTunnel:
    """Local TCP port → remote host:port forwarded over SSH."""

    ssh_host: str
    ssh_user: str
    ssh_password: Optional[str]
    remote_port: int
    ssh_port: int = 22
    local_host: str = "127.0.0.1"
    local_port: int = 0  # 0 = pick a free port
    ssh_key_path: Optional[str] = None

    _ssh: Optional[paramiko.SSHClient] = None
    _transport: Optional[paramiko.Transport] = None
    _server: Optional[socketserver.TCPServer] = None
    _thread: Optional[threading.Thread] = None
    _allocated_port: int = 0

    def __enter__(self) -> "SSHTunnel":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()

    @property
    def local_url(self) -> str:
        """``ws://127.0.0.1:<allocated>`` for the WebSocket client."""
        return f"ws://{self.local_host}:{self._allocated_port}"

    def start(self) -> None:
        if self._server is not None:
            return
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = {
            "hostname": self.ssh_host,
            "port": self.ssh_port,
            "username": self.ssh_user,
            "timeout": 30.0,
            "banner_timeout": 15.0,
            "auth_timeout": 15.0,
        }
        if self.ssh_key_path:
            connect_kwargs["key_filename"] = self.ssh_key_path
            connect_kwargs["look_for_keys"] = False
            connect_kwargs["allow_agent"] = False
        elif self.ssh_password:
            connect_kwargs["password"] = self.ssh_password
            connect_kwargs["allow_agent"] = False
            connect_kwargs["look_for_keys"] = False
        else:
            # No password or key specified — try default keys
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True
        ssh.connect(**connect_kwargs)
        transport = ssh.get_transport()
        if transport is None:
            ssh.close()
            raise RuntimeError("SSH transport not available after connect")
        transport.set_keepalive(30)
        self._ssh = ssh
        self._transport = transport

        # Bind to a free local port when not specified.
        bind_port = self.local_port
        server = _TunnelServer(
            (self.local_host, bind_port),
            transport,
            (self.ssh_host, self.remote_port),
        )
        server.daemon_threads = True
        # Re-read allocated port in case we bound to 0.
        self._allocated_port = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name=f"codex-tunnel-{self._allocated_port}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "codex ssh tunnel ready: %s -> %s:%d via %s@%s",
            self.local_url,
            self.ssh_host,
            self.remote_port,
            self.ssh_user,
            self.ssh_host,
        )

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # pragma: no cover
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:  # pragma: no cover
                pass
            self._ssh = None
            self._transport = None
        logger.info("codex ssh tunnel stopped")


class _TunnelHandler(socketserver.BaseRequestHandler):
    """Pipe one local TCP connection through a paramiko channel."""

    def handle(self) -> None:
        server: _TunnelServer = self.server  # type: ignore[assignment]
        transport = server._transport
        dest = server._dest
        try:
            chan = transport.open_channel(
                "direct-tcpip",
                dest,
                self.client_address,
            )
        except Exception as e:  # pragma: no cover
            logger.warning("tunnel channel open failed: %s", e)
            return
        try:
            _pipe(self.request, chan)
        finally:
            try:
                chan.close()
            except Exception:  # pragma: no cover
                pass


class _TunnelServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        transport: paramiko.Transport,
        dest: tuple[str, int],
    ) -> None:
        self._transport = transport
        self._dest = dest
        super().__init__(server_address, _TunnelHandler)


def _pipe(local: socket.socket, chan: paramiko.Channel) -> None:
    """Bidirectional copy between a local socket and a paramiko channel."""
    local.setblocking(False)
    chan.setblocking(False)
    pending_out = b""  # local → chan
    pending_in = b""  # chan → local
    while True:
        rlist = []
        if not pending_out:
            rlist.append(local)
        if not pending_in:
            rlist.append(chan)
        try:
            r, _, _ = select.select(rlist, [], [], 1.0)
        except (OSError, ValueError):  # pragma: no cover
            break
        if local in r:
            try:
                data = local.recv(8192)
            except BlockingIOError:
                data = b""
            if not data:
                # Local closed → flush remaining and exit.
                _flush(chan, pending_out)
                _flush_local(local, pending_in)
                return
            pending_out += data
        if chan in r:
            try:
                data = chan.recv(8192)
            except BlockingIOError:
                data = b""
            if not data:
                _flush_local(local, pending_in)
                return
            pending_in += data
        if pending_out:
            sent = chan.send(pending_out)
            if sent > 0:
                pending_out = pending_out[sent:]
        if pending_in:
            try:
                sent = local.send(pending_in)
                if sent > 0:
                    pending_in = pending_in[sent:]
            except BlockingIOError:
                pass


def _flush(chan: paramiko.Channel, data: bytes) -> None:
    while data:
        sent = chan.send(data)
        if sent <= 0:
            break
        data = data[sent:]


def _flush_local(local: socket.socket, data: bytes) -> None:
    while data:
        try:
            sent = local.send(data)
        except BlockingIOError:
            continue
        if sent <= 0:
            break
        data = data[sent:]
