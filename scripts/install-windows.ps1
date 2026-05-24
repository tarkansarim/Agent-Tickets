param(
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Show-Usage {
    @"
Usage: install-windows.bat

Installs agent-tickets on native Windows. The installer copies the CLI, skill,
compose file, notify hook, and source manifest into user-level locations, starts
Kanboard with Docker Desktop when available, and bootstraps the board once a
real Kanboard API token is configured.

Run from cmd.exe:
  install-windows.bat

Run from PowerShell:
  powershell.exe -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
"@
}

if ($Help) {
    Show-Usage
    exit 0
}

function Die([string]$Message) {
    Write-Error "agent-tickets/install-windows: ERROR: $Message"
    exit 1
}

function Get-CommandName([string[]]$Candidates) {
    foreach ($candidate in $Candidates) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $cmd) {
            return $cmd.Source
        }
    }
    return $null
}

function Convert-ToDockerPath([string]$Path) {
    return ([System.IO.Path]::GetFullPath($Path) -replace "\\", "/")
}

function Test-UnderPath([string]$Child, [string]$Parent) {
    $childFull = [System.IO.Path]::GetFullPath($Child).TrimEnd("\", "/")
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd("\", "/")
    return $childFull.Equals($parentFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        $childFull.StartsWith($parentFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir ".."))
$HomeDir = [Environment]::GetFolderPath("UserProfile")
if ([string]::IsNullOrWhiteSpace($HomeDir)) {
    Die "could not resolve the current user's profile directory."
}

$Python = Get-CommandName @("python", "py")
if ($null -eq $Python) {
    Die "Python 3 is required and must be on PATH."
}

if ($env:KANBOARD_DATA_DIR) {
    $DataDir = [System.IO.Path]::GetFullPath($env:KANBOARD_DATA_DIR)
} else {
    $DataDir = [System.IO.Path]::GetFullPath((Join-Path $HomeDir "kanboard-data"))
}
$CfgDir = Join-Path $HomeDir ".config\agent-tickets"
$BinDir = Join-Path $HomeDir ".local\bin"

if ([System.IO.Path]::GetFullPath($DataDir).TrimEnd("\", "/").Equals([System.IO.Path]::GetFullPath($HomeDir).TrimEnd("\", "/"), [System.StringComparison]::OrdinalIgnoreCase)) {
    Die "KANBOARD_DATA_DIR must not be the home directory itself. Use a local non-cloud path such as $HomeDir\kanboard-data."
}

$badRoots = @(
    $Root,
    (Join-Path $HomeDir "Dropbox"),
    (Join-Path $HomeDir "OneDrive"),
    (Join-Path $HomeDir "Google Drive"),
    (Join-Path $HomeDir "GoogleDrive"),
    (Join-Path $HomeDir "iCloud Drive"),
    (Join-Path $HomeDir "Nextcloud"),
    (Join-Path $HomeDir "ownCloud"),
    (Join-Path $HomeDir "pCloudDrive"),
    (Join-Path $HomeDir "Sync")
)
foreach ($badRoot in $badRoots) {
    if (Test-UnderPath $DataDir $badRoot) {
        Die "KANBOARD_DATA_DIR ($DataDir) is under $badRoot. A live Kanboard SQLite DB must be on a local non-cloud path."
    }
}

Write-Host "==> agent-tickets: installing on Windows from $Root"

New-Item -ItemType Directory -Force -Path $BinDir, $CfgDir | Out-Null

$CliPy = Join-Path $BinDir "agent-ticket.py"
$CliCmd = Join-Path $BinDir "agent-ticket.cmd"
Copy-Item -Force (Join-Path $Root "bin\agent-ticket") $CliPy
@"
@echo off
"$Python" "%~dp0agent-ticket.py" %*
"@ | Set-Content -Encoding ASCII $CliCmd
Write-Host "    installed $CliPy"
Write-Host "    installed $CliCmd"
if (($env:PATH -split ";") -notcontains $BinDir) {
    Write-Host "    NOTE: add $BinDir to your PATH"
}

foreach ($skillsRoot in @((Join-Path $HomeDir ".claude\skills"), (Join-Path $HomeDir ".codex\skills"))) {
    $providerHome = Split-Path -Parent $skillsRoot
    if (Test-Path $providerHome) {
        $dest = Join-Path $skillsRoot "agent-tickets"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        $skillDest = Join-Path $dest "SKILL.md"
        Copy-Item -Force (Join-Path $Root "skill\SKILL.md") $skillDest
        Write-Host "    installed $skillDest"
    }
}

$ComposeDest = Join-Path $CfgDir "docker-compose.yml"
$NotifyDest = Join-Path $CfgDir "notify-hook.sh"
$NotifyCmd = Join-Path $CfgDir "notify-hook.cmd"
$SourceJson = Join-Path $CfgDir "source.json"
$EnvDest = Join-Path $CfgDir ".env"
$ConfigDest = Join-Path $CfgDir "config.json"

Copy-Item -Force (Join-Path $Root "docker-compose.yml") $ComposeDest
Copy-Item -Force (Join-Path $Root "scripts\notify-hook.sh") $NotifyDest
@"
@echo off
bash "%~dp0notify-hook.sh" %*
"@ | Set-Content -Encoding ASCII $NotifyCmd
Write-Host "    installed $ComposeDest"
Write-Host "    installed $NotifyDest"
Write-Host "    installed $NotifyCmd"

$manifestScript = @'
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

home = os.path.expanduser("~")
manifest = {
    "schema_version": 1,
    "installed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "install_mode": "copy",
    "platform": "windows",
    "source_dir": source_dir,
    "source_cli": os.path.join(source_dir, "bin", "agent-ticket"),
    "source_skill": os.path.join(source_dir, "skill", "SKILL.md"),
    "source_notify_hook": os.path.join(source_dir, "scripts", "notify-hook.sh"),
    "installed_cli": installed_cli,
    "installed_cli_launcher": os.path.join(os.path.dirname(installed_cli), "agent-ticket.cmd"),
    "installed_codex_skill": os.path.join(home, ".codex", "skills", "agent-tickets", "SKILL.md"),
    "installed_claude_skill": os.path.join(home, ".claude", "skills", "agent-tickets", "SKILL.md"),
    "installed_notify_hook": os.path.join(home, ".config", "agent-tickets", "notify-hook.sh"),
    "installed_notify_hook_launcher": os.path.join(home, ".config", "agent-tickets", "notify-hook.cmd"),
    "rollout_command": "cd %s && install-windows.bat" % source_dir,
    "source_git": git_snapshot(source_dir),
    "source_cli_sha256": sha256(os.path.join(source_dir, "bin", "agent-ticket")),
    "installed_cli_sha256": sha256(installed_cli),
    "source_skill_sha256": sha256(os.path.join(source_dir, "skill", "SKILL.md")),
    "installed_codex_skill_sha256": sha256(os.path.join(home, ".codex", "skills", "agent-tickets", "SKILL.md")),
    "installed_claude_skill_sha256": sha256(os.path.join(home, ".claude", "skills", "agent-tickets", "SKILL.md")),
    "source_notify_hook_sha256": sha256(os.path.join(source_dir, "scripts", "notify-hook.sh")),
    "installed_notify_hook_sha256": sha256(os.path.join(home, ".config", "agent-tickets", "notify-hook.sh")),
}

os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".source.", suffix=".tmp", dir=os.path.dirname(manifest_path))
try:
    with os.fdopen(fd, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    os.replace(tmp, manifest_path)
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
'@
$manifestScript | & $Python - $Root $CliPy $SourceJson
Write-Host "    wrote $SourceJson (source ownership manifest)"

$DockerDataDir = Convert-ToDockerPath $DataDir
"KANBOARD_DATA_DIR=$DockerDataDir" | Set-Content -Encoding ASCII $EnvDest
Write-Host "    wrote $EnvDest (KANBOARD_DATA_DIR=$DockerDataDir)"

if (-not (Test-Path $ConfigDest)) {
    Copy-Item -Force (Join-Path $Root "config.example.json") $ConfigDest
    Write-Host "    created $ConfigDest  <-- EDIT: put your Kanboard API token in it"
} else {
    Write-Host "    $ConfigDest already exists, left untouched"
}

if (Get-Command bash -ErrorAction SilentlyContinue) {
    & $Python (Join-Path $Root "scripts\register-hooks.py") $NotifyCmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    (hook registration had a problem; not fatal)"
    }
} else {
    Write-Host "    NOTE: bash not found; agent notify hooks were not registered. Core CLI/Kanboard usage still works."
}

$Docker = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $Docker) {
    Write-Host "    NOTE: docker not found - Kanboard not started. Install Docker Desktop, then re-run this script."
    Write-Host "==> CLI installed. Kanboard was NOT started."
    exit 0
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    NOTE: Docker is installed but the daemon is not reachable. Start Docker Desktop, then re-run."
    Write-Host "==> CLI installed. Kanboard was NOT started."
    exit 0
}

& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    NOTE: 'docker compose' plugin not available. Install/update Docker Desktop, then re-run."
    Write-Host "==> CLI installed. Kanboard was NOT started."
    exit 0
}

New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "data"), (Join-Path $DataDir "plugins"), (Join-Path $DataDir "ssl") | Out-Null
Write-Host "    starting Kanboard (data: $DataDir) ..."
$env:KANBOARD_DATA_DIR = $DockerDataDir
& docker compose -f $ComposeDest up -d
if ($LASTEXITCODE -ne 0) {
    Die "'docker compose up -d' failed. Check Docker Desktop, port 8765, and image pull output, then re-run."
}

