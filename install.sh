#!/usr/bin/env bash
# Roll out the agent ticketing system from this dev directory onto the current machine.
#
# MyTools is where stuff is *created*. Once installed, the artifacts must be
# self-contained — normal ticket operations do not reference back into MyTools at
# runtime. So this script COPIES files out (it does not symlink). Re-run it to
# push updates after editing the source copies here; it overwrites the installed
# CLI/skill/compose but never touches your real config (with the token). It also
# writes a non-secret source manifest so `agent-ticket source-info` can show
# which source folder produced the installed copy and what git status was visible.
#
# Requires: python3. Optional but recommended: docker (+ docker compose plugin).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${KANBOARD_DATA_DIR:-$HOME/kanboard-data}"
CFG_DIR="$HOME/.config/agent-tickets"

die() { echo "agent-tickets/install: ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./install.sh

Roll out the agent ticketing system onto this machine. The installer copies the
CLI, skill, compose file, notify hook, and source manifest into user-level
locations, starts Kanboard when Docker is available, and bootstraps the board
once a real API token is configured.

Live dispatch/supervise/supervise-batch contact paths use guarded
agent-contact probes and need AGENT_CONTACT_TRUSTED_PROVIDER_ROOTS plus
AGENT_CONTACT_TRUSTED_LAUNCHER_ROOTS in the agent environment.

Options:
  -h, --help    show this help and exit without installing
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $arg"
      ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3 is required (used by bootstrap-board.py and the CLI)."

# canonicalise the data dir once, so the guard, mkdir, the .env, and Compose all agree
DATA_DIR="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$DATA_DIR")"

echo "==> agent-tickets: installing (copying) from $DIR"

# --- 0. validate KANBOARD_DATA_DIR is NOT inside a cloud-synced / source tree ---
# A live SQLite DB + a syncing client = corruption risk; that is the whole reason
# data lives outside this repo. Refuse obviously-bad locations (case-insensitive,
# so it also catches macOS-style filesystems).
python3 - "$DATA_DIR" "$DIR" "$HOME" <<'PY' || die "refusing to use that KANBOARD_DATA_DIR (see message above)."
import os, sys
data, src, home = (os.path.realpath(p) for p in sys.argv[1:4])
def bail(why):
    sys.stderr.write(
        "agent-tickets/install: ERROR: KANBOARD_DATA_DIR (%s) %s.\n"
        "  A live Kanboard SQLite DB must not sit there. Pick a plain local path, e.g. ~/kanboard-data,\n"
        "  or unset KANBOARD_DATA_DIR.\n" % (data, why))
    sys.exit(1)
if data == home:
    bail("is your home directory itself")
bad_roots = [src] + [os.path.realpath(os.path.join(home, d)) for d in (
    "Dropbox", "OneDrive", "Google Drive", "GoogleDrive", "iCloud Drive",
    "Library/Mobile Documents", "Nextcloud", "ownCloud", "pCloudDrive", "Sync", ".dropbox",
)]
dl = data.lower()
for r in bad_roots:
    rl = r.lower()
    if dl == rl or dl.startswith(rl + os.sep):
        bail("is under %s (a cloud-synced folder or the source repo)" % r)
PY

# --- 1. CLI -> ~/.local/bin/agent-ticket (real copy) ---
mkdir -p "$HOME/.local/bin"
install -m 0755 "$DIR/bin/agent-ticket" "$HOME/.local/bin/agent-ticket"
echo "    installed ~/.local/bin/agent-ticket"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "    NOTE: add ~/.local/bin to your PATH";; esac

# --- 2. Skill -> agent skills dirs (real copy). Same SKILL.md works for both Claude Code and Codex. ---
for SKILLS_ROOT in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
  if [ -d "$(dirname "$SKILLS_ROOT")" ]; then
    mkdir -p "$SKILLS_ROOT/agent-tickets"
    install -m 0644 "$DIR/skill/SKILL.md" "$SKILLS_ROOT/agent-tickets/SKILL.md"
    echo "    installed $SKILLS_ROOT/agent-tickets/SKILL.md"
  else
    echo "    skipped $SKILLS_ROOT/agent-tickets/SKILL.md ($(dirname "$SKILLS_ROOT") not present)"
  fi
