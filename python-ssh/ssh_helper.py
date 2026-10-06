"""SSH helper for connecting to remote server."""
import os
import paramiko


def get_ssh_client(host=None, user=None, password=None):
    """Create and return an SSH client.
    
    Configuration from environment variables:
    - SSH_HOST: remote server hostname (default: 124.221.28.203)
    - SSH_USER: username (default: root)
    - SSH_PASSWORD: password (required, no default)
    """
    host = host or os.environ.get("SSH_HOST", "124.221.28.203")
    user = user or os.environ.get("SSH_USER", "root")
    password = password or os.environ.get("SSH_PASSWORD")
    
    if not password:
        raise ValueError("SSH_PASSWORD environment variable is required")
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=22, username=user, password=password, timeout=30)
    return client


def run_cmd(client, cmd, timeout=120):
    """Run a command and return (exit_code, stdout, stderr)."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return exit_code, out, err


def run_cmd_print(client, cmd, timeout=120):
    """Run a command and print output."""
    print(f"$ {cmd}")
    exit_code, out, err = run_cmd(client, cmd, timeout)
    if out.strip():
        print(out.strip())
    if err.strip():
        print(f"STDERR: {err.strip()}")
    print(f"exit={exit_code}")
    print("---")
    return exit_code, out, err
