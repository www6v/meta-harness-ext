"""
SSH 客户端封装，基于 paramiko 提供简化的远程命令执行。

使用示例:
    from ssh import SSHClient

    client = SSHClient("124.221.28.203", "root", "1qaZxsw@")
    result = client.run("docker ps")
    print(result.stdout)

    # 或使用上下文管理器
    with SSHClient("124.221.28.203", "root", "1qaZxsw@") as c:
        print(c.run("hostname").stdout)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import paramiko

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SSHResult:
    """一条远程命令的执行结果。"""

    stdout: str
    """命令标准输出（已解码为字符串）。"""
    stderr: str
    """命令标准错误输出（已解码为字符串）。"""
    exit_code: int
    """命令退出码；获取失败时为 -1。"""

    @property
    def ok(self) -> bool:
        """退出码为 0 且 stderr 为空时返回 True。"""
        return self.exit_code == 0 and not self.stderr.strip()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SSHClient:
    """paramiko SSH 客户端封装。

    连接参数以实例属性方式暴露，方便子类或外部读取::

        >>> c = SSHClient("1.2.3.4")
        >>> c.connect()
        >>> print(c.run("whoami"))
    """

    def __init__(
        self,
        host: str,
        username: str = "root",
        password: str | None = None,
        port: int = 22,
        timeout: float = 15.0,
        *,
        connect: bool = False,
    ) -> None:
        """创建 SSH 客户端实例。

        Args:
            host: 目标主机名或 IP。
            username: SSH 登录用户名。
            password: SSH 登录密码；为 None 时不使用密码认证。
            port: SSH 端口。
            timeout: 连接及命令执行的超时秒数。
            connect: 若为 True，构造后立即调用 :meth:`connect`。
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

        self._ssh: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

        if connect:
            self.connect()

    # ---- context manager ---------------------------------------------------

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ---- connection --------------------------------------------------------

    @property
    def ssh(self) -> paramiko.SSHClient:
        """已连接的 paramiko SSH 客户端；未连接时抛出 RuntimeError。"""
        if self._ssh is None:
            raise RuntimeError("SSH 未连接，请先调用 connect()")
        return self._ssh

    @property
    def sftp(self) -> paramiko.SFTPClient:
        """已连接的 paramiko SFTP 客户端；未连接时抛出 RuntimeError。"""
        if self._sftp is None:
            self._sftp = self.ssh.open_sftp()
        return self._sftp

    @property
    def connected(self) -> bool:
        """当前是否处于已连接状态。"""
        return self._ssh is not None and self._ssh.get_transport() is not None

    def connect(self) -> None:
        """建立 SSH 连接（幂等——已连接时无操作）。"""
        if self.connected:
            return

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict[str, Any] = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            "timeout": self.timeout,
            "banner_timeout": self.timeout,
        }
        if self.password is not None:
            connect_kwargs["password"] = self.password

        ssh.connect(**connect_kwargs)
        self._ssh = ssh

    def close(self) -> None:
        """关闭 SSH 及 SFTP 连接（幂等）。"""
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

    # ---- commands ----------------------------------------------------------

    def run(
        self,
        command: str,
        *,
        timeout: float | None = None,
        encoding: str = "utf-8",
        retry: int = 0,
        retry_delay: float = 2.0,
        retry_on: Callable[[SSHResult], bool] | None = None,
    ) -> SSHResult:
        """在远程主机执行一条命令并返回结果。

        Args:
            command: 要执行的 shell 命令。
            timeout: 命令超时秒数；默认为实例的 ``timeout``。
            encoding: stdout/stderr 解码方式。
            retry: 失败后重试次数。
            retry_delay: 重试间隔（秒）。
            retry_on: 判断是否需要重试的回调；接受 ``SSHResult`` 返回 ``bool``。
                      默认为 ``lambda r: not r.ok``。
        """
        if timeout is None:
            timeout = self.timeout
        if retry_on is None:
            retry_on = lambda r: not r.ok

        last: SSHResult | None = None
        for attempt in range(retry + 1):
            if attempt > 0 and last is not None and not retry_on(last):
                return last
            if attempt > 0:
                time.sleep(retry_delay)
            last = self._run_once(command, timeout=timeout, encoding=encoding)
            if last.ok or not retry_on(last):
                return last
        return last  # type: ignore[return-value]

    def _run_once(self, command: str, *, timeout: float, encoding: str) -> SSHResult:
        start = time.monotonic()
        chan = self.ssh.get_transport()
        if chan is None:
            raise RuntimeError("SSH transport 已断开")

        _stdin, stdout, stderr = self.ssh.exec_command(command, timeout=timeout)
        # 必须读取完再取 exit_code，否则可能阻塞。
        out = stdout.read()
        err = stderr.read()
        exit_code = stdout.channel.recv_exit_status() if stdout.channel else -1

        elapsed = time.monotonic() - start
        _ = elapsed  # 可用于 debug logging

        return SSHResult(
            stdout=out.decode(encoding, errors="replace"),
            stderr=err.decode(encoding, errors="replace"),
            exit_code=exit_code,
        )

    def run_quiet(self, command: str, **kwargs: Any) -> str:
        """执行命令并仅返回 stdout 文本（静默模式）。"""
        return self.run(command, **kwargs).stdout

    # ---- file transfer -----------------------------------------------------

    def upload(self, local_path: str, remote_path: str) -> None:
        """上传本地文件到远程。"""
        self.sftp.put(local_path, remote_path)

    def download(self, remote_path: str, local_path: str) -> None:
        """从远程下载文件到本地。"""
        self.sftp.get(remote_path, local_path)

    def read_file(self, remote_path: str) -> str:
        """读取远程文件内容并返回字符串。"""
        with self.sftp.open(remote_path, "r") as f:
            return f.read().decode("utf-8", errors="replace")

    def write_file(self, remote_path: str, content: str) -> None:
        """将字符串内容写入远程文件（覆盖）。"""
        with self.sftp.open(remote_path, "w") as f:
            f.write(content)

    def file_exists(self, remote_path: str) -> bool:
        """检查远程路径是否存在。"""
        try:
            self.sftp.stat(remote_path)
            return True
        except FileNotFoundError:
            return False


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def ssh_connect(
    host: str,
    username: str = "root",
    password: str | None = None,
    port: int = 22,
    timeout: float = 15.0,
) -> SSHClient:
    """快捷方法：创建并连接 SSH 客户端。"""
    return SSHClient(host, username=username, password=password, port=port, timeout=timeout, connect=True)


def ssh_execute(
    host: str,
    command: str,
    username: str = "root",
    password: str | None = None,
    port: int = 22,
    timeout: float = 15.0,
) -> SSHResult:
    """快捷方法：连接、执行一条命令、断开连接，返回结果。"""
    with SSHClient(host, username=username, password=password, port=port, timeout=timeout) as c:
        return c.run(command, timeout=timeout)