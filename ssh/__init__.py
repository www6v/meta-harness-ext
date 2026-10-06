"""
SSH 客户端工具模块。

提供基于 paramiko 的可复用 SSH 连接和远程命令执行能力，
以及面向 Codex app-server 的 WebSocket JSON-RPC 桥接。
"""

from .client import SSHClient, ssh_execute, ssh_connect
from .codex_client import CodexClient, CodexConfig, CodexTurnResult
from .codex_tunnel import SSHTunnel

__all__ = [
    "SSHClient",
    "ssh_execute",
    "ssh_connect",
    "CodexClient",
    "CodexConfig",
    "CodexTurnResult",
    "SSHTunnel",
]
