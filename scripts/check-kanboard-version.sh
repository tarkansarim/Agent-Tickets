#!/usr/bin/env bash
# Compare the Kanboard image tag pinned in docker-compose.yml against the latest
# GitHub release, and remind you to bump if behind. Read-only; never changes anything.
#
#   scripts/check-kanboard-version.sh
#
# To bump deliberately: edit the `image:` tag in docker-compose.yml, re-run
# ../install.sh (or `docker compose -f ~/.config/agent-tickets/docker-compose.yml up -d`),
# then smoke-test:  agent-ticket columns
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pinned="$(grep -oE 'kanboard/kanboard:[^[:space:]"#]+' "$DIR/docker-compose.yml" | head -1 | cut -d: -f2 || true)"
[ -n "$pinned" ] || { echo "could not find a pinned kanboard/kanboard:<tag> in $DIR/docker-compose.yml" >&2; exit 1; }

fetch() { command -v curl >/dev/null 2>&1 && curl -fsSL "$1" || { command -v wget >/dev/null 2>&1 && wget -qO- "$1"; }; }
latest="$(fetch https://api.github.com/repos/kanboard/kanboard/releases/latest 2>/dev/null \
          | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name",""))' 2>/dev/null || true)"

echo "pinned:  $pinned"
if [ -z "$latest" ]; then
  echo "latest:  (could not fetch — check https://github.com/kanboard/kanboard/releases manually)"
  exit 0
fi
echo "latest:  $latest"
if [ "$pinned" = "$latest" ]; then
  echo "=> up to date."
else
  echo "=> a newer Kanboard release exists. Review the changelog, then bump the image tag in"
  echo "   $DIR/docker-compose.yml to '$latest', re-run install.sh, and smoke-test 'agent-ticket columns'."
fi
