#!/usr/bin/env python3
"""register-hooks.py <path-to-notify-hook.sh>

Idempotently registers the agent-tickets notify hook with whatever agents are
present on this machine:
  * Claude Code (~/.claude/settings.json):  SessionStart -> "<hook> baseline",
                                            UserPromptSubmit -> "<hook> changes"
  * Codex      (~/.codex/hooks.json):       SessionStart -> "<hook> baseline",
                                            Stop -> "<hook> codex-stop-changes"

Preserves any hooks already configured (e.g. Rewind's Stop hook). Safe to re-run.
Called by install.sh; can also be run directly.
"""
import json, os, sys

if len(sys.argv) != 2:
    sys.exit("usage: register-hooks.py <path-to-notify-hook.sh>")
HOOK = os.path.realpath(sys.argv[1])
if not os.path.exists(HOOK):
    sys.exit("register-hooks: %s does not exist" % HOOK)

MARKER = "notify-hook.sh"   # how we recognise our own hook entries on re-runs


def _entry(mode):
    return {"hooks": [{"type": "command", "command": "%s %s" % (HOOK, mode), "timeout": 10}]}


def _has_ours(event_list, mode):
    for grp in event_list or []:
        for h in (grp.get("hooks") or []):
            cmd = h.get("command", "")
            if MARKER in cmd and cmd.rstrip().endswith(mode):
                return True
    return False


def _remove_ours(event_list):
    changed = False
    kept_groups = []
    for grp in event_list or []:
        hooks = []
        for h in (grp.get("hooks") or []):
            cmd = h.get("command", "")
            if MARKER in cmd:
                changed = True
                continue
            hooks.append(h)
        if hooks:
            new_grp = dict(grp)
            new_grp["hooks"] = hooks
            kept_groups.append(new_grp)
    return kept_groups, changed


def _ensure(hooks_obj, event, mode):
    """Return True if we added an entry."""
    lst = hooks_obj.setdefault(event, [])
    if not isinstance(lst, list):
        return False
    if _has_ours(lst, mode):
        return False
    lst.append(_entry(mode))
    return True


def _ensure_replaced(hooks_obj, event, mode):
    """Ensure this event has exactly one current agent-tickets hook entry."""
    lst = hooks_obj.setdefault(event, [])
    if not isinstance(lst, list):
        return False
    kept, removed = _remove_ours(lst)
    hooks_obj[event] = kept
    added = _ensure(hooks_obj, event, mode)
    return removed or added


def _load(path):
    if os.path.exists(path):
        with open(path) as f:
            txt = f.read().strip()
        return json.loads(txt) if txt else {}
    return {}


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def register_claude():
    path = os.path.expanduser("~/.claude/settings.json")
    if not os.path.isdir(os.path.dirname(path)):
        print("  claude: ~/.claude not present — skipped"); return
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    changed = _ensure(hooks, "SessionStart", "baseline")
    changed = _ensure(hooks, "UserPromptSubmit", "changes") or changed
    if changed:
        _save(path, data); print("  claude: registered notify hook (SessionStart=baseline, UserPromptSubmit=changes) in %s" % path)
    else:
        print("  claude: notify hook already registered")


def register_codex():
    path = os.path.expanduser("~/.codex/hooks.json")
    if not os.path.isdir(os.path.expanduser("~/.codex")):
        print("  codex: ~/.codex not present — skipped"); return
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    changed = _ensure(hooks, "SessionStart", "baseline")
    # Codex has no UserPromptSubmit; "Stop" fires after each agent turn — closest per-turn checkpoint.
    # Stop hook stdout must be valid Codex hook JSON or empty, so use the Codex-specific
    # silent mode and replace older plain-text `changes` registrations from this tool.
    changed = _ensure_replaced(hooks, "Stop", "codex-stop-changes") or changed
    if changed:
        _save(path, data)
        print("  codex:  registered notify hook (SessionStart=baseline, Stop=codex-stop-changes) in %s" % path)
        print("          NOTE: Codex validates hooks by hash — it will ask you to TRUST the new hook")
        print("          the first time you run `codex` after this. Approve it.")
    else:
        print("  codex:  notify hook already registered")


if __name__ == "__main__":
    print("register-hooks: notify hook = %s" % HOOK)
    register_claude()
    register_codex()