done

# --- 3. Config dir (holds the API token): real config + compose file + .env + notify hook live here ---
mkdir -p "$CFG_DIR"
chmod 700 "$CFG_DIR"   # private — it contains the token
install -m 0644 "$DIR/docker-compose.yml" "$CFG_DIR/docker-compose.yml"
echo "    installed $CFG_DIR/docker-compose.yml"
install -m 0755 "$DIR/scripts/notify-hook.sh" "$CFG_DIR/notify-hook.sh"
echo "    installed $CFG_DIR/notify-hook.sh"
# record source ownership for the installed copy. This is diagnostic metadata
# only; the CLI, hook, and Kanboard runtime remain self-contained if MyTools is
# unavailable.
python3 - "$DIR" "$HOME/.local/bin/agent-ticket" "$CFG_DIR/source.json" <<'PY'
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

source_dir, installed_cli, manifest_path = (os.path.realpath(p) for p in sys.argv[1:4])


def sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def git_metadata_state(path):
    git_path = os.path.join(path, ".git")
    if not os.path.lexists(git_path):
        return {"path": git_path, "state": "absent"}
    if os.path.islink(git_path):
        return {"path": git_path, "state": "symlink", "target": os.path.realpath(git_path)}
    if os.path.isfile(git_path):
        return {"path": git_path, "state": "file"}
    if os.path.isdir(git_path):
        try:
            entries = os.listdir(git_path)
        except OSError as e:
            return {"path": git_path, "state": "unreadable-directory", "error": str(e)}
        return {"path": git_path, "state": "directory" if entries else "empty-directory"}
    return {"path": git_path, "state": "other"}


