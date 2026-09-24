#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
files=$(grep -E '^- filename:' .talismanrc | awk '{print $3}')
{
  sed '/^fileignoreconfig:/,$d' .talismanrc
  echo "fileignoreconfig:"
  for f in $files; do
    [ -f "$f" ] || continue
    sum=$(talisman --checksum "$f" 2>/dev/null | awk '/checksum:/{print $2}')
    printf -- "- filename: %s\n  checksum: %s\n" "$f" "$sum"
  done
  echo 'version: "1.0"'
} > .talismanrc.new
mv .talismanrc.new .talismanrc
