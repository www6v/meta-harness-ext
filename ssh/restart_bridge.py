"""Restart bridge with fresh ws-token from Codex container."""
import paramiko
import subprocess
import sys
import os

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("124.221.28.203", port=22, username="root", password="1qaZxsw@",
                   timeout=30, allow_agent=False, look_for_keys=False)

    stdin, stdout, stderr = client.exec_command(
        "docker exec codex-poc cat /home/codex/.codex/ws-token", timeout=10)
    token = stdout.read().decode().strip()
    client.close()

    if not token:
        print("ERROR: could not read ws-token", file=sys.stderr)
        sys.exit(1)

    # Set env and start bridge
    os.environ["CODEX_WS_URL"] = "ws://124.221.28.203:8765"
    os.environ["CODEX_WS_TOKEN"] = token
    os.environ["CODEX_SSH_HOST"] = ""
    os.environ["CODEX_REMOTE_PORT"] = "8765"

    bridge_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    log_path = os.path.join(bridge_dir, "bridge_debug.log")
    print(f"Starting bridge with fresh token (len={len(token)})...")
    print(f"Logs: {log_path}")
    log_file = open(log_path, "w", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, "-u", "codex_bridge.py"],
        cwd=bridge_dir,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    print("Bridge started.")

if __name__ == "__main__":
    main()
