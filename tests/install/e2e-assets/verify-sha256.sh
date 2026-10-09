#!/usr/bin/env bash
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: verify-sha256.sh FILE EXPECTED_SHA256" >&2; exit 2; }
file=$1
expected=$2
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || { echo "error: expected digest must be lowercase SHA-256" >&2; exit 2; }
[ -f "$file" ] && [ ! -L "$file" ] || { echo "error: candidate is not a regular non-symlink file: $file" >&2; exit 1; }
if command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$file" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$file" | awk '{print $1}')"
else
  echo "error: no SHA-256 implementation available" >&2
  exit 1
fi
[ "$actual" = "$expected" ] || {
  echo "error: candidate digest mismatch: expected $expected, got $actual" >&2
  exit 1
}
