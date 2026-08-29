#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
match_root="$(cd "$project_root/.." && pwd)"

export LIMA_HOME="${MATCH_LIMA_HOME:-$match_root/.lima}"
vm_name="${MATCH_LIMA_NAME:-ctf-sandbox}"
ssh_port="${MATCH_LIMA_SSH_PORT:-60022}"

action="${1:-status}"
shift || true

case "$action" in
  start)
    status="$(limactl list "$vm_name" --format '{{.Status}}' 2>/dev/null || true)"
    if [[ -z "$status" ]]; then
      limactl start --name="$vm_name" --mount-none --cpus=4 --memory=8 \
        --disk=60 --ssh-port="$ssh_port" --tty=false --timeout=30m template:docker
    elif [[ "$status" != "Running" ]]; then
      limactl start --tty=false "$vm_name"
    else
      echo "$vm_name is already running"
    fi
    ;;
  stop)
    limactl stop "$vm_name"
    ;;
  status)
    limactl list "$vm_name"
    ;;
  shell)
    limactl shell "$vm_name" "$@"
    ;;
  docker)
    limactl shell "$vm_name" docker "$@"
    ;;
  ssh-config)
    printf '%s\n' "$LIMA_HOME/$vm_name/ssh.config"
    ;;
  *)
    echo "usage: $0 {start|stop|status|shell|docker|ssh-config}" >&2
    exit 2
    ;;
esac