def git_snapshot(path):
    info = {"available": False, "metadata": git_metadata_state(path)}
    try:
        p = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except FileNotFoundError:
        info["error"] = "git executable not found"
        return info
    except subprocess.TimeoutExpired:
        info["error"] = "git rev-parse timed out"
        return info
    except (OSError, ValueError) as e:
        info["error"] = str(e)
        return info
    if p.returncode != 0:
        info["error"] = first_line(p.stderr) or first_line(p.stdout) or "git rev-parse failed"
        return info
    info["available"] = True
    info["toplevel"] = p.stdout.strip()
    status = subprocess.run(
        ["git", "-C", path, "status", "--short", "--branch"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
    )
    if status.returncode == 0:
        info["status_short_branch"] = status.stdout.splitlines()
    else:
        info["status_error"] = first_line(status.stderr) or first_line(status.stdout)
    head = subprocess.run(
        ["git", "-C", path, "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=5,
    )
    if head.returncode == 0:
        info["head"] = head.stdout.strip()
    return info


manifest = {
    "schema_version": 1,
    "installed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "install_mode": "copy",
    "source_dir": source_dir,
    "source_cli": os.path.join(source_dir, "bin", "agent-ticket"),
    "source_skill": os.path.join(source_dir, "skill", "SKILL.md"),
    "source_notify_hook": os.path.join(source_dir, "scripts", "notify-hook.sh"),
    "installed_cli": installed_cli,
    "installed_codex_skill": os.path.expanduser("~/.codex/skills/agent-tickets/SKILL.md"),
    "installed_claude_skill": os.path.expanduser("~/.claude/skills/agent-tickets/SKILL.md"),
    "installed_notify_hook": os.path.expanduser("~/.config/agent-tickets/notify-hook.sh"),
    "rollout_command": "cd %s && ./install.sh" % source_dir,
    "source_git": git_snapshot(source_dir),
    "source_cli_sha256": sha256(os.path.join(source_dir, "bin", "agent-ticket")),
    "installed_cli_sha256": sha256(installed_cli),
    "source_skill_sha256": sha256(os.path.join(source_dir, "skill", "SKILL.md")),
    "installed_codex_skill_sha256": sha256(os.path.expanduser("~/.codex/skills/agent-tickets/SKILL.md")),
    "installed_claude_skill_sha256": sha256(os.path.expanduser("~/.claude/skills/agent-tickets/SKILL.md")),
    "source_notify_hook_sha256": sha256(os.path.join(source_dir, "scripts", "notify-hook.sh")),
    "installed_notify_hook_sha256": sha256(os.path.expanduser("~/.config/agent-tickets/notify-hook.sh")),
}

os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".source.", suffix=".tmp", dir=os.path.dirname(manifest_path))
try:
    with os.fdopen(fd, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, manifest_path)
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
PY
echo "    wrote $CFG_DIR/source.json (source ownership manifest)"
# record the data dir next to the compose file so a later `docker compose up`
# (without the env var) uses the same location instead of splitting Docker state
printf 'KANBOARD_DATA_DIR=%s\n' "$DATA_DIR" > "$CFG_DIR/.env"
chmod 600 "$CFG_DIR/.env"
echo "    wrote $CFG_DIR/.env (KANBOARD_DATA_DIR=$DATA_DIR)"
if [ ! -f "$CFG_DIR/config.json" ]; then
  install -m 0600 "$DIR/config.example.json" "$CFG_DIR/config.json"
  echo "    created $CFG_DIR/config.json  <-- EDIT: put your Kanboard API token in it"
else
  echo "    $CFG_DIR/config.json already exists, left untouched"
fi
chmod 600 "$CFG_DIR/config.json"   # always: it holds (or will hold) the API token

# --- 3b. Register the notify hook with whatever agents are installed (Claude Code, Codex) ---
# (Harmless when Kanboard is down — the hook is silent on any failure.)
python3 "$DIR/scripts/register-hooks.py" "$CFG_DIR/notify-hook.sh" || echo "    (hook registration had a problem — see above; not fatal)"

# --- 4. Docker preflight ---
DOCKER_OK=0
if ! command -v docker >/dev/null 2>&1; then
  echo "    NOTE: docker not found — Kanboard not started. Install Docker, then re-run this script"
  echo "          (or point the CLI at an existing Kanboard via $CFG_DIR/config.json)."
elif ! docker info >/dev/null 2>&1; then
  echo "    NOTE: docker is installed but the daemon isn't reachable — Kanboard not started."
  echo "          Start Docker (e.g. 'sudo systemctl start docker' or open Docker Desktop), then re-run."
elif ! docker compose version >/dev/null 2>&1; then
  echo "    NOTE: 'docker compose' plugin not available — Kanboard not started."
  echo "          Install the Compose plugin, then re-run."
else
  DOCKER_OK=1
fi

# --- 5. Bring up Kanboard + wait for it (only if Docker is fully OK) ---
KANBOARD_UP=0
if [ "$DOCKER_OK" = "1" ]; then
  mkdir -p "$DATA_DIR/data" "$DATA_DIR/plugins" "$DATA_DIR/ssl"
  echo "    starting Kanboard (data: $DATA_DIR) ..."
  if ! KANBOARD_DATA_DIR="$DATA_DIR" docker compose -f "$CFG_DIR/docker-compose.yml" up -d; then
    die "'docker compose up -d' failed (port 8765 in use? image pull failed?). Fix the error above and re-run."
  fi
  echo "    waiting for Kanboard to answer on http://localhost:8765 ..."
  kb_probe() {   # exit: 0=ready (200/302), 2=answering 5xx, 1=not up yet
    python3 -c '
import urllib.request, urllib.error, sys
try:
    code = urllib.request.urlopen("http://127.0.0.1:8765/", timeout=2).status
except urllib.error.HTTPError as e:
    code = e.code
except Exception:
    sys.exit(1)
if code in (200, 302):
    sys.exit(0)
if 500 <= code < 600:
    sys.exit(2)
sys.exit(1)
' 2>/dev/null
  }
  ready=0; saw5xx=0
  for _ in $(seq 1 30); do
    if kb_probe; then
      ready=1; break
    else
      rc=$?
      if [ "$rc" = "2" ]; then saw5xx=1; fi
    fi
    sleep 1
  done
  if [ "$ready" != "1" ]; then
    if [ "$saw5xx" = "1" ]; then
      die "Kanboard started but is answering HTTP 5xx — broken startup. Check 'docker logs kanboard'."
    fi
    die "Kanboard did not become ready on http://localhost:8765 within ~30s. Check 'docker logs kanboard'."
  fi
  KANBOARD_UP=1
  echo "    Kanboard is up at http://localhost:8765  (first run: log in admin/admin, then change the password)"
fi

# --- 6. Bootstrap the board (project + columns + categories) — needs Kanboard up + a real token ---
if [ "$KANBOARD_UP" != "1" ]; then
  echo "==> CLI + skill installed. Kanboard was NOT started (see notes above) — re-run after fixing Docker."
  echo "    (Normal ticket operations do not reference $DIR at runtime; source-info only reports its manifest.)"
  exit 0
fi

set +e
TOKEN_NOW="$(python3 - "$CFG_DIR/config.json" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("token", "") or "")
except json.JSONDecodeError as e:
    sys.stderr.write("malformed JSON: %s\n" % e); sys.exit(3)
except OSError as e:
    sys.stderr.write("%s\n" % e); sys.exit(4)
PY
)"
rc=$?
set -e
if [ "$rc" = "3" ]; then
  die "$CFG_DIR/config.json is not valid JSON (see error above) — fix it and re-run."
