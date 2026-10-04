#!/usr/bin/env bash
# .worktree.env is an optional, git-ignored file for one checkout. The five keys
# are COMPOSE_PROJECT_NAME, PG_HOST_PORT (default 5432), MINIO_API_HOST_PORT
# (default 9000), MINIO_CONSOLE_HOST_PORT (default 9001) and API_HOST_PORT
# (default 8000). The file wins over the environment. ADR-0011 amendment.
set -euo pipefail

KEYS="COMPOSE_PROJECT_NAME PG_HOST_PORT MINIO_API_HOST_PORT MINIO_CONSOLE_HOST_PORT API_HOST_PORT"

default_of() {
  case "$1" in
    PG_HOST_PORT) printf '%s' 5432 ;;
    MINIO_API_HOST_PORT) printf '%s' 9000 ;;
    MINIO_CONSOLE_HOST_PORT) printf '%s' 9001 ;;
    API_HOST_PORT) printf '%s' 8000 ;;
    *) return 1 ;;
  esac
}

valid_port() {
  case "$1" in
    [1-9]|[1-9][0-9]|[1-9][0-9][0-9]|[1-9][0-9][0-9][0-9]|[1-9][0-9][0-9][0-9][0-9]) ;;
    *) return 1 ;;
  esac
  [ "$1" -le 65535 ]
}

valid_project() {
  case "$1" in
    [a-z0-9]*) ;;
    *) return 1 ;;
  esac
  case "$1" in
    *[!a-z0-9_-]*) return 1 ;;
  esac
}

die_line() {
  echo "worktree_env: $1:$2: $3" >&2
  exit 1
}

has_forbidden() {
  case "$1" in
    *'$'*) return 0 ;;
  esac
  case "$1" in
    *\\*) return 0 ;;
  esac
  case "$1" in
    *$'\r'*) return 0 ;;
  esac
  return 1
}

exports_file() {
  local file="$1"
  if [ ! -e "$file" ]; then
    exit 0
  fi
  if [ ! -f "$file" ]; then
    echo "worktree_env: ${file}: not a regular file" >&2
    exit 1
  fi
  local n=0 line key val seen=" " out=""
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    if has_forbidden "$line"; then
      die_line "$file" "$n" "forbidden character"
    fi
    case "$line" in
      ''|'#'*) continue ;;
    esac
    case "$line" in
      *=*) ;;
      *) die_line "$file" "$n" "expected KEY=VALUE" ;;
    esac
    key="${line%%=*}"
    case " $KEYS " in
      *" $key "*) ;;
      *) die_line "$file" "$n" "unknown key ${key}" ;;
    esac
    case "$seen" in
      *" $key "*) die_line "$file" "$n" "duplicate ${key}" ;;
    esac
    seen="${seen}${key} "
    val="${line#*=}"
    if [ -n "$val" ]; then
      case "$key" in
        COMPOSE_PROJECT_NAME)
          valid_project "$val" || die_line "$file" "$n" "${key} is not a valid project name"
          ;;
        *)
          valid_port "$val" || die_line "$file" "$n" "${key} is not a valid port"
          ;;
      esac
      out="${out}export ${key}=${val}
"
    fi
  done < "$file"
  printf '%s' "$out"
}

normalise_name() {
  printf '%s' "$1" | python3 -c 'import sys,re; print(re.sub(r"[^a-z0-9_-]", "", sys.stdin.read().lower()))'
}

dotenv_value() {
  local key="$1" line val
  [ -f .env ] || return 1
  line="$(grep -E "^(export )?${key}=" .env | tail -n 1 || true)"
  [ -n "$line" ] || return 1
  line="${line#export }"
  val="${line#"${key}="}"
  case "$val" in
    \"*\") val="${val#\"}"; val="${val%\"}" ;;
    \'*\') val="${val#\'}"; val="${val%\'}" ;;
  esac
  printf '%s' "$val"
}

resolved_setting() {
  local key="$1" dotted
  if [ -n "${!key+x}" ]; then
    printf '%s' "${!key}"
    return 0
  fi
  if dotted="$(dotenv_value "$key")"; then
    printf '%s' "$dotted"
    return 0
  fi
  return 1
}

effective_port() {
  local key="$1" val=""
  case "$key" in
    PG_HOST_PORT) val="${PG_HOST_PORT-}" ;;
    MINIO_API_HOST_PORT) val="${MINIO_API_HOST_PORT-}" ;;
    MINIO_CONSOLE_HOST_PORT) val="${MINIO_CONSOLE_HOST_PORT-}" ;;
    API_HOST_PORT) val="${API_HOST_PORT-}" ;;
  esac
  if [ -z "$val" ]; then
    val="$(default_of "$key")"
  fi
  printf '%s' "$val"
}

