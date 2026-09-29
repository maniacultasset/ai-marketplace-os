#!/usr/bin/env bash
# The `gitleaks` check and pre-commit hook: a pinned gitleaks, checksum-verified, in .tools/.
# Modes:
#   --staged                  the staged changes (pre-commit)
#   BASE_SHA and HEAD_SHA     that range: a PR's base..head, or a push's before..after
#   otherwise                 origin/main..HEAD
# A zero or unknown BASE_SHA (a new ref, a force-push) never skips: it scans all history reachable
# from HEAD_SHA instead.
set -euo pipefail
cd "$(dirname "$0")/../.."

VERSION="8.30.1"
SHA256_LINUX_X64="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
SHA256_DARWIN_ARM64="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) platform="linux_x64"; want="$SHA256_LINUX_X64" ;;
  Darwin-arm64) platform="darwin_arm64"; want="$SHA256_DARWIN_ARM64" ;;
  *) echo "gitleaks.sh: unsupported platform $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

bin=".tools/gitleaks-${VERSION}/gitleaks"
if [ ! -x "$bin" ]; then
  archive="gitleaks_${VERSION}_${platform}.tar.gz"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -sSfL -o "$tmp/$archive" \
    "https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${archive}"
  if command -v sha256sum >/dev/null; then
    got="$(sha256sum "$tmp/$archive" | cut -d' ' -f1)"
  else
    got="$(shasum -a 256 "$tmp/$archive" | cut -d' ' -f1)"
  fi
  if [ "$got" != "$want" ]; then
    echo "gitleaks.sh: checksum mismatch for $archive" >&2
    exit 1
  fi
  tar -xzf "$tmp/$archive" -C "$tmp" gitleaks
  mkdir -p "$(dirname "$bin")"
  mv "$tmp/gitleaks" "$bin"
fi

args=(--redact --no-banner --exit-code 1 --config .gitleaks.toml)

if [ "${1:-}" = "--staged" ]; then
  echo "gitleaks: scanning staged changes"
  exec "$bin" git --staged "${args[@]}" .
fi

base="${BASE_SHA:-}"
head="${HEAD_SHA:-}"
if [ -n "$base" ] || [ -n "$head" ]; then
  : "${head:?HEAD_SHA is required with BASE_SHA}"
  if [ -z "$base" ] || [[ "$base" =~ ^0+$ ]] || ! git cat-file -e "${base}^{commit}" 2>/dev/null; then
    echo "gitleaks: base '${base}' is zero or unknown; scanning all history reachable from ${head}"
    exec "$bin" git "${args[@]}" --log-opts="$head" .
  fi
  echo "gitleaks: scanning ${base}..${head}"
  exec "$bin" git "${args[@]}" --log-opts="${base}..${head}" .
fi

echo "gitleaks: scanning origin/main..HEAD"
exec "$bin" git "${args[@]}" --log-opts="origin/main..HEAD" .