elif [ "$rc" != "0" ]; then
  die "could not read $CFG_DIR/config.json (rc=$rc)."
fi
# AGENT_TICKETS_TOKEN (env) overrides the config file — bootstrap-board.py honors it too.
EFFECTIVE_TOKEN="${AGENT_TICKETS_TOKEN:-$TOKEN_NOW}"
if [ -z "$EFFECTIVE_TOKEN" ] || [ "$EFFECTIVE_TOKEN" = "PUT-YOUR-KANBOARD-API-TOKEN-HERE" ]; then
  echo "==> Kanboard is up but the board isn't set up yet."
  echo "    1) open http://localhost:8765, log in admin/admin, change the password"
  echo "    2) Settings -> API -> copy the 'API token' (the 'jsonrpc' user's application token)"
  echo "       into $CFG_DIR/config.json  (or export AGENT_TICKETS_TOKEN)"
  echo "    3) re-run this script (or:  python3 $DIR/bootstrap-board.py )"
  echo "    (Normal ticket operations do not reference $DIR at runtime; source-info only reports its manifest.)"
  exit 0
fi

echo "    bootstrapping board ..."
if ! python3 "$DIR/bootstrap-board.py"; then
  die "bootstrap-board.py failed (see error above). The board may be incomplete; fix and re-run:  python3 $DIR/bootstrap-board.py"
fi
echo "==> done. Ticketing system is live. Smoke test:  agent-ticket columns"
echo "    (Normal ticket operations do not reference $DIR at runtime — MyTools can be unmounted and the ticketing system still works.)"
echo ""
echo "    NOTE: if you haven't already, change the default 'admin' / 'admin' login at"
echo "          http://localhost:8765 (top-right profile -> Password)."
# (No active credential probe: a failed login would count toward Kanboard's lockout
#  on every re-run once you've changed the password.)
echo ""
echo "    OPTIONAL (for 'agent-ticket dispatch', 'supervise', and 'supervise-batch'): agents can"
echo "    ping or probe a repo's owner agent only if"
echo "    AGENT_CONTACT_TRUSTED_PROVIDER_ROOTS / AGENT_CONTACT_TRUSTED_LAUNCHER_ROOTS are set in their"
echo "    environment. One-time: run 'agent-contact trust-roots --repo <some-repo> --provider codex' (and"
echo "    '--provider claude'), export the printed vars in your shell/agent profile. Until then 'dispatch'"
echo "    degrades gracefully (records a comment, exits 0), while supervised routes report/refuse unsafe"
echo "    live contact paths."
