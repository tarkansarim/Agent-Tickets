#!/usr/bin/env bash
# Agent-neutral notify hook for the agent ticketing system.
# Wired into Claude Code (SessionStart + UserPromptSubmit) and Codex (SessionStart + Stop)
# by install.sh. Surfaces open agent-tickets for the repo the agent is working in:
#
#   notify-hook.sh baseline   # print ALL currently-open tickets for this repo, seed the seen-cache
#   notify-hook.sh changes              # print only tickets that appeared since the last check (then update cache)
#   notify-hook.sh codex-stop-changes   # update the seen-cache, but keep stdout empty for Codex Stop JSON contract
#
# Repo is identified by the git toplevel's directory name (matching the `project:<name>`
# tag convention). Silent — prints nothing — when there's nothing new, when Kanboard is
# down, or when the CLI isn't installed. Never blocks the agent; always exits 0.
set -u
MODE="${1:-changes}"
OUTPUT_MODE="text"
if [ "$MODE" = "codex-stop-changes" ]; then
  MODE="changes"
  OUTPUT_MODE="codex-stop"
fi

CLI="$HOME/.local/bin/agent-ticket"
if ! [ -x "$CLI" ]; then
  command -v agent-ticket >/dev/null 2>&1 && CLI=agent-ticket || exit 0
fi

# repo name from the git toplevel, else the current dir's basename
REPO="$(basename "$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || echo "$PWD")")"
[ -n "$REPO" ] || exit 0

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/agent-tickets"
mkdir -p "$CACHE_DIR" 2>/dev/null || exit 0
SAFE_REPO="$(printf '%s' "$REPO" | tr -c 'A-Za-z0-9._-' '_')"
SEEN="$CACHE_DIR/seen-$SAFE_REPO.json"

# fetch open tickets for this repo as JSON; ticket listing can fail when Kanboard
# is down, but pending local callback outbox notices should still be surfaced.
JSON="$("$CLI" list --project "$REPO" --json 2>/dev/null)" || JSON=""

if [ -n "$JSON" ]; then
PY_STDOUT="/dev/stdout"
if [ "$OUTPUT_MODE" = "codex-stop" ]; then
  PY_STDOUT="/dev/null"
fi
AGENT_TICKETS_JSON="$JSON" AT_MODE="$MODE" AT_REPO="$REPO" AT_SEEN="$SEEN" python3 <<'PY' >"$PY_STDOUT" || true
import json, os, sys
mode = os.environ.get("AT_MODE", "changes")
repo = os.environ.get("AT_REPO", "")
seen_path = os.environ.get("AT_SEEN", "")
try:
    tickets = json.loads(os.environ.get("AGENT_TICKETS_JSON", "[]"))
except Exception:
    sys.exit(0)
if not isinstance(tickets, list):
    sys.exit(0)
cur = {}
for t in tickets:
    try:
        cur[int(t["id"])] = t
    except Exception:
        pass
prev = set()
if mode == "changes" and seen_path and os.path.exists(seen_path):
    try:
        prev = set(int(x) for x in json.load(open(seen_path)))
    except Exception:
        prev = set()
# update the cache to the current open set (so closed tickets drop out;
# a reopened ticket will surface again on the next "changes" check)
if seen_path:
    try:
        tmp = seen_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(cur.keys()), f)
        os.replace(tmp, seen_path)
    except Exception:
        pass
show_ids = sorted(cur.keys()) if mode == "baseline" else sorted(set(cur.keys()) - prev)
if not show_ids:
    sys.exit(0)
label = "Open" if mode == "baseline" else "New"
print("🎫 %s agent-ticket(s) for %r — use the `agent-tickets` skill / `agent-ticket` CLI to triage:" % (label, repo))
for tid in show_ids:
    t = cur[tid]
    tags = t.get("tags") or []
    extra = ("  {" + ", ".join(str(x) for x in tags) + "}") if tags else ""
    print("  #%-4s %s%s   %s" % (tid, t.get("title", ""), extra, t.get("url", "")))
print("  (`agent-ticket show <id>` for details; if you OWN this repo, fix in place and `agent-ticket close <id>`.)")
PY
fi

if [ "$OUTPUT_MODE" != "codex-stop" ]; then
  "$CLI" callbacks --pending --repo "$REPO" 2>/dev/null || true
fi
exit 0
