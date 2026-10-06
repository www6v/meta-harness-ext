# Python SSH Deployment Tools

Temporary deployment scripts for meta-harness to remote servers.

## Usage

```bash
# Set SSH credentials
export SSH_PASSWORD="your-password"

# Run deployment
python deploy.py
```

## Environment Variables

- `SSH_HOST`: Remote server hostname (default: 124.221.28.203)
- `SSH_USER`: Username (default: root)
- `SSH_PASSWORD`: Password (required)

## Files

- `ssh_helper.py` - SSH connection helper with paramiko
- `deploy.py` - Deployment script for meta-harness

## Requirements

```bash
pip install paramiko
```
