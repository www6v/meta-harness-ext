"""Check codex container auth setup via SSH."""
from __future__ import annotations
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from client import SSHClient


def main() -> None:
    with SSHClient("124.221.28.203", "root", "1qaZxsw@", timeout=15.0, connect=True) as c:
        print("\n=== docker ps (codex) ===")
        print(c.run("docker ps --filter name=codex --format '{{.Names}} {{.Image}} {{.Status}}'").stdout)

        print("\n=== inspect codex-poc for env / ports / cmd ===")
        print(c.run("docker inspect codex-poc --format '{{json .Config.Env}}'").stdout)
        print("---")
        print(c.run("docker inspect codex-poc --format '{{json .Config.Cmd}}'").stdout)
        print("---")
        print(c.run("docker inspect codex-poc --format '{{json .NetworkSettings.Ports}}'").stdout)
        print("---")
        print(c.run("docker inspect codex-poc --format '{{json .HostConfig.PortBindings}}'").stdout)

        print("\n=== look for codex config files ===")
        print(c.run("docker exec codex-poc find / -maxdepth 5 -name '*.toml' 2>/dev/null | head -20").stdout)
        print(c.run("docker exec codex-poc find / -maxdepth 5 -name 'config*' -type f 2>/dev/null | head -20").stdout)

        print("\n=== codex process + listen ===")
        print(c.run("docker exec codex-poc sh -c 'ps aux | head -20'").stdout)
        print(c.run("docker exec codex-poc sh -c 'netstat -tlnp 2>/dev/null || ss -tlnp'").stdout)

        print("\n=== check for bearer token in env ===")
        print(c.run("docker exec codex-poc sh -c 'env | grep -iE \"token|key|auth|secret|api\"'").stdout)


if __name__ == "__main__":
    main()
