#!/usr/bin/env bash
# Emulate a fresh user install without touching the caller's real ~/.config,
# ~/.local/bin, skills, hooks, or Kanboard data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-all}"

usage() {
  cat <<'EOF'
Usage: scripts/smoke-fresh-install.sh [all|linux-no-docker|wsl-no-docker]

Runs install.sh with an isolated temporary HOME and a deliberately minimal PATH
that contains no docker executable. This validates the fresh-machine bootstrap
path up to the point where Docker/Kanboard credentials are required:

  - CLI copied to ~/.local/bin
  - config directory, compose file, notify hook, source manifest, and .env written
  - config.json created with placeholder API token and mode 0600
  - installer exits cleanly with the Docker prerequisite note

The wsl-no-docker lane is the supported Windows onboarding lane for this Bash
installer: run it inside WSL. Native Windows/PowerShell install is not currently
implemented by this repo.
EOF
}

case "$MODE" in
  -h|--help)
    usage
    exit 0
    ;;
  all|linux-no-docker|wsl-no-docker)
    ;;
  *)
    echo "smoke-fresh-install: unknown mode: $MODE" >&2
    usage >&2
    exit 2
    ;;
esac

is_wsl() {
  [ -n "${WSL_DISTRO_NAME:-}" ] && return 0
  [ -r /proc/version ] && grep -qiE 'microsoft|wsl' /proc/version
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "smoke-fresh-install: required command not found: $cmd" >&2
    exit 2
  }
}

run_no_docker_lane() {
  local label="$1"
  local temp_home temp_bin log_file
  temp_home="$(mktemp -d "/tmp/agent-tickets-${label}.home.XXXXXX")"
  temp_bin="$(mktemp -d "/tmp/agent-tickets-${label}.bin.XXXXXX")"
  log_file="$temp_home/install.log"

  cleanup() {
    rm -rf "$temp_home" "$temp_bin"
  }
  trap cleanup RETURN

  for cmd in bash python3 dirname mkdir install chmod git; do
    require_cmd "$cmd"
    ln -s "$(command -v "$cmd")" "$temp_bin/$cmd"
  done

  echo "==> $label: isolated HOME=$temp_home"
  echo "==> $label: running install.sh with docker intentionally absent from PATH"
  HOME="$temp_home" PATH="$temp_bin" "$ROOT/install.sh" | tee "$log_file"

  HOME="$temp_home" PATH="$temp_bin" python3 - "$temp_home" "$ROOT" "$log_file" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

home = Path(sys.argv[1])
root = Path(sys.argv[2]).resolve()
log_file = Path(sys.argv[3])

def fail(message):
    raise SystemExit("fresh-install validation failed: " + message)

def require_file(path, mode=None):
    if not path.is_file():
        fail("missing file %s" % path)
    if mode is not None:
        actual = stat.S_IMODE(path.stat().st_mode)
        if actual != mode:
            fail("%s mode is %04o, expected %04o" % (path, actual, mode))

cfg_dir = home / ".config" / "agent-tickets"
require_file(home / ".local" / "bin" / "agent-ticket", 0o755)
require_file(cfg_dir / "docker-compose.yml", 0o644)
require_file(cfg_dir / "notify-hook.sh", 0o755)
require_file(cfg_dir / "source.json", 0o644)
require_file(cfg_dir / ".env", 0o600)
require_file(cfg_dir / "config.json", 0o600)

cfg_mode = stat.S_IMODE(cfg_dir.stat().st_mode)
if cfg_mode != 0o700:
    fail("%s mode is %04o, expected 0700" % (cfg_dir, cfg_mode))

config = json.loads((cfg_dir / "config.json").read_text())
if config.get("endpoint") != "http://127.0.0.1:8765/jsonrpc.php":
    fail("unexpected endpoint %r" % config.get("endpoint"))
if config.get("token") != "PUT-YOUR-KANBOARD-API-TOKEN-HERE":
    fail("config token is not the fresh placeholder")
if "project_id" in config:
    fail("fresh placeholder config should not contain project_id")

source = json.loads((cfg_dir / "source.json").read_text())
if Path(source.get("source_dir", "")).resolve() != root:
    fail("source manifest source_dir does not point at repo root")
if not source.get("installed_cli_sha256"):
    fail("source manifest did not hash installed CLI")

env_text = (cfg_dir / ".env").read_text()
if "KANBOARD_DATA_DIR=" not in env_text or str(home / "kanboard-data") not in env_text:
    fail(".env does not point KANBOARD_DATA_DIR at the isolated home")

log = log_file.read_text(errors="replace")
if "docker not found" not in log:
    fail("installer did not report the expected Docker prerequisite")
if "Kanboard was NOT started" not in log:
    fail("installer did not stop at the no-Docker onboarding boundary")

print("fresh-install validation: ok")
PY

  echo "==> $label: ok"
}

if [ "$MODE" = "all" ] || [ "$MODE" = "linux-no-docker" ]; then
  run_no_docker_lane "linux-no-docker"
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "wsl-no-docker" ]; then
  if is_wsl; then
    run_no_docker_lane "wsl-no-docker"
  else
    echo "==> wsl-no-docker: skipped (not running inside WSL)"
  fi
fi
