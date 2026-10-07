# shellcheck shell=bash
# Sourced helper (S14b T25; reused by S26's fresh-stack script): free loopback ports for every port
# key of a checkout's Makefile `WORKTREE_KEYS :=` line (all keys but COMPOSE_PROJECT_NAME), so a
# new key is covered without editing this file.
#
#   worktree_port_keys MAKEFILE   prints the port keys, one per line
#   free_ports MAKEFILE           prints KEY=PORT lines: distinct ports the kernel just handed out
#                                 on 127.0.0.1 (bind to port 0), none in use at that moment

worktree_port_keys() {
  local makefile="$1" line
  line="$(grep -m1 -E '^WORKTREE_KEYS[[:space:]]*:=' "$makefile")" || {
    echo "free_ports: no WORKTREE_KEYS line in $makefile" >&2
    return 1
  }
  line="${line#*:=}"
  local key
  for key in $line; do
    [ "$key" = "COMPOSE_PROJECT_NAME" ] || printf '%s\n' "$key"
  done
}

free_ports() {
  local makefile="$1" keys
  keys="$(worktree_port_keys "$makefile")" || return 1
  [ -n "$keys" ] || { echo "free_ports: no port keys in $makefile" >&2; return 1; }
  # shellcheck disable=SC2086 # one key per word
  python3 - $keys <<'PY'
import socket
import sys

keys = sys.argv[1:]
sockets, ports = [], []
for _ in keys:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))  # held open until every port is chosen, so no two repeat
    sockets.append(s)
    ports.append(s.getsockname()[1])
for s in sockets:
    s.close()
for key, port in zip(keys, ports):  # noqa: B905 - the system python3 may predate strict=
    print(f"{key}={port}")
PY
}