url_port() {
  printf '%s' "$1" | python3 -c 'import sys, urllib.parse; print(urllib.parse.urlsplit(sys.stdin.read()).port or "")'
}

require_url() {
  local setting="$1" port_key="$2" value="" port="" expected=""
  if ! value="$(resolved_setting "$setting")"; then
    return 0
  fi
  port="$(url_port "$value")" || exit 1
  expected="$(effective_port "$port_key")"
  if [ -z "$port" ]; then
    echo "worktree_env: ${setting} has no port" >&2
    exit 1
  fi
  if [ "$port" != "$expected" ]; then
    echo "worktree_env: ${setting} port ${port} != ${port_key} ${expected}" >&2
    exit 1
  fi
}

require_plain() {
  local setting="$1" port_key="$2" value="" expected=""
  if ! value="$(resolved_setting "$setting")"; then
    return 0
  fi
  expected="$(effective_port "$port_key")"
  if [ "$value" != "$expected" ]; then
    echo "worktree_env: ${setting} port ${value} != ${port_key} ${expected}" >&2
    exit 1
  fi
}

check_project() {
  local git_dir="" common="" primary="" primary_project="" self_project="" exports_out=""
  git_dir="$(git rev-parse --path-format=absolute --git-dir 2>/dev/null)" || return 0
  common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 0
  [ "$git_dir" != "$common" ] || return 0
  primary="$(dirname "$common")"
  if [ -f "$primary/.worktree.env" ]; then
    exports_out="$(exports_file "$primary/.worktree.env")" || exit 1
    primary_project="$(printf '%s\n' "$exports_out" | sed -n 's/^export COMPOSE_PROJECT_NAME=//p')"
  fi
  if [ -z "$primary_project" ]; then
    primary_project="$(normalise_name "$(basename "$primary")")"
  fi
  if [ -n "${COMPOSE_PROJECT_NAME-}" ]; then
    self_project="$COMPOSE_PROJECT_NAME"
  else
    self_project="$(normalise_name "$(basename "$PWD")")"
  fi
  if [ "$self_project" = "$primary_project" ]; then
    echo "worktree_env: COMPOSE_PROJECT_NAME ${self_project} is the primary checkout's project" >&2
    exit 1
  fi
}

check_main() {
  local worktree_exports="" key="" val=""
  worktree_exports="$(exports_file .worktree.env)" || exit 1
  eval "$worktree_exports"
  for key in PG_HOST_PORT MINIO_API_HOST_PORT MINIO_CONSOLE_HOST_PORT API_HOST_PORT; do
    val=""
    case "$key" in
      PG_HOST_PORT) val="${PG_HOST_PORT-}" ;;
      MINIO_API_HOST_PORT) val="${MINIO_API_HOST_PORT-}" ;;
      MINIO_CONSOLE_HOST_PORT) val="${MINIO_CONSOLE_HOST_PORT-}" ;;
      API_HOST_PORT) val="${API_HOST_PORT-}" ;;
    esac
    if [ -z "$val" ]; then
      val="$(default_of "$key")"
    fi
    # Do not round-trip $val through a command substitution: that strips a trailing newline.
    valid_port "$val" || { echo "worktree_env: ${key} is not a valid port" >&2; exit 1; }
  done
  require_url DATABASE_URL PG_HOST_PORT
  require_url ALEMBIC_DATABASE_URL PG_HOST_PORT
  require_url S3_ENDPOINT_URL MINIO_API_HOST_PORT
  require_plain PORT API_HOST_PORT
  check_project
}

usage() {
  echo "usage: worktree_env.sh exports [FILE] | check" >&2
}

case "${1:-}" in
  exports) exports_file "${2:-.worktree.env}" ;;
  check) check_main ;;
  *) usage; exit 2 ;;
esac