Write-Host "    waiting for Kanboard to answer on http://localhost:8765 ..."
$ready = $false
$saw5xx = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8765/" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200 -or $response.StatusCode -eq 302) {
            $ready = $true
            break
        }
        if ($response.StatusCode -ge 500 -and $response.StatusCode -lt 600) {
            $saw5xx = $true
        }
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -ge 500) {
            $saw5xx = $true
        }
    }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    if ($saw5xx) {
        Die "Kanboard started but is answering HTTP 5xx. Check 'docker logs kanboard'."
    }
    Die "Kanboard did not become ready on http://localhost:8765 within about 30s. Check 'docker logs kanboard'."
}
Write-Host "    Kanboard is up at http://localhost:8765 (first run: log in admin/admin, then change the password)"

try {
    $config = Get-Content -Raw $ConfigDest | ConvertFrom-Json
} catch {
    Die "$ConfigDest is not valid JSON. Fix it and re-run."
}
$tokenNow = ""
if ($null -ne $config.PSObject.Properties["token"]) {
    $tokenNow = [string]$config.token
}
$effectiveToken = if ($env:AGENT_TICKETS_TOKEN) { $env:AGENT_TICKETS_TOKEN } else { $tokenNow }
if ([string]::IsNullOrWhiteSpace($effectiveToken) -or $effectiveToken -eq "PUT-YOUR-KANBOARD-API-TOKEN-HERE") {
    Write-Host "==> Kanboard is up but the board is not set up yet."
    Write-Host "    1) open http://localhost:8765, log in admin/admin, change the password"
    Write-Host "    2) Settings -> API -> copy the 'API token' into $ConfigDest"
    Write-Host "    3) re-run install-windows.bat"
    exit 0
}

Write-Host "    bootstrapping board ..."
& $Python (Join-Path $Root "bootstrap-board.py")
if ($LASTEXITCODE -ne 0) {
    Die "bootstrap-board.py failed. The board may be incomplete; fix and re-run install-windows.bat."
}

Write-Host "==> done. Ticketing system is live. Smoke test: agent-ticket columns"
