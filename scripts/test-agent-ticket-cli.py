#!/usr/bin/env python3
"""Focused tests for agent-ticket CLI behavior that does not need Kanboard."""
import argparse
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "bin" / "agent-ticket"


def load_cli():
    loader = SourceFileLoader("agent_ticket_cli", str(CLI_PATH))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class AgentTicketCliTests(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        callback_dir = pathlib.Path(self.tmpdir.name) / "callbacks"
        self.cli.CALLBACK_DIR = str(callback_dir)
        self.cli.WATCHERS_PATH = str(callback_dir / "watchers.json")
        self.cli.WATCHERS_LOCK_PATH = str(callback_dir / "watchers.lock")
        self.cli.OUTBOX_DIR = str(callback_dir / "outbox")
        supervision_dir = pathlib.Path(self.tmpdir.name) / "supervision"
        self.cli.SUPERVISION_DIR = str(supervision_dir)
        self.cli.SUPERVISION_LEASES_PATH = str(supervision_dir / "leases.json")
        self.cli.SUPERVISION_LOCK_PATH = str(supervision_dir / "leases.lock")
        self.cli.BATCH_LOCK_DIR = str(pathlib.Path(self.tmpdir.name) / "batch-locks")

    def reserve_then_notify(self, cfg, ticket_id):
        self.cli._reserve_ticket_closed_callback(cfg, ticket_id)
        return self.cli._notify_ticket_closed(cfg, ticket_id)

    def batch_args(self, **overrides):
        values = {
            "project": None,
            "severity": None,
            "kind": None,
            "tag": None,
            "column": None,
            "include_p3": False,
            "include_needs_human": False,
            "provider": None,
            "session_prefix": "batch-owner",
            "full_permission": False,
            "message": "",
            "dry_run": False,
            "poll_interval": 0,
            "max_polls": 0,
            "strict_closeout": False,
            "require_clean": False,
            "require_validation": False,
            "require_commit": False,
            "require_install": False,
            "supervisor_id": "test-supervisor",
            "supervision_ttl_hours": 1,
            "adopt_supervision": False,
            "steal_supervision": False,
            "force_supervision": False,
            "json": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def write_supervision_claims(self, claims):
        path = pathlib.Path(self.cli.SUPERVISION_LEASES_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "claims": claims}, indent=2, sort_keys=True) + "\n")

    def active_supervision_claim(self, claim_id="claim-demo", owner_id="other-supervisor",
                                 repo="/tmp/demo", ticket_ids=None, now=None, ttl=3600,
                                 worker_session="batch-owner-demo", worker_provider="codex"):
        now = time.time() if now is None else now
        return {
            "claim_id": claim_id,
            "repo": repo,
            "repo_key": "demo-key",
            "project": "demo",
            "projects": ["demo"],
            "ticket_ids": list(ticket_ids or [80]),
            "command": "supervise-batch",
            "owner_id": owner_id,
            "origin_provider": "codex",
            "origin_repo": "/tmp/origin",
            "origin_session": "origin-session",
            "worker_provider": worker_provider,
            "worker_session": worker_session,
            "worker_mode": "launch",
            "created_at": self.cli._utc_iso(now - 20),
            "created_at_epoch": now - 20,
            "updated_at": self.cli._utc_iso(now - 5),
            "updated_at_epoch": now - 5,
            "last_heartbeat_at": self.cli._utc_iso(now - 5),
            "last_heartbeat_epoch": now - 5,
            "expires_at": self.cli._utc_iso(now + ttl),
            "expires_at_epoch": now + ttl,
            "heartbeat_count": 1,
        }

    def test_rpc_retries_transient_database_lock(self):
        responses = [
            {"error": {"code": 0, "message": "SQL Error[HY000]: SQLSTATE[HY000]: General error: 5 database is locked"}},
            {"error": {"code": 0, "message": "database is locked"}},
            {"result": {"ok": True}},
        ]
        calls = []
        sleeps = []

        def fake_urlopen(req, timeout):
            calls.append((req, timeout))
            return FakeResponse(responses.pop(0))

        with mock.patch.object(self.cli.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(self.cli.time, "sleep", side_effect=lambda delay: sleeps.append(delay)), \
             mock.patch("sys.stderr", new=io.StringIO()):
            result = self.cli.rpc({"endpoint": "http://kanboard.invalid/jsonrpc.php", "token": "t"}, "closeTask", {"task_id": 1})

        self.assertEqual({"ok": True}, result)
        self.assertEqual(3, len(calls))
        self.assertEqual([self.cli.RPC_LOCK_RETRY_DELAYS[0], self.cli.RPC_LOCK_RETRY_DELAYS[1]], sleeps)

    def test_rpc_does_not_retry_non_lock_api_error(self):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append((req, timeout))
            return FakeResponse({"error": {"code": 123, "message": "permission denied"}})

        with mock.patch.object(self.cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(SystemExit) as caught:
                self.cli.rpc({"endpoint": "http://kanboard.invalid/jsonrpc.php", "token": "t"}, "closeTask", {"task_id": 1})

        self.assertIn("permission denied", str(caught.exception))
        self.assertEqual(1, len(calls))

    def test_close_moves_open_task_to_done_before_closing(self):
        calls = []
        task = {"id": 42, "column_id": 3, "swimlane_id": 7, "is_active": 1}

        def fake_rpc(cfg, method, params=None):
            calls.append((method, params))
            return True

        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "resolve_column_id", return_value=5), \
             mock.patch.object(self.cli, "rpc", side_effect=fake_rpc), \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.cli.cmd_close({"project_id": 1}, argparse.Namespace(id=42, json=False))

        self.assertEqual(
            [
                ("moveTaskPosition", {"project_id": 1, "task_id": 42, "column_id": 5, "position": 1, "swimlane_id": 7}),
                ("closeTask", {"task_id": 42}),
            ],
            calls,
        )

    def test_close_moves_inactive_task_to_done_without_reclosing(self):
        calls = []
        task = {"id": 43, "column_id": 3, "swimlane_id": 1, "is_active": 0}

        def fake_rpc(cfg, method, params=None):
            calls.append((method, params))
            return True

        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "resolve_column_id", return_value=5), \
             mock.patch.object(self.cli, "rpc", side_effect=fake_rpc), \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.cli.cmd_close({"project_id": 1}, argparse.Namespace(id=43, json=False))

        self.assertEqual(
            [("moveTaskPosition", {"project_id": 1, "task_id": 43, "column_id": 5, "position": 1, "swimlane_id": 1})],
            calls,
        )

    def test_reopen_moves_to_live_column_and_records_audit_comment(self):
        calls = []
        task = {"id": 44, "column_id": 5, "swimlane_id": 1, "is_active": 0}

        def fake_rpc(cfg, method, params=None):
            calls.append((method, params))
            return True

        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "resolve_column_id", return_value=2), \
             mock.patch.object(self.cli, "rpc", side_effect=fake_rpc), \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.cli.cmd_reopen({"project_id": 1, "comment_user_id": 9}, argparse.Namespace(id=44, column="Triaging", json=False))

        self.assertEqual("openTask", calls[0][0])
        self.assertEqual("moveTaskPosition", calls[1][0])
        self.assertEqual("createComment", calls[2][0])
        self.assertIn("agent-ticket reopen:", calls[2][1]["content"])
        self.assertIn("Triaging", calls[2][1]["content"])

    def test_closeout_strict_requires_validation_evidence(self):
        task = {"id": 45, "column_id": 5, "swimlane_id": 1, "is_active": 0}
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "_ticket_comments", return_value=[{"comment": "Fixed in source."}]), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "column_name", return_value="Done"), \
             mock.patch.object(self.cli, "_git_worktree_info", return_value={"available": True, "clean": True, "head": "abc1234"}):
            report = self.cli._closeout_report({"project_id": 1}, 45, strict=True)

        self.assertFalse(report["ok"])
        validation = [c for c in report["checks"] if c["name"] == "validation_evidence"][0]
        self.assertEqual("fail", validation["status"])
        self.assertTrue(validation["required"])

    def test_closeout_flags_dirty_worktree(self):
        task = {"id": 46, "column_id": 5, "swimlane_id": 1, "is_active": 0}
        comments = [{"comment": "Validation: python3 tests OK. Commit abcdef123456."}]
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "_ticket_comments", return_value=comments), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "column_name", return_value="Done"), \
             mock.patch.object(self.cli, "_git_worktree_info", return_value={
                 "available": True, "clean": False, "status_short": [" M src/file.py"], "head": "abcdef1"
             }):
            report = self.cli._closeout_report({"project_id": 1}, 46, strict=True)

        self.assertFalse(report["ok"])
        worktree = [c for c in report["checks"] if c["name"] == "worktree_clean"][0]
        self.assertEqual("fail", worktree["status"])

    def test_closeout_does_not_treat_reviewer_uuid_as_commit_evidence(self):
        task = {"id": 48, "column_id": 5, "swimlane_id": 1, "is_active": 0}
        comments = [{"comment": "Validation: tests OK. reviewer 019e19ea-3e0b-7be3-9876-7aaff275660b"}]
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "_ticket_comments", return_value=comments), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "column_name", return_value="Done"), \
             mock.patch.object(self.cli, "_git_worktree_info", return_value={"available": False, "error": ".git absent"}):
            report = self.cli._closeout_report({"project_id": 1}, 48, strict=True)

        commit = [c for c in report["checks"] if c["name"] == "commit_id"][0]
        self.assertEqual("warn", commit["status"])
        self.assertIn("no commit id", commit["detail"])

    def test_closeout_commit_evidence_prefers_latest_comment_and_reports_source(self):
        task = {"id": 49, "column_id": 5, "swimlane_id": 1, "is_active": 0}
        comments = [
            {
                "id": 10,
                "date_creation": 100,
                "comment": "Partial progress. Validation: tests OK. Commit ab0da7d.",
            },
            {
                "id": 11,
                "date_creation": 200,
                "comment": "Final closeout. Validation: tests OK. Commit 180e868.",
            },
        ]
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "_ticket_comments", return_value=comments), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "column_name", return_value="Done"), \
             mock.patch.object(self.cli, "_git_worktree_info", return_value={"available": True, "clean": True, "head": "9999999"}):
            report = self.cli._closeout_report({"project_id": 1}, 49, strict=True, require_commit=True)

        self.assertTrue(report["ok"])
        commit = [c for c in report["checks"] if c["name"] == "commit_id"][0]
        self.assertEqual("pass", commit["status"])
        self.assertIn("180e868", commit["detail"])
        self.assertIn("comment #11", commit["detail"])
        self.assertNotIn("ab0da7d", commit["detail"])

    def test_notify_hook_codex_stop_mode_is_silent_for_new_tickets(self):
        home = pathlib.Path(self.tmpdir.name) / "home"
        cli_dir = home / ".local" / "bin"
        cli_dir.mkdir(parents=True)
        fake_cli = cli_dir / "agent-ticket"
        fake_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if sys.argv[1:4] == ['list', '--project', 'agent-tickets'] and sys.argv[4:] == ['--json']:\n"
            "    print(json.dumps([{'id': 72, 'title': 'Hook JSON', 'tags': ['project:agent-tickets'], 'url': 'u'}]))\n"
            "elif sys.argv[1:4] == ['callbacks', '--pending', '--repo']:\n"
            "    print('Pending callback text')\n"
        )
        fake_cli.chmod(0o755)
        work = pathlib.Path(self.tmpdir.name) / "agent-tickets"
        work.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "XDG_CACHE_HOME": str(pathlib.Path(self.tmpdir.name) / "cache"),
        }
        hook = ROOT / "scripts" / "notify-hook.sh"

        baseline = subprocess.run(
            [str(hook), "baseline"],
            cwd=str(work),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Open agent-ticket(s)", baseline.stdout)

        codex_stop = subprocess.run(
            [str(hook), "codex-stop-changes"],
            cwd=str(work),
            env={**env, "XDG_CACHE_HOME": str(pathlib.Path(self.tmpdir.name) / "cache-codex")},
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual("", codex_stop.stdout)

    def test_notify_hook_baseline_does_not_reopen_stdout_path(self):
        home = pathlib.Path(self.tmpdir.name) / "home"
        cli_dir = home / ".local" / "bin"
        cli_dir.mkdir(parents=True)
        fake_cli = cli_dir / "agent-ticket"
        fake_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if sys.argv[1:4] == ['list', '--project', 'agent-tickets'] and sys.argv[4:] == ['--json']:\n"
            "    print(json.dumps([{'id': 125, 'title': 'Hook stdout', 'tags': ['project:agent-tickets'], 'url': 'u'}]))\n"
            "elif sys.argv[1:4] == ['callbacks', '--pending', '--repo']:\n"
            "    pass\n"
        )
        fake_cli.chmod(0o755)
        work = pathlib.Path(self.tmpdir.name) / "agent-tickets"
        work.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "XDG_CACHE_HOME": str(pathlib.Path(self.tmpdir.name) / "cache-socket-stdout"),
        }
        hook = ROOT / "scripts" / "notify-hook.sh"
        parent_sock, child_sock = socket.socketpair()
        try:
            proc = subprocess.run(
                [str(hook), "baseline"],
                cwd=str(work),
                env=env,
                stdout=child_sock,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
                timeout=10,
            )
        finally:
            child_sock.close()

        chunks = []
        parent_sock.settimeout(1)
        try:
            while True:
                chunk = parent_sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        finally:
            parent_sock.close()

        stdout = b"".join(chunks).decode("utf-8")
        self.assertEqual("", proc.stderr)
        self.assertIn("Open agent-ticket(s)", stdout)
        self.assertIn("#125", stdout)

    def test_register_hooks_replaces_old_codex_stop_notify_command(self):
        home = pathlib.Path(self.tmpdir.name) / "home"
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True)
        hook = pathlib.Path(self.tmpdir.name) / "notify-hook.sh"
        hook.write_text("#!/bin/sh\n")
        hooks_path = codex_dir / "hooks.json"
        hooks_path.write_text(json.dumps({
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "%s changes" % hook, "timeout": 10}]},
                    {"hooks": [{"type": "command", "command": "python3 rewind.py", "timeout": 300}]},
                ]
            }
        }, indent=2))

        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "register-hooks.py"), str(hook)],
            env={**os.environ, "HOME": str(home)},
            text=True,
            capture_output=True,
            check=True,
        )

        data = json.loads(hooks_path.read_text())
        stop_commands = [
            h["command"]
            for group in data["hooks"]["Stop"]
            for h in group.get("hooks", [])
        ]
        self.assertIn("%s codex-stop-changes" % hook, stop_commands)
        self.assertIn("python3 rewind.py", stop_commands)
        self.assertNotIn("%s changes" % hook, stop_commands)

    def test_register_hooks_skips_claude_when_provider_home_absent(self):
        home = pathlib.Path(self.tmpdir.name) / "fresh-home"
        hook = pathlib.Path(self.tmpdir.name) / "notify-hook.sh"
        hook.write_text("#!/bin/sh\n")

        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "register-hooks.py"), str(hook)],
            env={**os.environ, "HOME": str(home)},
            text=True,
            capture_output=True,
            check=True,
        )

        settings_path = home / ".claude" / "settings.json"
        self.assertFalse(settings_path.exists())
        self.assertIn("claude: ~/.claude not present", result.stdout)

    def test_register_hooks_creates_claude_settings_when_provider_home_exists(self):
        home = pathlib.Path(self.tmpdir.name) / "home-with-claude"
        (home / ".claude").mkdir(parents=True)
        hook = pathlib.Path(self.tmpdir.name) / "notify-hook.sh"
        hook.write_text("#!/bin/sh\n")

        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "register-hooks.py"), str(hook)],
            env={**os.environ, "HOME": str(home)},
            text=True,
            capture_output=True,
            check=True,
        )

        settings_path = home / ".claude" / "settings.json"
        self.assertTrue(settings_path.exists())
        data = json.loads(settings_path.read_text())
        commands = [
            h["command"]
            for event in ("SessionStart", "UserPromptSubmit")
            for group in data["hooks"].get(event, [])
            for h in group.get("hooks", [])
        ]
        self.assertIn("%s baseline" % hook, commands)
        self.assertIn("%s changes" % hook, commands)

    def test_supervise_dry_run_launches_fresh_without_trusting_stale_latest(self):
        task = {"id": 47, "title": "Fix demo", "column_id": 1, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed %s pane found for /tmp/demo" % provider},
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-latest":
                self.fail("single-ticket supervise must not consult codex-latest")
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "rc": 1,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("dry-run should not launch")

        args = argparse.Namespace(
            id=47, provider=None, session=None, session_prefix="owner", full_permission=True,
            message="", dry_run=True, no_tool_ticket=False, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("codex", result["route"]["provider"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertEqual("owner-demo-47", result["route"]["session"])
        self.assertEqual("skipped", result["route"]["resume_latest"]["status"])
        self.assertIn("fresh", result["route"]["resume_latest"]["reason"])
        self.assertNotIn("codex-resume-latest", [call[0] for call in tmux_calls])

    def test_supervise_dry_run_blocks_overlapping_active_supervision_claim(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(ticket_ids=[47], repo="/tmp/demo"),
        })
        task = {"id": 47, "title": "Fix demo", "column_id": 1, "swimlane_id": 1, "is_active": 1}
        args = argparse.Namespace(
            id=47, provider=None, session=None, session_prefix="owner", full_permission=True,
            message="", dry_run=True, no_tool_ticket=False, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("already supervised", result["reason"])
        active = result["active_supervision"][0]
        self.assertEqual("other-supervisor", active["owner_id"])
        self.assertEqual([47], active["ticket_ids"])
        self.assertFalse(active["owned_by_current"])
        self.assertGreaterEqual(active["age_seconds"], 0)
        self.assertGreater(active["expires_in_seconds"], 0)
        contact.assert_not_called()
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_defer_same_owner_same_repo_claim_for_different_ticket(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                claim_id="claim-demo",
                owner_id="current-supervisor",
                ticket_ids=[203],
                repo="/tmp/demo",
                worker_session="owner-demo-203",
            ),
        })
        task = {"id": 204, "title": "Second demo ticket", "column_id": 1, "swimlane_id": 1, "is_active": 1}
        args = argparse.Namespace(
            id=204, provider=None, session=None, session_prefix="owner", full_permission=True,
            message="", dry_run=False, no_tool_ticket=False, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("deferred", result["status"])
        self.assertEqual("repo already supervised", result["reason"])
        self.assertEqual([203], result["active_supervision"][0]["ticket_ids"])
        self.assertTrue(result["active_supervision"][0]["owned_by_current"])
        self.assertIn("owner-demo-203", result["detail"])
        contact.assert_not_called()
        tmux.assert_not_called()
        audit.assert_called_once()

    def test_supervise_dry_run_does_not_comment_when_same_repo_claim_deferred(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                claim_id="claim-demo",
                owner_id="current-supervisor",
                ticket_ids=[203],
                repo="/tmp/demo",
                worker_session="owner-demo-203",
            ),
        })
        task = {"id": 204, "title": "Second demo ticket", "column_id": 1, "swimlane_id": 1, "is_active": 1}
        args = argparse.Namespace(
            id=204, provider=None, session=None, session_prefix="owner", full_permission=True,
            message="", dry_run=True, no_tool_ticket=False, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("deferred", result["status"])
        contact.assert_not_called()
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_acquire_defer_same_repo_claim_created_after_preflight(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                claim_id="claim-demo",
                owner_id="current-supervisor",
                ticket_ids=[203],
                repo="/tmp/demo",
                worker_session="owner-demo-203",
            ),
        })
        task = {"id": 204, "title": "Second demo ticket", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        args = argparse.Namespace(
            id=204, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=False, no_tool_ticket=False, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        empty_preflight = {
            "owner_id": "current-supervisor",
            "blocked": False,
            "active_claims": [],
            "conflicting_claims": [],
            "owned_claims": [],
            "stale_claims": [],
            "override": False,
        }
        contactable = [{"provider": "codex", "session": "owner-demo-204", "probe": {"ok": True}}]
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_supervision_preflight", return_value=empty_preflight), \
             mock.patch.object(self.cli, "_contactable_providers", return_value=(contactable, [])), \
             mock.patch.object(self.cli, "_agent_contact") as send, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("deferred", result["status"])
        self.assertEqual("repo already supervised", result["reason"])
        self.assertEqual([203], result["active_supervision"][0]["ticket_ids"])
        send.assert_not_called()
        tmux.assert_not_called()
        audit.assert_called_once()

    def test_supervise_after_claim_release_launches_fresh_not_latest(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                claim_id="claim-demo", owner_id="current-supervisor", ticket_ids=[47], repo="/tmp/demo"),
        })
        release_args = argparse.Namespace(
            action="release", claim="claim-demo", repo=None, ticket=None, all=False,
            active_only=False, supervisor_id="current-supervisor", force=False,
            supervision_ttl_hours=1, origin_repo=None, origin_provider=None, origin_session=None,
            json=True,
        )
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.cli.cmd_supervision(None, release_args)

        task = {"id": 47, "title": "Fix demo", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed %s pane found for /tmp/demo" % provider},
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "rc": 1,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "argv": ["agent-tmux"] + argv,
                }
            if argv[0] == "codex-latest":
                self.fail("single-ticket supervise must not consult codex-latest after claim release")
            self.fail("dry-run should not launch")

        args = argparse.Namespace(
            id=47, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=False, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertEqual("owner-demo-47", result["route"]["session"])
        self.assertEqual([["codex-existing", "/tmp/demo", "owner-demo-47"]], tmux_calls)
        audit.assert_not_called()

    def test_supervise_launch_uses_fresh_ticket_scoped_session_not_resume_latest(self):
        task = {"id": 47, "title": "Fix demo", "column_id": 1, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed %s pane found for /tmp/demo" % provider},
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-latest":
                self.fail("single-ticket supervise must not consult codex-latest")
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "rc": 1,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "argv": ["agent-tmux"] + argv,
                }
            if argv[0] == "codex" and argv[1] == "owner-demo-47":
                return {"ok": True, "rc": 0, "stdout": "launched", "stderr": "", "argv": ["agent-tmux"] + argv}
            return {"ok": False, "rc": 2, "stdout": "", "stderr": "requested session already exists", "argv": ["agent-tmux"] + argv}

        args = argparse.Namespace(
            id=47, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=False, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment"), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("routed", result["status"])
        self.assertEqual("owner-demo-47", result["route"]["session"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertEqual("codex", tmux_calls[-1][0])
        self.assertEqual("owner-demo-47", tmux_calls[-1][1])
        self.assertNotIn("codex-resume-latest", [call[0] for call in tmux_calls])

    def test_supervise_blocks_unsafe_contact_refusal_before_fresh_launch(self):
        task = {"id": 49, "title": "Fix unsafe", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            if provider == "codex":
                return {
                    "ok": False,
                    "rc": 3,
                    "json": {"reason": "pane busy", "session": "review-demo", "pane_state": "agent_working"},
                    "stderr": "",
                    "raw": "",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed claude pane found for /tmp/demo"},
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            return {"ok": True, "rc": 0, "stdout": "unexpected", "stderr": "", "argv": ["agent-tmux"] + argv}

        args = argparse.Namespace(
            id=49, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("unsafe provider refusal", result["reason"])
        self.assertIn("review-demo", result["detail"])
        self.assertEqual([], tmux_calls)
        audit.assert_not_called()

    def test_supervise_blocks_unsafe_refusal_even_when_other_provider_contactable(self):
        task = {"id": 50, "title": "Fix unsafe mixed", "column_id": 2, "swimlane_id": 1, "is_active": 1}

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            if provider == "codex":
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {"status": "would_send", "session": "safe-codex-demo"},
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "pane busy", "session": "busy-claude-demo", "pane_state": "agent_working"},
                "stderr": "",
                "raw": "",
            }

        args = argparse.Namespace(
            id=50, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("unsafe provider refusal", result["reason"])
        self.assertIn("busy-claude-demo", result["detail"])
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_blocks_unbound_contactable_stale_review_lane(self):
        task = {"id": 51, "title": "Fix stale contact", "column_id": 2, "swimlane_id": 1, "is_active": 1}

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            if provider == "codex":
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {"status": "would_send", "session": "review-demo-51"},
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed claude pane found for /tmp/demo"},
                "stderr": "",
                "raw": "",
            }

        args = argparse.Namespace(
            id=51, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("unbound contactable session", result["reason"])
        self.assertIn("review-demo-51", result["detail"])
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_rejects_same_session_wrong_provider_claim_binding(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                claim_id="claim-demo", owner_id="current-supervisor", ticket_ids=[54], repo="/tmp/demo",
                worker_provider="codex", worker_session="shared-name"),
        })
        task = {"id": 54, "title": "Fix provider binding", "column_id": 2, "swimlane_id": 1, "is_active": 1}

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            if provider == "claude":
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {"status": "would_send", "session": "shared-name"},
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed codex pane found for /tmp/demo"},
                "stderr": "",
                "raw": "",
            }

        args = argparse.Namespace(
            id=54, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("unbound contactable session", result["reason"])
        self.assertIn("claude session shared-name", result["detail"])
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_exact_session_send_preserves_session_when_probe_omits_json_session(self):
        task = {"id": 52, "title": "Fix exact session", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append((provider, dry_run, session))
            if provider == "codex" and session == "owner-demo-52":
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {"status": "would_send" if dry_run else "sent"},
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed %s pane found for /tmp/demo" % provider},
                "stderr": "",
                "raw": "",
            }

        args = argparse.Namespace(
            id=52, provider="codex", session="owner-demo-52", session_prefix="owner", full_permission=False,
            message="", dry_run=False, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment"), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("contact", result["route"]["mode"])
        self.assertEqual("owner-demo-52", result["route"]["session"])
        self.assertIn(("codex", True, "owner-demo-52"), contact_calls)
        self.assertIn(("codex", False, "owner-demo-52"), contact_calls)
        tmux.assert_not_called()

    def test_supervise_exact_session_absence_launches_without_unsafe_refusal(self):
        task = {"id": 53, "title": "Fix absent exact session", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {
                "ok": False,
                "rc": 3,
                "json": {
                    "reason": "no tmux-managed %s pane found for /tmp/demo in session 'owner-demo-53'" % provider
                },
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "rc": 1,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo; session: owner-demo-53",
                    "argv": ["agent-tmux"] + argv,
                }
            if argv[0] == "codex-latest":
                self.fail("single-ticket supervise must not consult codex-latest")
            self.fail("dry-run should not launch")

        args = argparse.Namespace(
            id=53, provider="codex", session="owner-demo-53", session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertEqual("owner-demo-53", result["route"]["session"])
        self.assertEqual([["codex-existing", "/tmp/demo", "owner-demo-53"]], tmux_calls)
        audit.assert_not_called()

    def test_supervise_closed_ticket_skips_without_contact_or_blocker(self):
        task = {"id": 54, "title": "Already closed", "column_id": 5, "swimlane_id": 1, "is_active": 0}
        args = argparse.Namespace(
            id=54, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=False, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Done"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual("already done", result["status"])
        self.assertEqual("Done", result["column"])
        contact.assert_not_called()
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_inactive_ticket_in_stale_live_column_skips_without_contact(self):
        task = {"id": 57, "title": "Closed but stale column", "column_id": 3, "swimlane_id": 1, "is_active": 0}
        args = argparse.Namespace(
            id=57, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=False, no_tool_ticket=False, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Agent working"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertNotIn("dry_run", result)
        self.assertEqual("already done", result["status"])
        self.assertEqual("Agent working", result["column"])
        contact.assert_not_called()
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_explicit_missing_tmux_window_launches_without_unsafe_refusal(self):
        task = {"id": 55, "title": "Fix absent exact tmux window", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {
                "ok": False,
                "rc": 3,
                "json": {
                    "reason": "tmux pane discovery failed: can't find window: owner-demo-55"
                },
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "rc": 1,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo; session: owner-demo-55",
                    "argv": ["agent-tmux"] + argv,
                }
            if argv[0] == "codex-latest":
                self.fail("single-ticket supervise must not consult codex-latest")
            self.fail("dry-run should not launch")

        args = argparse.Namespace(
            id=55, provider="codex", session="owner-demo-55", session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertEqual("owner-demo-55", result["route"]["session"])
        self.assertEqual([["codex-existing", "/tmp/demo", "owner-demo-55"]], tmux_calls)
        audit.assert_not_called()

    def test_supervise_missing_tmux_server_launches_without_unsafe_refusal(self):
        task = {"id": 56, "title": "Fix absent tmux server", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []
        missing_socket = "tmux pane discovery failed: error connecting to /tmp/tmux-1000/default (No such file or directory)"

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": missing_socket},
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "rc": 1,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo; session: owner-demo-56",
                    "argv": ["agent-tmux"] + argv,
                }
            if argv[0] == "codex-latest":
                self.fail("single-ticket supervise must not consult codex-latest")
            self.fail("dry-run should not launch")

        args = argparse.Namespace(
            id=56, provider=None, session=None, session_prefix="owner", full_permission=True,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertEqual("owner-demo-56", result["route"]["session"])
        self.assertEqual([["codex-existing", "/tmp/demo", "owner-demo-56"]], tmux_calls)
        self.assertEqual(2, len(result["refused"]))
        audit.assert_not_called()

    def test_supervise_reuses_existing_ticket_session_through_guarded_contact(self):
        task = {"id": 58, "title": "Fix demo", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append((provider, dry_run, session))
            if provider == "codex" and session == "owner-demo-58":
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {"status": "would_send" if dry_run else "sent", "session": "owner-demo-58"},
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed %s pane found for /tmp/demo" % provider},
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-latest":
                self.fail("single-ticket supervise must not consult codex-latest")
            if argv[0] == "codex-existing":
                if len(argv) == 3 and argv[2] == "owner-demo-58":
                    return {"ok": True, "rc": 0, "stdout": "owner-demo-58", "stderr": "", "argv": ["agent-tmux"] + argv}
                return {"ok": False, "rc": 2, "stdout": "", "stderr": "multiple detached Codex tmux sessions", "argv": ["agent-tmux"] + argv}
            self.fail("existing ticket session should be contacted, not launched")

        args = argparse.Namespace(
            id=58, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=False, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Agent working"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment"), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("routed", result["status"])
        self.assertEqual("contact", result["route"]["mode"])
        self.assertEqual("owner-demo-58", result["route"]["session"])
        self.assertIn(("codex", True, "owner-demo-58"), contact_calls)
        self.assertIn(("codex", False, "owner-demo-58"), contact_calls)
        self.assertNotIn("codex-resume-latest", [call[0] for call in tmux_calls])

    def test_supervise_accepts_default_ticket_session(self):
        task = {"id": 59, "title": "Fix default ticket session", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append((provider, dry_run, session))
            if provider == "codex" and session == "owner-demo-59":
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {"status": "would_send", "session": "owner-demo-59"},
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed %s pane found for /tmp/demo" % provider},
                "stderr": "",
                "raw": "",
            }

        args = argparse.Namespace(
            id=59, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("contact", result["route"]["mode"])
        self.assertEqual("owner-demo-59", result["route"]["session"])
        self.assertIn(("codex", True, "owner-demo-59"), contact_calls)
        self.assertNotIn(("codex", True, None), contact_calls)
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_default_ticket_session_ignores_untrusted_unrelated_repo_panes_when_exact_session_absent(self):
        task = {"id": 98, "title": "Fix Rewind", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        contact_calls = []
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append((provider, dry_run, session))
            if session is None:
                return {
                    "ok": False,
                    "rc": 3,
                    "json": {
                        "reason": (
                            "candidate codex pane found for /tmp/Rewind, but provider root or "
                            "launcher root is not trusted; session=owner-Rewind pane_id=%36"
                        )
                    },
                    "stderr": "",
                    "raw": "",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {
                    "reason": "no tmux-managed %s pane found for /tmp/Rewind in session 'owner-Rewind-98'" % provider
                },
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "rc": 1,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/Rewind; session: owner-Rewind-98",
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("dry-run should not launch")

        args = argparse.Namespace(
            id=98, provider=None, session=None, session_prefix="owner", full_permission=True,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:Rewind"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("Rewind", "/tmp/Rewind")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertEqual("owner-Rewind-98", result["route"]["session"])
        self.assertEqual([("codex", True, "owner-Rewind-98"), ("claude", True, "owner-Rewind-98")], contact_calls)
        self.assertEqual([["codex-existing", "/tmp/Rewind", "owner-Rewind-98"]], tmux_calls)
        audit.assert_not_called()

    def test_supervise_blocks_existing_ticket_session_when_guarded_contact_refuses(self):
        task = {"id": 58, "title": "Fix demo", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            reason = (
                "no tmux-managed codex pane found for /tmp/demo in session 'owner-demo-58'"
                if session else
                "no tmux-managed %s pane found for /tmp/demo" % provider
            )
            return {"ok": False, "rc": 3, "json": {"reason": reason}, "stderr": "", "raw": ""}

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-latest":
                self.fail("single-ticket supervise must not consult codex-latest")
            if argv[0] == "codex-existing":
                if len(argv) == 3 and argv[2] == "owner-demo-58":
                    return {"ok": True, "rc": 0, "stdout": "owner-demo-58", "stderr": "", "argv": ["agent-tmux"] + argv}
                return {"ok": False, "rc": 2, "stdout": "", "stderr": "multiple detached Codex tmux sessions", "argv": ["agent-tmux"] + argv}
            self.fail("unsafe existing ticket session should block before launch")

        args = argparse.Namespace(
            id=58, provider=None, session=None, session_prefix="owner", full_permission=False,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Agent working"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("existing session not contactable", result["reason"])
        self.assertEqual("owner-demo-58", result["route"]["session"])
        self.assertEqual("owner-demo-58", result["existing_ticket_session"]["stdout"])
        self.assertIn("owner-demo-58", result["detail"])
        self.assertNotIn("codex-resume-latest", [call[0] for call in tmux_calls])
        audit.assert_not_called()

    def test_supervise_dry_run_accepts_exact_session_would_send_idle_prompt(self):
        task = {"id": 70, "title": "Fix routing mismatch", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append((provider, dry_run, session))
            if provider == "codex" and session == "owner-agent-tickets":
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {
                        "status": "would_send",
                        "session": "owner-agent-tickets",
                        "pane_state": "idle_empty_prompt",
                    },
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed %s pane found for /tmp/demo in session 'owner-agent-tickets'" % provider},
                "stderr": "",
                "raw": "",
            }

        args = argparse.Namespace(
            id=70, provider="codex", session="owner-agent-tickets", session_prefix="owner",
            full_permission=True, message="", dry_run=True, no_tool_ticket=True,
            poll_interval=0, max_polls=0, strict_closeout=False, require_clean=False,
            require_validation=False, require_commit=False, require_install=False, json=True,
            watch_origin=False, supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual("contact", result["route"]["mode"])
        self.assertEqual("owner-agent-tickets", result["route"]["session"])
        self.assertEqual("codex", result["route"]["provider"])
        self.assertTrue(all(call[1] for call in contact_calls))
        tmux.assert_not_called()
        audit.assert_not_called()

    def test_supervise_live_sends_to_would_send_idle_prompt_session(self):
        task = {"id": 70, "title": "Fix routing mismatch", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append((provider, dry_run, session))
            if provider == "codex" and session == "owner-agent-tickets":
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {
                        "status": "would_send" if dry_run else "sent",
                        "session": "owner-agent-tickets",
                        "pane_state": "idle_empty_prompt",
                    },
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "no tmux-managed %s pane found for /tmp/demo in session 'owner-agent-tickets'" % provider},
                "stderr": "",
                "raw": "",
            }

        args = argparse.Namespace(
            id=70, provider="codex", session="owner-agent-tickets", session_prefix="owner",
            full_permission=False, message="", dry_run=False, no_tool_ticket=True,
            poll_interval=0, max_polls=0, strict_closeout=False, require_clean=False,
            require_validation=False, require_commit=False, require_install=False, json=True,
            watch_origin=False, supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("routed", result["status"])
        self.assertEqual("polling_stopped", result["result"])
        self.assertEqual("limit_reached", result["polling"]["status"])
        self.assertEqual("worker still running", result["detail"].split("; ", 1)[1])
        self.assertEqual("contact", result["route"]["mode"])
        self.assertEqual("owner-agent-tickets", result["route"]["session"])
        self.assertIn(("codex", True, "owner-agent-tickets"), contact_calls)
        self.assertIn(("codex", False, "owner-agent-tickets"), contact_calls)
        tmux.assert_not_called()
        audit.assert_called_once()

    def test_supervise_contact_uses_compact_message_for_claude_live_send(self):
        task = {
            "id": 115,
            "title": "Compact ComfyCommander router description while preserving deferred references",
            "column_id": 2,
            "swimlane_id": 1,
            "is_active": 1,
        }
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append((provider, dry_run, session, message))
            if provider == "claude" and session == "owner-demo-115-claude":
                if dry_run:
                    return {
                        "ok": True,
                        "rc": 0,
                        "json": {
                            "status": "would_send",
                            "session": "owner-demo-115-claude",
                            "pane_state": "idle_empty_prompt",
                        },
                        "stderr": "",
                        "raw": "{}",
                    }
                if len(message) > 420:
                    return {
                        "ok": False,
                        "rc": 2,
                        "json": {
                            "status": "mutated_unsubmitted",
                            "stage": "submit",
                            "reason": (
                                "pre-submit revalidation failed after paste; target composer may contain "
                                "an unsubmitted message: full guarded contact line or exact Codex "
                                "pasted-content placeholder is not the current composer prompt body"
                            ),
                            "session": "owner-demo-115-claude",
                            "pane_state": "pending_user_text",
                        },
                        "stderr": "",
                        "raw": "{}",
                    }
                return {
                    "ok": True,
                    "rc": 0,
                    "json": {"status": "sent", "session": "owner-demo-115-claude"},
                    "stderr": "",
                    "raw": "{}",
                }
            return {
                "ok": False,
                "rc": 3,
                "json": {
                    "reason": "no tmux-managed %s pane found for /tmp/demo in session 'owner-demo-115-claude'" % provider
                },
                "stderr": "",
                "raw": "",
            }

        args = argparse.Namespace(
            id=115, provider="claude", session="owner-demo-115-claude", session_prefix="owner",
            full_permission=False, message="", dry_run=False, no_tool_ticket=True,
            poll_interval=0, max_polls=0, strict_closeout=False, require_clean=False,
            require_validation=False, require_commit=False, require_install=False, json=True,
            watch_origin=False, supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("routed", result["status"])
        claude_messages = [call[3] for call in contact_calls if call[0] == "claude"]
        self.assertEqual(2, len(claude_messages))
        self.assertTrue(all(len(message) <= 420 for message in claude_messages))
        self.assertTrue(all("agent-ticket show 115" in message for message in claude_messages))
        self.assertTrue(all("Closeout gate before" not in message for message in claude_messages))
        self.assertEqual("contact", result["route"]["mode"])
        self.assertEqual("claude", result["route"]["provider"])
        self.assertEqual("owner-demo-115-claude", result["route"]["session"])
        tmux.assert_not_called()
        audit.assert_called_once()

    def test_register_origin_watcher_persists_metadata_and_audit_comment(self):
        args = argparse.Namespace(
            watch_origin=True,
            origin_repo="/tmp/origin-repo",
            origin_provider="codex",
            origin_session="origin-session",
            watch_expires_hours=2,
            callback_reason="owner closeout",
            correlation_id="corr-36",
        )

        with mock.patch.object(self.cli, "audit_comment") as audit:
            watcher = self.cli._register_origin_watcher({"comment_user_id": 1}, 36, args, "supervise")

        self.assertEqual(36, watcher["ticket_id"])
        self.assertEqual("/tmp/origin-repo", watcher["origin_repo"])
        self.assertEqual("codex", watcher["origin_provider"])
        self.assertEqual("origin-session", watcher["origin_session"])
        self.assertEqual("corr-36", watcher["correlation_id"])
        store = json.loads(pathlib.Path(self.cli.WATCHERS_PATH).read_text())
        self.assertEqual("corr-36", store["watchers"]["36"]["correlation_id"])
        audit.assert_called_once()
        self.assertIn("registered origin watcher", audit.call_args.args[2])

    def test_ticket_closed_outbox_is_durable_before_guarded_callback_send(self):
        watcher = {
            "ticket_id": 50,
            "origin_repo": "/tmp/origin-repo",
            "origin_provider": "codex",
            "origin_session": "origin-session",
            "created_at": "2026-05-12T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "expires_at_epoch": 4070908800.0,
            "callback_reason": "owner closeout",
            "correlation_id": "corr-50",
            "source_command": "supervise",
            "close_revision": 0,
            "last_closed_state": False,
            "delivered_events": [],
        }
        self.cli._save_watcher_store({"version": 1, "watchers": {"50": watcher}})
        calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            event = self.cli._read_event_record("ticket:50:closed:1")
            calls.append(("dry-run" if dry_run else "send", event is not None))
            self.assertIsNotNone(event)
            return {"ok": True, "rc": 0, "json": {"session": "origin-session"}, "stderr": "", "raw": "{}"}

        with mock.patch.object(self.cli, "_closeout_report", return_value={"ok": True, "project": "demo", "repo": "/tmp/demo", "checks": []}), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "audit_comment"):
            records = self.reserve_then_notify({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 50)

        self.assertEqual([("dry-run", True), ("send", True)], calls)
        self.assertEqual("delivered", records[0]["status"])
        self.assertEqual("ticket:50:closed:1", records[0]["event_key"])

    def test_ticket_closed_callback_is_idempotent_until_reopen(self):
        watcher = {
            "ticket_id": 51,
            "origin_repo": "/tmp/origin-repo",
            "origin_provider": "codex",
            "origin_session": "origin-session",
            "created_at": "2026-05-12T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "expires_at_epoch": 4070908800.0,
            "callback_reason": "owner closeout",
            "correlation_id": "corr-51",
            "source_command": "supervise",
            "close_revision": 0,
            "last_closed_state": False,
            "delivered_events": [],
        }
        self.cli._save_watcher_store({"version": 1, "watchers": {"51": watcher}})
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append("dry-run" if dry_run else "send")
            return {"ok": True, "rc": 0, "json": {"session": "origin-session"}, "stderr": "", "raw": "{}"}

        with mock.patch.object(self.cli, "_closeout_report", return_value={"ok": True, "project": "demo", "repo": "/tmp/demo", "checks": []}), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "audit_comment"):
            first = self.reserve_then_notify({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 51)[0]
            second = self.cli._notify_ticket_closed({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 51)[0]
            self.cli._mark_ticket_reopened_for_callbacks(51)
            third = self.reserve_then_notify({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 51)[0]

        self.assertEqual("ticket:51:closed:1", first["event_key"])
        self.assertEqual("ticket:51:closed:1", second["event_key"])
        self.assertEqual("ticket:51:closed:2", third["event_key"])
        self.assertEqual(["dry-run", "send", "dry-run", "send"], contact_calls)

    def test_callback_pending_when_agent_contact_dry_run_refuses(self):
        watcher = {
            "ticket_id": 52,
            "origin_repo": "/tmp/origin-repo",
            "origin_provider": "codex",
            "origin_session": "origin-session",
            "created_at": "2026-05-12T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "expires_at_epoch": 4070908800.0,
            "callback_reason": "owner closeout",
            "correlation_id": "corr-52",
            "source_command": "supervise",
            "close_revision": 0,
            "last_closed_state": False,
            "delivered_events": [],
        }
        self.cli._save_watcher_store({"version": 1, "watchers": {"52": watcher}})
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append(dry_run)
            self.assertTrue(dry_run)
            return {"ok": False, "rc": 3, "json": {"reason": "pane busy"}, "stderr": "", "raw": ""}

        with mock.patch.object(self.cli, "_closeout_report", return_value={"ok": True, "project": "demo", "repo": "/tmp/demo", "checks": []}), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "audit_comment") as audit:
            record = self.reserve_then_notify({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 52)[0]

        self.assertEqual([True], contact_calls)
        self.assertEqual("pending", record["status"])
        self.assertIn("dry-run refused", record["pending_reason"])
        self.assertEqual("dry-run", record["delivery_attempts"][0]["stage"])
        self.assertIn("pending ticket.closed callback", audit.call_args.args[2])

    def test_callbacks_pending_filters_by_origin_repo(self):
        record = {
            "event": "ticket.closed",
            "event_key": "ticket:53:closed:1",
            "ticket_id": 53,
            "revision": 1,
            "status": "pending",
            "pending_reason": "origin provider missing",
            "created_at": "2026-05-12T00:00:00Z",
            "ticket_url": "http://kanboard.invalid/task/53",
            "watcher": {
                "origin_repo": "/tmp/agent-tickets",
                "origin_provider": None,
                "origin_session": None,
                "correlation_id": "corr-53",
            },
        }
        self.cli._write_event_record(record)
        out = io.StringIO()
        with mock.patch("sys.stdout", new=out):
            self.cli.cmd_callbacks(None, argparse.Namespace(pending=True, repo="agent-tickets", json=True))

        callbacks = json.loads(out.getvalue())
        self.assertEqual(1, len(callbacks))
        self.assertEqual("ticket:53:closed:1", callbacks[0]["event_key"])

    def test_callback_ack_removes_record_from_pending_hook_surface(self):
        record = {
            "event": "ticket.closed",
            "event_key": "ticket:54:closed:1",
            "ticket_id": 54,
            "revision": 1,
            "status": "pending",
            "pending_reason": "dry-run refused",
            "created_at": "2026-05-12T00:00:00Z",
            "ticket_url": "http://kanboard.invalid/task/54",
            "watcher": {"origin_repo": "/tmp/agent-tickets", "correlation_id": "corr-54"},
        }
        self.cli._write_event_record(record)

        with mock.patch("sys.stdout", new=io.StringIO()):
            self.cli.cmd_callbacks(None, argparse.Namespace(action="ack", event_key="ticket:54:closed:1",
                                                            pending=False, repo=None, json=False))
        out = io.StringIO()
        with mock.patch("sys.stdout", new=out):
            self.cli.cmd_callbacks(None, argparse.Namespace(action="list", event_key=None,
                                                            pending=True, repo="agent-tickets", json=True))

        self.assertEqual([], json.loads(out.getvalue()))
        acked = self.cli._read_event_record("ticket:54:closed:1")
        self.assertEqual("acknowledged", acked["status"])
        self.assertIn("acknowledged_at", acked)

    def test_callback_without_origin_provider_probes_both_and_sends_one_safe_target(self):
        watcher = {
            "ticket_id": 55,
            "origin_repo": "/tmp/origin-repo",
            "origin_provider": None,
            "origin_session": None,
            "created_at": "2026-05-12T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "expires_at_epoch": 4070908800.0,
            "callback_reason": "owner closeout",
            "correlation_id": "corr-55",
            "source_command": "supervise",
            "close_revision": 0,
            "last_closed_state": False,
            "delivered_events": [],
        }
        self.cli._save_watcher_store({"version": 1, "watchers": {"55": watcher}})
        contact_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            contact_calls.append((provider, dry_run, session))
            if dry_run and provider == "codex":
                return {"ok": False, "rc": 3, "json": {"reason": "no pane"}, "stderr": "", "raw": ""}
            return {"ok": True, "rc": 0, "json": {"session": "claude-safe"}, "stderr": "", "raw": "{}"}

        with mock.patch.object(self.cli, "_closeout_report", return_value={"ok": True, "project": "demo", "repo": "/tmp/demo", "checks": []}), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "audit_comment"):
            record = self.reserve_then_notify({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 55)[0]

        self.assertEqual([("codex", True, None), ("claude", True, None), ("claude", False, "claude-safe")], contact_calls)
        self.assertEqual("delivered", record["status"])
        self.assertEqual("claude", record["delivered_provider"])

    def test_close_preflights_callback_state_before_kanboard_mutation(self):
        pathlib.Path(self.cli.CALLBACK_DIR).mkdir(parents=True, exist_ok=True)
        pathlib.Path(self.cli.WATCHERS_PATH).write_text("{not json")
        task = {"id": 56, "column_id": 3, "swimlane_id": 1, "is_active": 1}
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "rpc") as rpc_mock:
            with self.assertRaises(SystemExit) as caught:
                self.cli.cmd_close({"project_id": 1}, argparse.Namespace(id=56, json=False))

        self.assertIn("callback watcher store", str(caught.exception))
        rpc_mock.assert_not_called()

    def test_new_watch_origin_requires_dispatch(self):
        args = argparse.Namespace(
            project="demo", force=True, watch_origin=True, dispatch=False, tag=None, agent=None,
            severity=None, title="demo", column="New", body="", kind=None, json=False,
        )
        with self.assertRaises(SystemExit) as caught:
            self.cli.cmd_new({"project_id": 1}, args)

        self.assertIn("requires --dispatch", str(caught.exception))

    def test_new_without_dispatch_prompts_agent_to_ask_user_about_guarded_routing(self):
        args = argparse.Namespace(
            project="demo", force=True, watch_origin=False, dispatch=False, tag=None, agent="codex",
            severity="p2", title="Demo issue", column="New", body="body", kind="bug", json=False,
        )
        task = {"id": 70, "title": "Demo issue", "column_id": 1, "swimlane_id": 1, "is_active": 1}

        def fake_rpc(cfg, method, params=None):
            if method == "createTask":
                return 70
            if method == "getTask":
                return task
            self.fail("unexpected rpc call %s" % method)

        out = io.StringIO()
        with mock.patch.object(self.cli, "resolve_column_id", return_value=1), \
             mock.patch.object(self.cli, "resolve_category_id", return_value=2), \
             mock.patch.object(self.cli, "rpc", side_effect=fake_rpc), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "agent:codex", "p2"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_new({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://kanboard.invalid/jsonrpc.php",
            }, args)

        text = out.getvalue()
        self.assertIn("Created ticket #70", text)
        self.assertIn("Ask the user now", text)
        self.assertIn("agent-ticket dispatch 70", text)
        self.assertIn("agent-ticket supervise 70 --full-permission", text)
        contact.assert_not_called()

    def test_new_without_dispatch_json_includes_owner_agent_routing_prompt(self):
        args = argparse.Namespace(
            project="demo", force=True, watch_origin=False, dispatch=False, tag=None, agent="codex",
            severity="p2", title="Demo issue", column="New", body="body", kind="bug", json=True,
        )
        task = {"id": 70, "title": "Demo issue", "column_id": 1, "swimlane_id": 1, "is_active": 1}

        def fake_rpc(cfg, method, params=None):
            if method == "createTask":
                return 70
            if method == "getTask":
                return task
            self.fail("unexpected rpc call %s" % method)

        out = io.StringIO()
        with mock.patch.object(self.cli, "resolve_column_id", return_value=1), \
             mock.patch.object(self.cli, "resolve_category_id", return_value=2), \
             mock.patch.object(self.cli, "rpc", side_effect=fake_rpc), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "agent:codex", "p2"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_new({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://kanboard.invalid/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        prompt = result["owner_agent_routing_prompt"]
        self.assertTrue(prompt["ask_user"])
        self.assertEqual("demo", prompt["project"])
        self.assertEqual("/tmp/demo", prompt["repo"])
        commands = [route["command"] for route in prompt["approved_routes"]]
        self.assertIn("agent-ticket dispatch 70", commands)
        self.assertIn("agent-ticket supervise 70 --full-permission", commands)

    def test_new_without_dispatch_in_current_owner_repo_does_not_prompt_routing(self):
        args = argparse.Namespace(
            project="demo", force=True, watch_origin=False, dispatch=False, tag=None, agent="codex",
            severity="p2", title="Demo issue", column="New", body="body", kind="bug", json=True,
        )
        task = {"id": 71, "title": "Demo issue", "column_id": 1, "swimlane_id": 1, "is_active": 1}

        def fake_rpc(cfg, method, params=None):
            if method == "createTask":
                return 71
            if method == "getTask":
                return task
            self.fail("unexpected rpc call %s" % method)

        out = io.StringIO()
        with mock.patch.object(self.cli, "resolve_column_id", return_value=1), \
             mock.patch.object(self.cli, "resolve_category_id", return_value=2), \
             mock.patch.object(self.cli, "rpc", side_effect=fake_rpc), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "agent:codex", "p2"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "_current_origin_repo", return_value="/tmp/demo"), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_new({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://kanboard.invalid/jsonrpc.php",
            }, args)

        prompt = json.loads(out.getvalue())["owner_agent_routing_prompt"]
        self.assertFalse(prompt["ask_user"])
        self.assertEqual("current repo owns ticket; fix in-place", prompt["reason"])
        self.assertEqual([], prompt["approved_routes"])
        self.assertIn("Agent working", prompt["safety"])

    def test_concurrent_watcher_registrations_preserve_both_tickets(self):
        def register(ticket_id):
            args = argparse.Namespace(
                watch_origin=True,
                origin_repo="/tmp/origin-%s" % ticket_id,
                origin_provider="codex",
                origin_session=None,
                watch_expires_hours=2,
                callback_reason="owner closeout",
                correlation_id="corr-%s" % ticket_id,
            )
            self.cli._register_origin_watcher({"comment_user_id": 1}, ticket_id, args, "supervise")

        with mock.patch.object(self.cli, "audit_comment"):
            threads = [threading.Thread(target=register, args=(tid,)) for tid in (57, 58)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        store = json.loads(pathlib.Path(self.cli.WATCHERS_PATH).read_text())
        self.assertEqual("corr-57", store["watchers"]["57"]["correlation_id"])
        self.assertEqual("corr-58", store["watchers"]["58"]["correlation_id"])

    def test_reopen_supersedes_unacknowledged_close_event(self):
        watcher = {
            "ticket_id": 59,
            "origin_repo": "/tmp/origin-repo",
            "origin_provider": "codex",
            "origin_session": "origin-session",
            "created_at": "2026-05-12T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "expires_at_epoch": 4070908800.0,
            "callback_reason": "owner closeout",
            "correlation_id": "corr-59",
            "source_command": "supervise",
            "close_revision": 0,
            "last_closed_state": False,
            "delivered_events": [],
        }
        self.cli._save_watcher_store({"version": 1, "watchers": {"59": watcher}})

        def refused_contact(repo, provider, message, dry_run=False, session=None):
            return {"ok": False, "rc": 3, "json": {"reason": "pane busy"}, "stderr": "", "raw": ""}

        with mock.patch.object(self.cli, "_closeout_report", return_value={"ok": True, "project": "demo", "repo": "/tmp/demo", "checks": []}), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=refused_contact), \
             mock.patch.object(self.cli, "audit_comment"):
            pending = self.reserve_then_notify({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 59)[0]

        self.assertEqual("pending", pending["status"])
        reopened = self.cli._mark_ticket_reopened_for_callbacks(59)
        superseded = reopened["superseded_event"]
        self.assertEqual("ticket:59:closed:1", superseded["event_key"])
        self.assertEqual("superseded", superseded["status"])

        with mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.cli.cmd_callbacks(None, argparse.Namespace(action="retry", event_key="ticket:59:closed:1",
                                                            pending=False, repo=None, json=False))
        contact.assert_not_called()

    def test_concurrent_notify_delivers_callback_once(self):
        watcher = {
            "ticket_id": 60,
            "origin_repo": "/tmp/origin-repo",
            "origin_provider": "codex",
            "origin_session": "origin-session",
            "created_at": "2026-05-12T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "expires_at_epoch": 4070908800.0,
            "callback_reason": "owner closeout",
            "correlation_id": "corr-60",
            "source_command": "supervise",
            "close_revision": 0,
            "last_closed_state": False,
            "delivered_events": [],
        }
        self.cli._save_watcher_store({"version": 1, "watchers": {"60": watcher}})
        calls = []
        send_started = threading.Event()

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            calls.append(("dry-run" if dry_run else "send", provider))
            if not dry_run:
                send_started.set()
                time.sleep(0.05)
            return {"ok": True, "rc": 0, "json": {"session": "origin-session"}, "stderr": "", "raw": "{}"}

        def notify():
            self.reserve_then_notify({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 60)

        with mock.patch.object(self.cli, "_closeout_report", return_value={"ok": True, "project": "demo", "repo": "/tmp/demo", "checks": []}), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "audit_comment"):
            threads = [threading.Thread(target=notify) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(send_started.wait(1))
            for thread in threads:
                thread.join()

        self.assertEqual([("dry-run", "codex"), ("send", "codex")], calls)
        record = self.cli._read_event_record("ticket:60:closed:1")
        self.assertEqual("delivered", record["status"])
        self.assertEqual(2, len(record["delivery_attempts"]))

    def test_ack_during_delivery_is_not_overwritten(self):
        record = {
            "schema": 1,
            "event": "ticket.closed",
            "event_key": "ticket:61:closed:1",
            "ticket_id": 61,
            "revision": 1,
            "created_at": "2026-05-12T00:00:00Z",
            "status": "pending",
            "ticket_url": "http://kanboard.invalid/task/61",
            "watcher": {
                "origin_repo": "/tmp/origin-repo",
                "origin_provider": "codex",
                "origin_session": "origin-session",
                "expires_at": "2099-01-01T00:00:00Z",
                "expires_at_epoch": 4070908800.0,
                "correlation_id": "corr-61",
            },
            "closeout": {"ok": True, "project": "demo", "repo": "/tmp/demo", "summary": "closeout-check pass", "checks": []},
            "delivery_attempts": [],
        }
        self.cli._write_event_record(record)
        send_started = threading.Event()

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            if not dry_run:
                send_started.set()
                time.sleep(0.05)
            return {"ok": True, "rc": 0, "json": {"session": "origin-session"}, "stderr": "", "raw": "{}"}

        with mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "audit_comment"), \
             mock.patch("sys.stdout", new=io.StringIO()):
            thread = threading.Thread(
                target=lambda: self.cli._attempt_callback_delivery(
                    {"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1},
                    record,
                )
            )
            thread.start()
            self.assertTrue(send_started.wait(1))
            self.cli.cmd_callbacks(None, argparse.Namespace(action="ack", event_key="ticket:61:closed:1",
                                                            pending=False, repo=None, json=False))
            thread.join()

        final = self.cli._read_event_record("ticket:61:closed:1")
        self.assertEqual("acknowledged", final["status"])
        self.assertIn("acknowledged_at", final)

    def test_stale_delivering_callback_is_pending_visible_and_retryable(self):
        old = time.time() - (self.cli.CALLBACK_DELIVERING_STALE_SECONDS + 60)
        record = {
            "schema": 1,
            "event": "ticket.closed",
            "event_key": "ticket:62:closed:1",
            "ticket_id": 62,
            "revision": 1,
            "created_at": "2026-05-12T00:00:00Z",
            "status": "delivering",
            "delivery_started_at": self.cli._utc_iso(old),
            "ticket_url": "http://kanboard.invalid/task/62",
            "watcher": {
                "origin_repo": "/tmp/agent-tickets",
                "origin_provider": "codex",
                "origin_session": "origin-session",
                "expires_at": "2099-01-01T00:00:00Z",
                "expires_at_epoch": 4070908800.0,
                "correlation_id": "corr-62",
            },
            "closeout": {"ok": True, "project": "demo", "repo": "/tmp/demo", "summary": "closeout-check pass", "checks": []},
            "delivery_attempts": [],
        }
        self.cli._write_event_record(record)
        out = io.StringIO()
        with mock.patch("sys.stdout", new=out):
            self.cli.cmd_callbacks(None, argparse.Namespace(action="list", event_key=None,
                                                            pending=True, repo="agent-tickets", json=True))
        visible = json.loads(out.getvalue())
        self.assertEqual("ticket:62:closed:1", visible[0]["event_key"])
        self.assertEqual("pending", visible[0]["status"])

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {"ok": True, "rc": 0, "json": {"session": "origin-session"}, "stderr": "", "raw": "{}"}

        with mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "audit_comment"), \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.cli.cmd_callbacks({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1},
                                    argparse.Namespace(action="retry", event_key="ticket:62:closed:1",
                                                       pending=False, repo=None, json=False))
        self.assertEqual("delivered", self.cli._read_event_record("ticket:62:closed:1")["status"])

    def test_missing_referenced_outbox_event_fails_loudly(self):
        watcher = {
            "ticket_id": 63,
            "origin_repo": "/tmp/origin-repo",
            "origin_provider": "codex",
            "origin_session": "origin-session",
            "created_at": "2026-05-12T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "expires_at_epoch": 4070908800.0,
            "callback_reason": "owner closeout",
            "correlation_id": "corr-63",
            "source_command": "supervise",
            "close_revision": 1,
            "last_closed_state": True,
            "last_event_key": "ticket:63:closed:1",
            "delivered_events": [],
        }
        self.cli._save_watcher_store({"version": 1, "watchers": {"63": watcher}})
        with self.assertRaises(SystemExit) as caught:
            self.cli._notify_ticket_closed({"endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1}, 63)
        self.assertIn("missing or unreadable", str(caught.exception))

    def test_close_reserves_outbox_event_before_kanboard_move_or_close(self):
        watcher = {
            "ticket_id": 64,
            "origin_repo": "/tmp/origin-repo",
            "origin_provider": "codex",
            "origin_session": "origin-session",
            "created_at": "2026-05-12T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "expires_at_epoch": 4070908800.0,
            "callback_reason": "owner closeout",
            "correlation_id": "corr-64",
            "source_command": "supervise",
            "close_revision": 0,
            "last_closed_state": False,
            "delivered_events": [],
        }
        self.cli._save_watcher_store({"version": 1, "watchers": {"64": watcher}})
        task = {"id": 64, "column_id": 3, "swimlane_id": 1, "is_active": 1}
        calls = []

        def fake_rpc(cfg, method, params=None):
            calls.append(method)
            if method == "moveTaskPosition":
                reserved = self.cli._read_event_record("ticket:64:closed:1")
                self.assertIsNotNone(reserved)
                self.assertIn(reserved["status"], ("closing", "delivering", "delivered"))
            return True

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {"ok": True, "rc": 0, "json": {"session": "origin-session"}, "stderr": "", "raw": "{}"}

        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "resolve_column_id", return_value=5), \
             mock.patch.object(self.cli, "_closeout_report", return_value={"ok": True, "project": "demo", "repo": "/tmp/demo", "checks": []}), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "rpc", side_effect=fake_rpc), \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.cli.cmd_close({"project_id": 1, "endpoint": "http://kanboard.invalid/jsonrpc.php", "comment_user_id": 1},
                               argparse.Namespace(id=64, json=False))

        self.assertIn("moveTaskPosition", calls)
        self.assertIn("closeTask", calls)

    def test_source_info_reports_skill_and_notify_hook_parity_fields(self):
        source = pathlib.Path(self.tmpdir.name) / "source"
        installed = pathlib.Path(self.tmpdir.name) / "installed"
        (source / "bin").mkdir(parents=True)
        (source / "skill").mkdir()
        (source / "scripts").mkdir()
        installed.mkdir()
        (source / "install.sh").write_text("#!/bin/sh\n")
        (source / "bin" / "agent-ticket").write_text("cli\n")
        (source / "skill" / "SKILL.md").write_text("skill\n")
        (source / "scripts" / "notify-hook.sh").write_text("hook\n")
        (installed / "agent-ticket").write_text("cli\n")
        (installed / "codex-skill.md").write_text("skill\n")
        (installed / "claude-skill.md").write_text("skill\n")
        (installed / "notify-hook.sh").write_text("hook\n")
        manifest = pathlib.Path(self.tmpdir.name) / "source.json"
        manifest.write_text(json.dumps({
            "source_dir": str(source),
            "source_cli": str(source / "bin" / "agent-ticket"),
            "source_skill": str(source / "skill" / "SKILL.md"),
            "source_notify_hook": str(source / "scripts" / "notify-hook.sh"),
            "installed_cli": str(installed / "agent-ticket"),
            "installed_codex_skill": str(installed / "codex-skill.md"),
            "installed_claude_skill": str(installed / "claude-skill.md"),
            "installed_notify_hook": str(installed / "notify-hook.sh"),
            "install_mode": "copy",
        }))
        old_manifest = self.cli.SOURCE_MANIFEST_PATH
        self.cli.SOURCE_MANIFEST_PATH = str(manifest)
        try:
            info = self.cli._source_info()
        finally:
            self.cli.SOURCE_MANIFEST_PATH = old_manifest

        self.assertTrue(info["installed_cli_matches_source"])
        self.assertTrue(info["installed_codex_skill_matches_source"])
        self.assertTrue(info["installed_claude_skill_matches_source"])
        self.assertTrue(info["installed_notify_hook_matches_source"])

    def test_skill_frontmatter_description_is_compact_discovery_only(self):
        text = (ROOT / "skill" / "SKILL.md").read_text()
        parts = text.split("---", 2)
        self.assertEqual(3, len(parts))
        frontmatter = parts[1]
        body = parts[2]
        description_line = next(
            line for line in frontmatter.splitlines()
            if line.startswith("description: ")
        )
        description = description_line.split(":", 1)[1].strip().strip('"')
        lowered = description.lower()

        self.assertLessEqual(len(description), 220)
        for term in (
            "agent-ticket",
            "file",
            "check",
            "dispatch",
            "supervise",
            "closeout-check",
            "callback",
        ):
            self.assertIn(term, lowered)
        for deferred_detail in (
            "if you own",
            "development backlog",
            "test/qa/review",
            "agent blocked while running",
        ):
            self.assertNotIn(deferred_detail, lowered)
        for body_detail in (
            "## Discovery Triggers",
            "## Who files vs. who consumes",
            "## Dispatching a filed ticket to its owner agent",
            "instruction source, the exact friction, expected behavior, observed behavior,\nand impact",
            "file a `friction` ticket even if you can\nresolve the immediate task",
            "agent-ticket supervise 42 --full-permission",
            "agent-ticket closeout-check 42 --strict",
            "agent-ticket callbacks --pending --repo <repo>",
            "## Onboarding Dependency Check",
            "agent-contact artifact-info --all --json",
            "git clone https://github.com/tarkansarim/Agent-Terminal-Contact.git",
        ):
            self.assertIn(body_detail, body)

    def test_install_help_and_unknown_args_exit_before_side_effects(self):
        with tempfile.TemporaryDirectory() as home:
            help_result = subprocess.run(
                ["bash", "install.sh", "--help"],
                cwd=ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={"HOME": home, "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(0, help_result.returncode, help_result.stderr)
            self.assertIn("Usage: ./install.sh", help_result.stdout)
            self.assertFalse((pathlib.Path(home) / ".local" / "bin" / "agent-ticket").exists())

            bad_result = subprocess.run(
                ["bash", "install.sh", "--dry-run"],
                cwd=ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={"HOME": home, "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(1, bad_result.returncode)
            self.assertIn("unknown argument: --dry-run", bad_result.stderr)
            self.assertFalse((pathlib.Path(home) / ".local" / "bin" / "agent-ticket").exists())

    def test_windows_installer_entrypoint_is_documented_and_uses_powershell(self):
        bat = (ROOT / "install-windows.bat").read_text()
        ps1 = (ROOT / "scripts" / "install-windows.ps1").read_text()
        readme = (ROOT / "README.md").read_text()
        register_hooks = (ROOT / "scripts" / "register-hooks.py").read_text()

        self.assertIn("powershell.exe -NoProfile -ExecutionPolicy Bypass", bat)
        self.assertIn(r"scripts\install-windows.ps1", bat)
        self.assertIn("Usage: install-windows.bat", ps1)
        self.assertIn("Docker Desktop", ps1)
        self.assertIn("agent-ticket.cmd", ps1)
        self.assertIn('.claude\\skills', ps1)
        self.assertIn('.codex\\skills', ps1)
        self.assertIn("Test-Path $providerHome", ps1)
        self.assertIn("skipped $skillDest", ps1)
        self.assertIn("install-windows.bat", readme)
        self.assertIn('MARKER = "notify-hook"', register_hooks)

    def test_cli_imports_when_fcntl_is_unavailable(self):
        import builtins
        import types

        original_import = builtins.__import__
        dummy_msvcrt = types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=2, locking=lambda *a, **k: None)

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "fcntl":
                raise ImportError("no fcntl on this platform")
            if name == "msvcrt":
                return dummy_msvcrt
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = fake_import
        try:
            loader = SourceFileLoader("agent_ticket_no_fcntl", str(ROOT / "bin" / "agent-ticket"))
            spec = spec_from_loader(loader.name, loader)
            module = module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            builtins.__import__ = original_import

        self.assertIsNone(module.fcntl)
        self.assertIs(module.msvcrt, dummy_msvcrt)

    def test_resolve_repo_path_accepts_single_case_insensitive_directory_match(self):
        root = pathlib.Path(self.tmpdir.name) / "repos"
        repo = root / "Reply-Verbosity"
        repo.mkdir(parents=True)

        resolved = self.cli.resolve_repo_path({"repo_roots": [str(root)]}, "reply-verbosity")

        self.assertEqual(str(repo.resolve()), resolved)

    def test_resolve_repo_path_prefers_exact_case_match(self):
        root = pathlib.Path(self.tmpdir.name) / "repos"
        exact = root / "reply-verbosity"
        alternate = root / "Reply-Verbosity"
        exact.mkdir(parents=True)
        alternate.mkdir()

        resolved = self.cli.resolve_repo_path({"repo_roots": [str(root)]}, "reply-verbosity")

        self.assertEqual(str(exact.resolve()), resolved)

    def test_resolve_repo_path_rejects_ambiguous_case_insensitive_matches(self):
        root = pathlib.Path(self.tmpdir.name) / "repos"
        (root / "Reply-Verbosity").mkdir(parents=True)
        (root / "reply-verbosity").mkdir()

        resolved = self.cli.resolve_repo_path({"repo_roots": [str(root)]}, "REPLY-VERBOSITY")

        self.assertIsNone(resolved)

    def test_tag_command_reports_stored_tags_after_kanboard_normalization(self):
        calls = []

        def fake_task_tags(cfg, tid):
            calls.append(tid)
            if len(calls) == 1:
                return ["project:reply-verbosity", "p2"]
            return ["project:reply-verbosity", "p2", "agent:codex"]

        def fake_rpc(cfg, method, params=None):
            self.assertEqual("setTaskTags", method)
            self.assertIn("project:Reply-Verbosity", params["tags"])
            return True

        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 219}), \
             mock.patch.object(self.cli, "task_tags", side_effect=fake_task_tags), \
             mock.patch.object(self.cli, "rpc", side_effect=fake_rpc), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_tag(
                {"project_id": 1},
                argparse.Namespace(
                    id=219,
                    add=["project:Reply-Verbosity", "agent:codex"],
                    remove=[],
                    json=False,
                ),
            )

        self.assertIn("project:reply-verbosity", out.getvalue())
        self.assertNotIn("project:Reply-Verbosity", out.getvalue())

    def test_supervise_batch_routes_lowercase_project_tag_to_uppercase_repo_directory(self):
        root = pathlib.Path(self.tmpdir.name) / "repos"
        repo = root / "Reply-Verbosity"
        repo.mkdir(parents=True)
        tasks = [{"id": 218, "title": "Route mixed case repo", "column_id": 1, "category_id": 1, "is_active": 1}]

        with mock.patch.object(self.cli, "_all_tasks", return_value=tasks), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:reply-verbosity", "p2"]), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"):
            groups, skipped = self.cli._batch_collect_ticket_groups(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php", "repo_roots": [str(root)]},
                self.batch_args(),
            )

        self.assertEqual([], skipped)
        self.assertEqual(1, len(groups))
        self.assertEqual(str(repo.resolve()), groups[0]["repo"])
        self.assertEqual("Reply-Verbosity", groups[0]["session_key"])
        self.assertEqual("reply-verbosity", groups[0]["project"])

    def test_supervise_batch_groups_same_repo_queue_and_skips_default_refusals(self):
        tasks = [
            {"id": 70, "title": "P1 demo", "column_id": 1, "category_id": 1, "is_active": 1},
            {"id": 71, "title": "P2 demo", "column_id": 2, "category_id": 1, "is_active": 1},
            {"id": 72, "title": "P3 demo", "column_id": 1, "category_id": 1, "is_active": 1},
            {"id": 73, "title": "Human demo", "column_id": 4, "category_id": 1, "is_active": 1},
            {"id": 74, "title": "No project", "column_id": 1, "category_id": 1, "is_active": 1},
            {"id": 75, "title": "Missing repo", "column_id": 1, "category_id": 1, "is_active": 1},
            {"id": 78, "title": "Done no project", "column_id": 5, "category_id": 1, "is_active": 1},
            {"id": 79, "title": "Done multiple projects", "column_id": 5, "category_id": 1, "is_active": 1},
            {"id": 80, "title": "Done missing repo", "column_id": 5, "category_id": 1, "is_active": 1},
            {"id": 81, "title": "Human no project", "column_id": 4, "category_id": 1, "is_active": 1},
            {"id": 82, "title": "Human multiple projects", "column_id": 4, "category_id": 1, "is_active": 1},
            {"id": 83, "title": "P3 no project", "column_id": 1, "category_id": 1, "is_active": 1},
            {"id": 84, "title": "P3 multiple projects", "column_id": 1, "category_id": 1, "is_active": 1},
        ]
        tags = {
            70: ["project:demo", "p1"],
            71: ["project:demo", "p2"],
            72: ["project:demo", "p3"],
            73: ["project:demo", "p1"],
            74: ["p1"],
            75: ["project:missing", "p1"],
            78: ["p1"],
            79: ["project:demo", "project:other", "p1"],
            80: ["project:missing", "p1"],
            81: ["p1"],
            82: ["project:demo", "project:other", "p1"],
            83: ["p3"],
            84: ["project:demo", "project:other", "p3"],
        }
        columns = {1: "New", 2: "Triaging", 4: "Needs human", 5: "Done"}

        with mock.patch.object(self.cli, "_all_tasks", return_value=tasks), \
             mock.patch.object(self.cli, "task_tags", side_effect=lambda cfg, tid: tags[int(tid)]), \
             mock.patch.object(self.cli, "column_name", side_effect=lambda cfg, cid: columns[int(cid)]), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "resolve_repo_path", side_effect=lambda cfg, project: "/tmp/demo" if project == "demo" else None):
            groups, skipped = self.cli._batch_collect_ticket_groups(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args())

        self.assertEqual(1, len(groups))
        self.assertEqual("demo", groups[0]["project"])
        self.assertEqual([70, 71], [t["id"] for t in groups[0]["tickets"]])
        self.assertEqual(["p1", "p2"], [t["severity"] for t in groups[0]["tickets"]])
        reasons = {s["ticket_id"]: s["reason"] for s in skipped}
        self.assertEqual("p3 excluded", reasons[72])
        self.assertEqual("Needs human", reasons[73])
        blocking = {s["ticket_id"]: s.get("blocking", False) for s in skipped}
        self.assertFalse(blocking[72])
        self.assertFalse(blocking[73])
        self.assertTrue(blocking[74])
        self.assertTrue(blocking[75])
        self.assertEqual("missing project tag", reasons[74])
        self.assertEqual("unresolved repo", reasons[75])
        self.assertEqual("Done column", reasons[78])
        self.assertEqual("Done column", reasons[79])
        self.assertEqual("Done column", reasons[80])
        self.assertEqual("Needs human", reasons[81])
        self.assertEqual("Needs human", reasons[82])
        self.assertEqual("p3 excluded", reasons[83])
        self.assertEqual("p3 excluded", reasons[84])
        self.assertFalse(blocking[78])
        self.assertFalse(blocking[79])
        self.assertFalse(blocking[80])
        self.assertFalse(blocking[81])
        self.assertFalse(blocking[82])
        self.assertFalse(blocking[83])
        self.assertFalse(blocking[84])

    def test_supervise_batch_explicit_filters_apply_before_project_tag_blockers(self):
        tasks = [
            {"id": 85, "title": "Severity filtered", "column_id": 1, "category_id": 1, "is_active": 1},
            {"id": 86, "title": "Tag filtered", "column_id": 1, "category_id": 1, "is_active": 1},
            {"id": 87, "title": "Kind filtered", "column_id": 1, "category_id": 2, "is_active": 1},
            {"id": 88, "title": "In scope missing project", "column_id": 1, "category_id": 1, "is_active": 1},
        ]
        tags = {
            85: ["p2", "special"],
            86: ["p1"],
            87: ["p1", "special"],
            88: ["p1", "special"],
        }

        with mock.patch.object(self.cli, "_all_tasks", return_value=tasks), \
             mock.patch.object(self.cli, "task_tags", side_effect=lambda cfg, tid: tags[int(tid)]), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", side_effect=lambda cfg, cid: "bug" if int(cid) == 1 else "friction"), \
             mock.patch.object(self.cli, "resolve_repo_path") as resolve:
            groups, skipped = self.cli._batch_collect_ticket_groups(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"},
                self.batch_args(severity=["p1"], tag=["special"], kind=["bug"]))

        self.assertEqual([], groups)
        self.assertEqual([88], [s["ticket_id"] for s in skipped])
        self.assertEqual("missing project tag", skipped[0]["reason"])
        self.assertTrue(skipped[0]["blocking"])
        resolve.assert_not_called()

    def test_supervise_batch_groups_alias_projects_by_resolved_repo(self):
        tasks = [
            {"id": 76, "title": "Primary", "column_id": 1, "category_id": 1, "is_active": 1},
            {"id": 77, "title": "Alias", "column_id": 1, "category_id": 1, "is_active": 1},
        ]
        tags = {
            76: ["project:demo", "p1"],
            77: ["project:demo-alias", "p2"],
        }

        with mock.patch.object(self.cli, "_all_tasks", return_value=tasks), \
             mock.patch.object(self.cli, "task_tags", side_effect=lambda cfg, tid: tags[int(tid)]), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/shared-real-repo"):
            groups, skipped = self.cli._batch_collect_ticket_groups(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args())

        self.assertEqual([], skipped)
        self.assertEqual(1, len(groups))
        self.assertEqual("/tmp/shared-real-repo", groups[0]["repo"])
        self.assertEqual(["demo", "demo-alias"], groups[0]["projects"])
        self.assertEqual([76, 77], [t["id"] for t in groups[0]["tickets"]])
        message = self.cli._batch_supervision_message(
            {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, groups[0], self.batch_args())
        self.assertIn("project:demo", message)
        self.assertIn("project:demo-alias", message)

    def test_supervise_batch_dry_run_reports_route_without_mutations(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                ticket_ids=[80], repo="/tmp/demo", owner_id="test-supervisor", worker_session="safe-demo"),
        })
        group = {
            "project": "demo",
            "projects": ["demo"],
            "repo": "/tmp/demo",
            "tickets": [{"id": 80, "title": "Demo", "severity": "p1", "kind": "bug", "column": "New",
                         "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        out = io.StringIO()
        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_contactable_providers", return_value=([
                 {"provider": "codex", "session": "safe-demo", "probe": {"ok": True}}
             ], [])), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 80, "title": "Demo", "column_id": 1, "category_id": 1, "is_active": 1}), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(dry_run=True))

        result = json.loads(out.getvalue())
        self.assertTrue(result["dry_run"])
        self.assertEqual("planned", result["groups"][0]["status"])
        self.assertEqual("contact", result["groups"][0]["route"]["mode"])
        self.assertEqual("safe-demo", result["groups"][0]["route"]["session"])
        tmux.assert_not_called()
        contact.assert_not_called()
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_blocks_unbound_contactable_stale_review_lane(self):
        group = {
            "project": "demo",
            "projects": ["demo"],
            "repo": "/tmp/demo",
            "tickets": [{"id": 80, "title": "Demo", "severity": "p1", "kind": "bug", "column": "New",
                         "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        out = io.StringIO()
        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_contactable_providers", return_value=([
                 {"provider": "codex", "session": "review-demo", "probe": {"ok": True}}
             ], [])), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 80, "title": "Demo", "column_id": 1, "category_id": 1, "is_active": 1}), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(dry_run=True))

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("blocked", result["groups"][0]["status"])
        self.assertEqual("unbound contactable session", result["groups"][0]["route"]["reason"])
        self.assertIn("review-demo", result["groups"][0]["route"]["detail"])
        tmux.assert_not_called()
        contact.assert_not_called()
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_dry_run_blocks_active_supervision_claim_without_mutations(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(ticket_ids=[80], repo="/tmp/demo"),
        })
        group = {
            "project": "demo",
            "projects": ["demo"],
            "repo": "/tmp/demo",
            "tickets": [{"id": 80, "title": "Demo", "severity": "p1", "kind": "bug", "column": "New",
                         "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        out = io.StringIO()
        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_contactable_providers") as contactable, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(dry_run=True))

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        group_result = result["groups"][0]
        self.assertEqual("blocked", group_result["status"])
        self.assertEqual("already supervised", group_result["route"]["reason"])
        active = group_result["route"]["active_supervision"][0]
        self.assertEqual("other-supervisor", active["owner_id"])
        self.assertEqual("batch-owner-demo", active["worker_session"])
        self.assertEqual([80], active["ticket_ids"])
        self.assertFalse(active["owned_by_current"])
        contactable.assert_not_called()
        tmux.assert_not_called()
        contact.assert_not_called()
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_live_blocks_active_supervision_claim_before_mutations(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(ticket_ids=[80], repo="/tmp/demo"),
        })
        group = {
            "project": "demo",
            "projects": ["demo"],
            "repo": "/tmp/demo",
            "tickets": [{"id": 80, "title": "Demo", "severity": "p1", "kind": "bug", "column": "New",
                         "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        out = io.StringIO()
        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_contactable_providers") as contactable, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(dry_run=False))

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("already supervised", result["groups"][0]["route"]["reason"])
        contactable.assert_not_called()
        tmux.assert_not_called()
        contact.assert_not_called()
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_same_owner_reentry_reports_owned_claim_and_plans_route(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                ticket_ids=[80], repo="/tmp/demo", owner_id="test-supervisor", worker_session="safe-demo"),
        })
        group = {
            "project": "demo",
            "projects": ["demo"],
            "repo": "/tmp/demo",
            "tickets": [{"id": 80, "title": "Demo", "severity": "p1", "kind": "bug", "column": "New",
                         "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        out = io.StringIO()
        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_contactable_providers", return_value=([
                 {"provider": "codex", "session": "safe-demo", "probe": {"ok": True}}
             ], [])), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 80, "title": "Demo", "column_id": 1, "category_id": 1, "is_active": 1}), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(dry_run=True))

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        group_result = result["groups"][0]
        self.assertEqual("planned", group_result["status"])
        self.assertEqual("contact", group_result["route"]["mode"])
        owned = group_result["supervision"]["owned_claims"][0]
        self.assertEqual("claim-demo", owned["claim_id"])
        self.assertTrue(owned["owned_by_current"])

    def test_supervision_status_reports_active_claim_visibility(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(ticket_ids=[80], repo="/tmp/demo"),
        })
        out = io.StringIO()
        args = argparse.Namespace(
            action="status", claim=None, repo=None, ticket=None, all=True,
            supervisor_id="current-supervisor", force=False, json=True,
        )
        with mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervision(None, args)

        result = json.loads(out.getvalue())
        self.assertEqual(1, len(result["claims"]))
        claim = result["claims"][0]
        self.assertEqual("claim-demo", claim["claim_id"])
        self.assertTrue(claim["active"])
        self.assertFalse(claim["owned_by_current"])
        self.assertEqual("codex", claim["origin_provider"])
        self.assertEqual("/tmp/origin", claim["origin_repo"])
        self.assertEqual("origin-session", claim["origin_session"])
        self.assertEqual("batch-owner-demo", claim["worker_session"])
        self.assertGreaterEqual(claim["age_seconds"], 0)
        self.assertGreater(claim["expires_in_seconds"], 0)

    def test_supervision_status_marks_dead_local_pid_owner_claim_stale_before_ttl(self):
        owner_id = "pid:%s:424242" % self.cli._current_supervision_host()
        self.write_supervision_claims({
            "claim-dead": self.active_supervision_claim(
                claim_id="claim-dead", owner_id=owner_id, ticket_ids=[80], repo="/tmp/demo", ttl=3600),
        })
        out = io.StringIO()
        args = argparse.Namespace(
            action="status", claim=None, repo=None, ticket=None, all=True,
            supervisor_id="current-supervisor", force=False, json=True,
        )
        with mock.patch.object(self.cli, "_process_is_alive", return_value=False), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervision(None, args)

        result = json.loads(out.getvalue())
        claim = result["claims"][0]
        self.assertEqual("claim-dead", claim["claim_id"])
        self.assertFalse(claim["active"])
        self.assertTrue(claim["stale"])
        self.assertEqual("dead", claim["owner_process"]["status"])
        self.assertEqual("owner process is not running", claim["stale_reason"])

    def test_supervision_preflight_does_not_block_on_dead_local_pid_owner_claim(self):
        owner_id = "pid:%s:424242" % self.cli._current_supervision_host()
        self.write_supervision_claims({
            "claim-dead": self.active_supervision_claim(
                claim_id="claim-dead", owner_id=owner_id, ticket_ids=[80], repo="/tmp/demo", ttl=3600),
        })

        with mock.patch.object(self.cli, "_process_is_alive", return_value=False):
            preflight = self.cli._supervision_preflight(
                self.batch_args(supervisor_id="current-supervisor"), "/tmp/demo", [80])

        self.assertFalse(preflight["blocked"])
        self.assertEqual([], preflight["conflicting_claims"])
        self.assertEqual("claim-dead", preflight["stale_claims"][0]["claim_id"])
        self.assertEqual("owner process is not running", preflight["stale_claims"][0]["stale_reason"])

    def test_supervision_release_requires_ownership_or_stale_claim(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(ticket_ids=[80], repo="/tmp/demo"),
        })
        out = io.StringIO()
        args = argparse.Namespace(
            action="release", claim="claim-demo", repo=None, ticket=None, all=False,
            active_only=False, supervisor_id="current-supervisor", force=False,
            supervision_ttl_hours=1, origin_repo=None, origin_provider=None, origin_session=None,
            json=True,
        )
        with mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervision(None, args)

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("claim-demo", result["blocked"][0]["claim_id"])
        data = json.loads(pathlib.Path(self.cli.SUPERVISION_LEASES_PATH).read_text())
        self.assertIn("claim-demo", data["claims"])

    def test_supervision_release_allows_stale_claim_recovery(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(ticket_ids=[80], repo="/tmp/demo", ttl=-10),
        })
        out = io.StringIO()
        args = argparse.Namespace(
            action="release", claim="claim-demo", repo=None, ticket=None, all=False,
            active_only=False, supervisor_id="current-supervisor", force=False,
            supervision_ttl_hours=1, origin_repo=None, origin_provider=None, origin_session=None,
            json=True,
        )
        with mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervision(None, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("claim-demo", result["claims"][0]["claim_id"])
        data = json.loads(pathlib.Path(self.cli.SUPERVISION_LEASES_PATH).read_text())
        self.assertEqual({}, data["claims"])

    def test_supervise_batch_adopt_supervision_reuses_stale_claim(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(ticket_ids=[80], repo="/tmp/demo", ttl=-10),
        })
        route = {"provider": "codex", "session": "safe-demo", "mode": "contact"}
        claim, conflicts = self.cli._acquire_supervision_claim(
            self.batch_args(adopt_supervision=True, supervisor_id="current-supervisor"),
            "supervise-batch", "/tmp/demo", "demo", ["demo"], [80], route=route)

        self.assertEqual([], conflicts)
        self.assertEqual("claim-demo", claim["claim_id"])
        self.assertEqual("current-supervisor", claim["owner_id"])
        self.assertEqual("safe-demo", claim["worker_session"])

    def test_supervise_batch_blocks_instead_of_launching_duplicate_when_existing_session_is_unsafe(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            self.assertEqual("codex-existing", argv[0])
            return {"ok": True, "stdout": "old-demo", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [
            {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
            {"provider": "claude", "reason": "no tmux-managed claude pane found for /tmp/demo", "probe": {}},
        ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("existing session unsafe", route["reason"])
        self.assertEqual("old-demo", route["session"])

    def test_supervise_batch_blocks_non_codex_contact_when_codex_session_exists(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()
        supervision_preflight = {"owned_claims": [{"worker_provider": "claude", "worker_session": "safe-claude"}]}

        def fake_tmux(argv, timeout=25):
            self.assertEqual("codex-existing", argv[0])
            return {"ok": True, "stdout": "old-codex", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([
            {"provider": "claude", "session": "safe-claude", "probe": {"ok": True}},
        ], [
            {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
        ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux) as tmux:
            route = self.cli._batch_select_route(
                args, group, "message", supervision_preflight=supervision_preflight)

        self.assertEqual("blocked", route["status"])
        self.assertEqual("existing session unsafe", route["reason"])
        self.assertEqual("old-codex", route["session"])
        tmux.assert_called_once()

    def test_supervise_batch_rejects_same_session_wrong_provider_claim_binding(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()
        supervision_preflight = {"owned_claims": [{"worker_provider": "codex", "worker_session": "safe-demo"}]}
        contactable = [{"provider": "claude", "session": "safe-demo", "probe": {"ok": True}}]

        with mock.patch.object(self.cli, "_contactable_providers", return_value=(contactable, [
            {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
        ])), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux:
            route = self.cli._batch_select_route(
                args, group, "message", supervision_preflight=supervision_preflight)

        self.assertEqual("blocked", route["status"])
        self.assertEqual("unbound contactable session", route["reason"])
        self.assertIn("claude session safe-demo", route["detail"])
        tmux.assert_not_called()

    def test_supervise_batch_blocks_when_existing_sessions_are_ambiguous(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            self.assertEqual("codex-existing", argv[0])
            return {
                "ok": False,
                "stdout": "",
                "stderr": "agent-tmux: multiple detached Codex tmux sessions; refusing to guess",
                "rc": 3,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("ambiguous existing sessions", route["reason"])

    def test_supervise_batch_blocks_unknown_existing_lookup_failure(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            self.assertEqual("codex-existing", argv[0])
            return {
                "ok": False,
                "stdout": "",
                "stderr": "agent-tmux not found on PATH",
                "rc": 127,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux existing lookup failed", route["reason"])

    def test_supervise_batch_blocks_unrecognized_existing_lookup_stderr(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            self.assertEqual("codex-existing", argv[0])
            return {
                "ok": False,
                "stdout": "",
                "stderr": "unexpected parser failure",
                "rc": 1,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux existing lookup failed", route["reason"])

    def test_supervise_batch_blocks_empty_existing_lookup_failure(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            self.assertEqual("codex-existing", argv[0])
            return {"ok": False, "stdout": "", "stderr": "", "rc": 1, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux existing lookup failed", route["reason"])

    def test_supervise_batch_rejects_no_session_text_with_wrong_existing_rc(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()
        calls = []

        def fake_tmux(argv, timeout=25):
            calls.append(argv[0])
            self.assertEqual("codex-existing", argv[0])
            return {
                "ok": False,
                "stdout": "",
                "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                "rc": 2,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux existing lookup failed", route["reason"])
        self.assertEqual(["codex-existing"], calls)

    def test_supervise_batch_rejects_no_existing_session_text_for_wrong_repo(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            self.assertEqual("codex-existing", argv[0])
            return {
                "ok": False,
                "stdout": "",
                "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/other",
                "rc": 1,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux existing lookup failed", route["reason"])

    def test_codex_existing_absence_rejects_wrong_requested_session_suffix(self):
        result = {
            "ok": False,
            "stdout": "",
            "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo; session: other-demo",
            "rc": 1,
            "argv": ["agent-tmux", "codex-existing", "/tmp/demo", "owner-demo-53"],
        }

        self.assertFalse(self.cli._batch_codex_existing_is_absent(result, "/tmp/demo", "owner-demo-53"))
        self.assertTrue(self.cli._batch_codex_existing_is_absent(result, "/tmp/demo", "other-demo"))

    def test_contact_missing_window_absence_requires_exact_requested_session(self):
        refusal = {
            "provider": "codex",
            "reason": "tmux pane discovery failed: can't find window: owner-demo-53",
            "probe": {"json": {"reason": "tmux pane discovery failed: can't find window: owner-demo-53"}},
        }

        self.assertFalse(self.cli._batch_refusal_is_safe_absence(refusal, "/tmp/demo"))
        self.assertFalse(self.cli._batch_refusal_is_safe_absence(refusal, "/tmp/demo", "other-demo"))
        self.assertTrue(self.cli._batch_refusal_is_safe_absence(refusal, "/tmp/demo", "owner-demo-53"))

    def test_contact_missing_tmux_server_socket_is_safe_absence(self):
        refusal = {
            "provider": "codex",
            "reason": "tmux pane discovery failed: error connecting to /tmp/tmux-1000/default (No such file or directory)",
            "probe": {
                "json": {
                    "reason": "tmux pane discovery failed: error connecting to /tmp/tmux-1000/default (No such file or directory)"
                }
            },
        }
        permission_refusal = {
            "provider": "codex",
            "reason": "tmux pane discovery failed: error connecting to /tmp/tmux-1000/default (Permission denied)",
            "probe": {
                "json": {
                    "reason": "tmux pane discovery failed: error connecting to /tmp/tmux-1000/default (Permission denied)"
                }
            },
        }

        self.assertTrue(self.cli._batch_refusal_is_safe_absence(refusal, "/tmp/demo", "owner-demo-53"))
        self.assertTrue(self.cli._batch_refusal_is_safe_absence(refusal, "/tmp/other", "other-demo"))
        self.assertFalse(self.cli._batch_refusal_is_safe_absence(permission_refusal, "/tmp/demo", "owner-demo-53"))

    def test_contact_no_current_target_is_safe_absence_for_exact_ticket_session(self):
        refusal = {
            "provider": "codex",
            "reason": "tmux pane discovery failed: no current target",
            "probe": {"json": {"reason": "tmux pane discovery failed: no current target"}},
        }

        self.assertTrue(self.cli._batch_refusal_is_safe_absence(refusal, "/tmp/demo", "owner-demo-53"))
        self.assertFalse(self.cli._batch_refusal_is_safe_absence(refusal, "/tmp/demo"))

    def test_supervise_dry_run_reports_first_contact_plan_for_no_owner_pane(self):
        task = {"id": 116, "title": "Fix first contact path", "column_id": 2, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {
                "ok": False,
                "rc": 3,
                "json": {"reason": "tmux pane discovery failed: no current target"},
                "stderr": "",
                "raw": "",
            }

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "rc": 1,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo; session: owner-demo-116",
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("dry-run should not launch")

        args = argparse.Namespace(
            id=116, provider=None, session=None, session_prefix="owner", full_permission=True,
            message="", dry_run=True, no_tool_ticket=True, poll_interval=0, max_polls=0,
            strict_closeout=False, require_clean=False, require_validation=False,
            require_commit=False, require_install=False, json=True, watch_origin=False,
            supervisor_id="current-supervisor", supervision_ttl_hours=1,
            adopt_supervision=False, steal_supervision=False, force_supervision=False,
        )
        out = io.StringIO()
        with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo"]), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_resolve_ticket_repo", return_value=("demo", "/tmp/demo")), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise({
                "project_id": 1,
                "repo_roots": ["/tmp"],
                "endpoint": "http://127.0.0.1:8765/jsonrpc.php",
            }, args)

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertEqual("planned", result["first_contact"]["status"])
        self.assertIn("fresh Codex launch", result["first_contact"]["reason"])
        self.assertIn("agent-tmux codex owner-demo-116 /tmp/demo -s danger-full-access -a never", result["first_contact"]["command"])
        self.assertEqual(2, len(result["absence_refusals"]))
        self.assertNotIn("unsafe provider refusal", json.dumps(result))
        self.assertEqual([["codex-existing", "/tmp/demo", "owner-demo-116"]], tmux_calls)
        audit.assert_not_called()

    def test_supervise_batch_does_not_query_latest_when_launching_fresh(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("codex-latest is not route authority for fresh launches")

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("planned", route["status"])
        self.assertEqual("launch", route["mode"])
        self.assertEqual("skipped", route["resume_latest"]["status"])

    def test_supervise_batch_missing_tmux_server_refusals_launch_fresh(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": [], "session_key": "demo"}
        args = self.batch_args()
        missing_socket = "tmux pane discovery failed: error connecting to /tmp/tmux-1000/default (No such file or directory)"

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("codex-latest is not route authority for fresh launches")

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [
            {"provider": "codex", "reason": missing_socket, "probe": {"json": {"reason": missing_socket}}},
            {"provider": "claude", "reason": missing_socket, "probe": {"json": {"reason": missing_socket}}},
        ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("planned", route["status"])
        self.assertEqual("launch", route["mode"])
        self.assertEqual("batch-owner-demo", route["session"])
        self.assertEqual("skipped", route["resume_latest"]["status"])

    def test_supervise_batch_does_not_query_latest_when_stale_latest_exists(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": [], "session_key": "demo"}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("codex-latest is not route authority for fresh launches")

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [
            {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
            {"provider": "claude", "reason": "no tmux-managed claude pane found for /tmp/demo", "probe": {}},
        ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("planned", route["status"])
        self.assertEqual("launch", route["mode"])
        self.assertEqual("batch-owner-demo", route["session"])
        self.assertEqual("skipped", route["resume_latest"]["status"])
        self.assertNotEqual("resume-latest", route["mode"])

    def test_supervise_batch_audit_comment_exposes_resume_latest_skip(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 97, "title": "One", "severity": "p1", "kind": "bug", "column": "Triaging",
                         "column_id": 2, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
            "session_key": "demo",
        }
        tmux_calls = []
        audit_messages = []

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            if argv[0] == "codex-latest":
                self.fail("supervise-batch must not consult codex-latest")
            if argv[0] == "codex":
                return {"ok": True, "stdout": "launched", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}
            return {"ok": False, "stdout": "", "stderr": "unexpected", "rc": 2, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [
                 {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
                 {"provider": "claude", "reason": "no tmux-managed claude pane found for /tmp/demo", "probe": {}},
             ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={
                 "id": 97, "title": "One", "column_id": 2, "category_id": 1, "is_active": 1, "swimlane_id": 1,
             }), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "audit_comment", side_effect=lambda cfg, tid, text: audit_messages.append(text)), \
             mock.patch.object(self.cli, "move_task_to_column"):
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("routed", result["status"])
        self.assertEqual("launch", result["route"]["mode"])
        self.assertIn("resume-latest skipped", audit_messages[0])
        self.assertNotIn("codex-resume-latest", [call[0] for call in tmux_calls])

    def test_supervise_batch_ignores_malformed_latest_when_launching_fresh(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("codex-latest is not route authority for fresh launches")

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("planned", route["status"])
        self.assertEqual("launch", route["mode"])
        self.assertEqual("skipped", route["resume_latest"]["status"])

    def test_supervise_batch_ignores_wrong_latest_rc_when_launching_fresh(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("codex-latest is not route authority for fresh launches")

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("planned", route["status"])
        self.assertEqual("launch", route["mode"])
        self.assertEqual("skipped", route["resume_latest"]["status"])

    def test_supervise_batch_ignores_wrong_repo_latest_when_launching_fresh(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("codex-latest is not route authority for fresh launches")

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("planned", route["status"])
        self.assertEqual("launch", route["mode"])
        self.assertEqual("skipped", route["resume_latest"]["status"])

    def test_supervise_batch_ignores_latest_success_with_extra_line(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("codex-latest is not route authority for fresh launches")

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("planned", route["status"])
        self.assertEqual("launch", route["mode"])
        self.assertEqual("skipped", route["resume_latest"]["status"])

    def test_supervise_batch_ignores_latest_success_with_stderr_warning(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            self.fail("codex-latest is not route authority for fresh launches")

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("planned", route["status"])
        self.assertEqual("launch", route["mode"])
        self.assertEqual("skipped", route["resume_latest"]["status"])

    def test_supervise_batch_blocks_cross_provider_unsafe_refusal_before_contact(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()
        contactable = [{"provider": "codex", "session": "safe-codex", "probe": {"ok": True}}]
        refused = [{
            "provider": "claude",
            "reason": "pane busy [session claude-demo, state agent_working]",
            "probe": {"json": {"reason": "pane busy", "session": "claude-demo", "pane_state": "agent_working"}},
        }]

        with mock.patch.object(self.cli, "_contactable_providers", return_value=(contactable, refused)), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux:
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("unsafe provider refusal", route["reason"])
        self.assertIn("claude", route["detail"])
        tmux.assert_not_called()

    def test_supervise_batch_blocks_cross_provider_unsafe_refusal_before_launch(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()
        refused = [
            {
                "provider": "codex",
                "reason": "no tmux-managed codex pane found for /tmp/demo",
                "probe": {"json": {"reason": "no tmux-managed codex pane found for /tmp/demo"}},
            },
            {
                "provider": "claude",
                "reason": "multiple candidate panes [session claude-demo, state ambiguous]",
                "probe": {"json": {"reason": "multiple candidate panes", "session": "claude-demo", "pane_state": "ambiguous"}},
            },
        ]

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], refused)), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux:
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("unsafe provider refusal", route["reason"])
        self.assertIn("claude", route["detail"])
        tmux.assert_not_called()

    def test_supervise_batch_blocks_wrong_repo_no_pane_refusal_before_launch(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args()
        refused = [
            {
                "provider": "codex",
                "reason": "no tmux-managed codex pane found for /tmp/other",
                "probe": {"json": {"reason": "no tmux-managed codex pane found for /tmp/other"}},
            },
            {
                "provider": "claude",
                "reason": "no tmux-managed claude pane found for /tmp/other",
                "probe": {"json": {"reason": "no tmux-managed claude pane found for /tmp/other"}},
            },
        ]

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], refused)), \
             mock.patch.object(self.cli, "_agent_tmux") as tmux:
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("unsafe provider refusal", route["reason"])
        self.assertIn("/tmp/other", route["detail"])
        tmux.assert_not_called()

    def test_supervise_batch_explicit_provider_blocks_other_contactable_lane(self):
        group = {"project": "demo", "repo": "/tmp/demo", "tickets": []}
        args = self.batch_args(provider="codex")
        supervision_preflight = {
            "owned_claims": [
                {"worker_provider": "codex", "worker_session": "safe-codex"},
                {"worker_provider": "claude", "worker_session": "safe-claude"},
            ]
        }
        contactable = [
            {"provider": "codex", "session": "safe-codex", "probe": {"ok": True}},
            {"provider": "claude", "session": "safe-claude", "probe": {"ok": True}},
        ]

        with mock.patch.object(self.cli, "_contactable_providers", return_value=(contactable, [])):
            route = self.cli._batch_select_route(
                args, group, "message", supervision_preflight=supervision_preflight)

        self.assertEqual("blocked", route["status"])
        self.assertEqual("provider conflict", route["reason"])
        self.assertIn("claude", route["detail"])

    def test_supervise_batch_launch_uses_deterministic_full_permission_session(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 81, "title": "One", "severity": "p1", "kind": "bug", "column": "Triaging",
                         "column_id": 2, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        tmux_calls = []

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            if argv[0] == "codex-latest":
                self.fail("supervise-batch must not consult codex-latest")
            return {"ok": True, "stdout": "launched", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 81, "title": "One", "column_id": 2, "category_id": 1, "is_active": 1}), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "audit_comment"), \
             mock.patch.object(self.cli, "move_task_to_column"):
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(full_permission=True), group)

        self.assertEqual("routed", result["status"])
        self.assertEqual("batch-owner-demo", result["route"]["session"])
        self.assertEqual("codex-full", tmux_calls[-1][0])
        self.assertEqual("batch-owner-demo", tmux_calls[-1][1])
        self.assertEqual("/tmp/demo", tmux_calls[-1][2])

    def test_supervise_batch_blocks_launched_worker_stuck_at_trust_prompt_before_ticket_mutation(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 81, "title": "One", "severity": "p1", "kind": "bug", "column": "New",
                         "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        tmux_calls = []

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            return {"ok": True, "stdout": "launched", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            self.assertTrue(dry_run)
            self.assertEqual("batch-owner-demo", session)
            return {
                "ok": False,
                "rc": 3,
                "json": {
                    "status": "refused",
                    "reason": "directory trust prompt is visible",
                    "session": "batch-owner-demo",
                    "pane_state": "trust_prompt",
                },
                "stderr": "",
                "raw": "{}",
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [
                 {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
                 {"provider": "claude", "reason": "no tmux-managed claude pane found for /tmp/demo", "probe": {}},
             ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={
                 "id": 81, "title": "One", "column_id": 1, "category_id": 1, "is_active": 1, "swimlane_id": 1,
             }), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move:
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("launched session blocked by prompt", result["route"]["reason"])
        self.assertIn("trust prompt", result["route"]["detail"])
        self.assertEqual(["codex-existing", "codex-existing", "codex-existing", "codex", "stop"], [call[0] for call in tmux_calls])
        audit.assert_not_called()
        move.assert_not_called()
        leases = json.loads(pathlib.Path(self.cli.SUPERVISION_LEASES_PATH).read_text())
        self.assertEqual({}, leases["claims"])

    def test_supervise_batch_waits_through_post_launch_placeholder_then_blocks_trust_prompt(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 81, "title": "One", "severity": "p1", "kind": "bug", "column": "New",
                         "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        tmux_calls = []
        probes = iter([
            {
                "ok": False,
                "rc": 3,
                "json": {
                    "status": "refused",
                    "reason": "codex starter placeholder is visible",
                    "session": "batch-owner-demo",
                    "pane_state": "idle_empty_prompt",
                },
                "stderr": "",
                "raw": "{}",
            },
            {
                "ok": False,
                "rc": 4,
                "json": {
                    "status": "refused",
                    "reason": "directory trust prompt is visible",
                    "session": "batch-owner-demo",
                    "pane_state": "trust_prompt",
                },
                "stderr": "",
                "raw": "{}",
            },
        ])
        sleeps = []

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            return {"ok": True, "stdout": "launched", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            self.assertTrue(dry_run)
            self.assertEqual("batch-owner-demo", session)
            return next(probes)

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [
                 {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
                 {"provider": "claude", "reason": "no tmux-managed claude pane found for /tmp/demo", "probe": {}},
             ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli.time, "sleep", side_effect=lambda delay: sleeps.append(delay)), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={
                 "id": 81, "title": "One", "column_id": 1, "category_id": 1, "is_active": 1, "swimlane_id": 1,
             }), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move:
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("launched session blocked by prompt", result["route"]["reason"])
        self.assertEqual([self.cli.POST_LAUNCH_PROMPT_PROBE_INTERVAL], sleeps)
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_waits_through_empty_capture_then_blocks_trust_prompt(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 81, "title": "One", "severity": "p1", "kind": "bug", "column": "New",
                         "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        probes = iter([
            {
                "ok": False,
                "rc": 4,
                "json": {
                    "status": "refused",
                    "reason": "capture was empty",
                    "session": "batch-owner-demo",
                    "pane_state": "dead_or_unknown",
                },
                "stderr": "",
                "raw": "{}",
            },
            {
                "ok": False,
                "rc": 4,
                "json": {
                    "status": "refused",
                    "reason": "directory trust prompt is visible",
                    "session": "batch-owner-demo",
                    "pane_state": "trust_prompt",
                },
                "stderr": "",
                "raw": "{}",
            },
        ])
        sleeps = []

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-existing":
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
            return {"ok": True, "stdout": "launched", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [
                 {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
                 {"provider": "claude", "reason": "no tmux-managed claude pane found for /tmp/demo", "probe": {}},
             ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=lambda *a, **k: next(probes)), \
             mock.patch.object(self.cli.time, "sleep", side_effect=lambda delay: sleeps.append(delay)), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={
                 "id": 81, "title": "One", "column_id": 1, "category_id": 1, "is_active": 1, "swimlane_id": 1,
             }), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move:
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("launched session blocked by prompt", result["route"]["reason"])
        self.assertEqual([self.cli.POST_LAUNCH_PROMPT_PROBE_INTERVAL], sleeps)
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_blocks_if_codex_session_appears_before_launch(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 81, "title": "One", "severity": "p1", "kind": "bug", "column": "Triaging",
                         "column_id": 2, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}],
        }
        tmux_calls = []

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-existing":
                existing_count = sum(1 for call in tmux_calls if call[0] == "codex-existing")
                if existing_count < 3:
                    return {
                        "ok": False,
                        "stdout": "",
                        "stderr": "agent-tmux: no Codex tmux session found for workdir: /tmp/demo",
                        "rc": 1,
                        "argv": ["agent-tmux"] + argv,
                    }
                return {"ok": True, "stdout": "new-owner-demo", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}
            if argv[0] == "codex-latest":
                self.fail("supervise-batch must not consult codex-latest")
            return {"ok": True, "stdout": "launched", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 81, "title": "One", "column_id": 2, "category_id": 1, "is_active": 1}), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "audit_comment"), \
             mock.patch.object(self.cli, "move_task_to_column"):
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("existing session appeared before launch", result["route"]["reason"])
        self.assertEqual(["codex-existing", "codex-existing", "codex-existing"], [call[0] for call in tmux_calls])
        self.assertNotIn("codex", [call[0] for call in tmux_calls])

    def test_supervise_batch_contacts_one_worker_for_same_repo_queue(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                ticket_ids=[82, 83], repo="/tmp/demo", owner_id="test-supervisor", worker_session="safe-demo"),
        })
        group = {
            "project": "demo",
            "projects": ["demo"],
            "repo": "/tmp/demo",
            "tickets": [
                {"id": 82, "title": "First", "severity": "p1", "kind": "bug", "column": "New",
                 "column_id": 1, "swimlane_id": 9, "project": "demo", "repo": "/tmp/demo", "url": "u1"},
                {"id": 83, "title": "Second", "severity": "p2", "kind": "bug", "column": "Triaging",
                 "column_id": 2, "swimlane_id": 9, "project": "demo", "repo": "/tmp/demo", "url": "u2"},
            ],
        }
        sent_messages = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            self.assertFalse(dry_run)
            sent_messages.append(message)
            return {"ok": True, "rc": 0, "json": {"session": "safe-demo"}, "stderr": "", "raw": "{}"}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([
                 {"provider": "codex", "session": "safe-demo", "probe": {"ok": True}}
             ], [])), \
             mock.patch.object(self.cli, "_agent_contact", side_effect=fake_contact), \
             mock.patch.object(self.cli, "get_task_in_project", side_effect=lambda cfg, tid: {
                 "id": int(tid), "title": "First" if int(tid) == 82 else "Second",
                 "column_id": 1 if int(tid) == 82 else 2, "category_id": 1, "is_active": 1,
                 "swimlane_id": 9,
             }), \
             mock.patch.object(self.cli, "task_tags", side_effect=lambda cfg, tid: ["project:demo", "p1" if int(tid) == 82 else "p2"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", side_effect=lambda cfg, cid: "New" if int(cid) == 1 else "Triaging"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move:
            move.return_value = 2
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("routed", result["status"])
        self.assertEqual(1, len(sent_messages))
        self.assertIn("#82", sent_messages[0])
        self.assertIn("#83", sent_messages[0])
        self.assertEqual(2, audit.call_count)
        move.assert_called_once()
        self.assertEqual(9, move.call_args.args[1]["swimlane_id"])
        self.assertEqual("Triaging", result["tickets"][0]["column"])
        self.assertEqual(2, result["tickets"][0]["column_id"])

    def test_supervise_batch_revalidates_before_route_and_blocks_needs_human_race(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [
                {"id": 87, "title": "Race", "severity": "p1", "kind": "bug", "column": "New",
                 "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}
            ],
        }

        with mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 87, "title": "Race", "column_id": 4, "category_id": 1, "is_active": 1}), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="Needs human"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "_contactable_providers") as contactable, \
             mock.patch.object(self.cli, "_agent_tmux") as tmux, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move:
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("stale ticket state", result["route"]["reason"])
        self.assertIn("Needs human", result["route"]["detail"])
        contactable.assert_not_called()
        tmux.assert_not_called()
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_revalidates_before_send_and_blocks_needs_human_race(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                ticket_ids=[88], repo="/tmp/demo", owner_id="test-supervisor", worker_session="safe-demo"),
        })
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [
                {"id": 88, "title": "Race", "severity": "p1", "kind": "bug", "column": "New",
                 "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}
            ],
        }
        states = iter([
            {"id": 88, "title": "Race", "column_id": 1, "category_id": 1, "is_active": 1, "swimlane_id": 1},
            {"id": 88, "title": "Race", "column_id": 4, "category_id": 1, "is_active": 1, "swimlane_id": 1},
        ])

        with mock.patch.object(self.cli, "get_task_in_project", side_effect=lambda cfg, tid: next(states)), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", side_effect=lambda cfg, cid: "New" if int(cid) == 1 else "Needs human"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "_contactable_providers", return_value=([
                 {"provider": "codex", "session": "safe-demo", "probe": {"ok": True}}
             ], [])), \
             mock.patch.object(self.cli, "_agent_contact") as contact, \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move:
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("stale ticket state", result["route"]["reason"])
        self.assertIn("Needs human", result["route"]["detail"])
        contact.assert_not_called()
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_revalidates_before_move_and_does_not_move_human_ticket(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                ticket_ids=[89], repo="/tmp/demo", owner_id="test-supervisor", worker_session="safe-demo"),
        })
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [
                {"id": 89, "title": "Race", "severity": "p1", "kind": "bug", "column": "New",
                 "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}
            ],
        }
        states = iter([
            {"id": 89, "title": "Race", "column_id": 1, "category_id": 1, "is_active": 1, "swimlane_id": 1},
            {"id": 89, "title": "Race", "column_id": 1, "category_id": 1, "is_active": 1, "swimlane_id": 1},
            {"id": 89, "title": "Race", "column_id": 1, "category_id": 1, "is_active": 1, "swimlane_id": 1},
            {"id": 89, "title": "Race", "column_id": 4, "category_id": 1, "is_active": 1, "swimlane_id": 1},
        ])

        with mock.patch.object(self.cli, "get_task_in_project", side_effect=lambda cfg, tid: next(states)), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", side_effect=lambda cfg, cid: "New" if int(cid) == 1 else "Needs human"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "_contactable_providers", return_value=([
                 {"provider": "codex", "session": "safe-demo", "probe": {"ok": True}}
             ], [])), \
             mock.patch.object(self.cli, "_agent_contact", return_value={"ok": True, "rc": 0, "json": {"session": "safe-demo"}, "stderr": "", "raw": "{}"}), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column") as move:
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("post_route_blocked", result["status"])
        self.assertIn("post_route_warnings", result)
        self.assertIn("Needs human", result["post_route_warnings"][0])
        self.assertTrue(result["post_route_barrier_failed"])
        audit.assert_not_called()
        move.assert_not_called()

    def test_supervise_batch_move_failure_marks_post_route_blocked(self):
        self.write_supervision_claims({
            "claim-demo": self.active_supervision_claim(
                ticket_ids=[90], repo="/tmp/demo", owner_id="test-supervisor", worker_session="safe-demo"),
        })
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [
                {"id": 90, "title": "Move fails", "severity": "p1", "kind": "bug", "column": "New",
                 "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u"}
            ],
        }

        with mock.patch.object(self.cli, "get_task_in_project", return_value={
                 "id": 90, "title": "Move fails", "column_id": 1, "category_id": 1, "is_active": 1, "swimlane_id": 1,
             }), \
             mock.patch.object(self.cli, "task_tags", return_value=["project:demo", "p1"]), \
             mock.patch.object(self.cli, "resolve_repo_path", return_value="/tmp/demo"), \
             mock.patch.object(self.cli, "column_name", return_value="New"), \
             mock.patch.object(self.cli, "category_name", return_value="bug"), \
             mock.patch.object(self.cli, "_contactable_providers", return_value=([
                 {"provider": "codex", "session": "safe-demo", "probe": {"ok": True}}
             ], [])), \
             mock.patch.object(self.cli, "_agent_contact", return_value={"ok": True, "rc": 0, "json": {"session": "safe-demo"}, "stderr": "", "raw": "{}"}), \
             mock.patch.object(self.cli, "audit_comment") as audit, \
             mock.patch.object(self.cli, "move_task_to_column", side_effect=SystemExit("boom")) as move, \
             mock.patch("sys.stderr", new=io.StringIO()):
            result = self.cli._batch_route_group(
                {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(), group)

        self.assertEqual("post_route_blocked", result["status"])
        self.assertTrue(result["post_route_barrier_failed"])
        self.assertIn("could not be moved", result["post_route_warnings"][0])
        audit.assert_called_once()
        move.assert_called_once()

    def test_supervise_batch_post_route_warning_blocks_aggregate_ok(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 90, "title": "Blocked", "severity": "p1", "kind": "bug", "column": "New", "url": "u"}],
            "status": "post_route_blocked",
            "route": {"status": "routed", "mode": "contact", "provider": "codex", "session": "safe-demo"},
            "closeouts": {},
            "post_route_warnings": ["#90 project tag changed before ticket move"],
            "post_route_barrier_failed": True,
        }
        out = io.StringIO()

        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_batch_route_groups", return_value=[group]), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 90, "column_id": 5, "is_active": 0}), \
             mock.patch.object(self.cli, "column_name", return_value="Done"), \
             mock.patch.object(self.cli, "_closeout_report", return_value={"ok": True, "ticket": 90, "checks": []}), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(max_polls=0))

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual({"90"}, set(result["groups"][0]["closeouts"].keys()))
        self.assertEqual("closed", result["groups"][0]["ticket_status"]["90"]["status"])

    def test_supervise_batch_human_output_shows_post_route_warning(self):
        text = self.cli._format_supervise_batch_result({
            "ok": False,
            "dry_run": False,
            "ticket_count": 1,
            "skipped": [],
            "groups": [{
                "project": "demo",
                "repo": "/tmp/demo",
                "tickets": [{"id": 90, "title": "Blocked", "severity": "p1", "kind": "bug"}],
                "route": {"status": "routed", "mode": "contact", "provider": "codex", "session": "safe-demo"},
                "status": "post_route_blocked",
                "post_route_warnings": ["#90 project tag changed before ticket move"],
                "ticket_status": {},
            }],
        })
        self.assertIn("supervise-batch result: blocked", text)
        self.assertIn("group status: post_route_blocked", text)
        self.assertIn("post-route warning: #90 project tag changed before ticket move", text)

    def test_supervise_batch_blocking_skips_block_aggregate_ok(self):
        out = io.StringIO()
        skipped = [{"ticket_id": 93, "title": "No repo", "reason": "unresolved repo", "blocking": True}]

        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([], skipped)), \
             mock.patch.object(self.cli, "_batch_route_groups", return_value=[]), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(max_polls=0))

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(skipped, result["skipped"])

    def test_supervise_batch_nonblocking_skips_do_not_block_empty_filtered_batch(self):
        out = io.StringIO()
        skipped = [{"ticket_id": 94, "title": "P3", "reason": "p3 excluded"}]

        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([], skipped)), \
             mock.patch.object(self.cli, "_batch_route_groups", return_value=[]), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(max_polls=0))

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(skipped, result["skipped"])

    def test_supervise_batch_holds_repo_lock_until_polling_finishes(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 95, "title": "Open", "severity": "p1", "kind": "bug", "column": "Triaging", "url": "u"}],
        }
        routed = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": group["tickets"],
            "route": {"status": "routed", "mode": "contact", "provider": "codex", "session": "safe-demo"},
            "status": "routed",
            "closeouts": {},
        }
        events = []
        out = io.StringIO()

        def fake_lock(repo):
            events.append(("lock", repo))
            return 123

        def fake_unlock(fd):
            events.append(("unlock", fd))

        def fake_poll(cfg, args, results):
            events.append(("poll", [result.get("_repo_lock_fd") for result in results]))
            self.assertNotIn(("unlock", 123), events)
            return {95: {"status": "open", "column": "Triaging"}}

        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_batch_route_group", return_value=routed), \
             mock.patch.object(self.cli, "_lock_batch_repo", side_effect=fake_lock), \
             mock.patch.object(self.cli, "_unlock_batch_repo", side_effect=fake_unlock), \
             mock.patch.object(self.cli, "_batch_poll_closeouts", side_effect=fake_poll), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(max_polls=0))

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual([("lock", "/tmp/demo"), ("poll", [123]), ("unlock", 123)], events)
        self.assertNotIn("_repo_lock_fd", result["groups"][0])

    def test_supervise_batch_prompt_matches_closeout_flags(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 96, "title": "Prompt", "severity": "p1", "kind": "bug", "column": "Triaging", "url": "u"}],
        }

        default_message = self.cli._batch_supervision_message(
            {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, group, self.batch_args())
        strict_message = self.cli._batch_supervision_message(
            {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, group,
            self.batch_args(strict_closeout=True, require_install=True))

        self.assertIn("`agent-ticket closeout-check <id>`", default_message)
        self.assertNotIn("`agent-ticket closeout-check <id> --strict`", default_message)
        self.assertIn("`agent-ticket closeout-check <id> --strict --require-install`", strict_message)

    def test_supervise_batch_rejects_negative_poll_options_before_routing(self):
        cases = [
            ("max_polls", self.batch_args(max_polls=-1), "--max-polls must be >= 0"),
            ("poll_interval", self.batch_args(poll_interval=-0.01), "--poll-interval must be >= 0"),
        ]
        for name, args, expected in cases:
            with self.subTest(name=name):
                with mock.patch.object(self.cli, "_batch_collect_ticket_groups") as collect, \
                     mock.patch.object(self.cli, "_batch_route_groups") as route, \
                     mock.patch("sys.stdout", new=io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        self.cli.cmd_supervise_batch(
                            {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, args)
                self.assertIn(expected, str(caught.exception))
                collect.assert_not_called()
                route.assert_not_called()

    def test_supervise_batch_revalidator_blocks_excluded_state_matrix(self):
        base_group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{
                "id": 91, "title": "Race", "severity": "p1", "kind": "bug", "column": "New",
                "column_id": 1, "swimlane_id": 1, "project": "demo", "repo": "/tmp/demo", "url": "u",
            }],
        }
        cases = [
            ("closed before send", {"is_active": 0, "column_id": 1, "category_id": 1}, ["project:demo", "p1"], "/tmp/demo", "New", "closed before send"),
            ("project tag lost", {"is_active": 1, "column_id": 1, "category_id": 1}, ["p1"], "/tmp/demo", "New", "project tag changed before send"),
            ("multiple project tags", {"is_active": 1, "column_id": 1, "category_id": 1}, ["project:demo", "project:other", "p1"], "/tmp/demo", "New", "project tag changed before send"),
            ("repo changed", {"is_active": 1, "column_id": 1, "category_id": 1}, ["project:demo", "p1"], "/tmp/other", "New", "resolved repo changed before send"),
            ("p3 excluded", {"is_active": 1, "column_id": 1, "category_id": 1}, ["project:demo", "p3"], "/tmp/demo", "New", "became p3 before send"),
            ("column filter mismatch", {"is_active": 1, "column_id": 2, "category_id": 1}, ["project:demo", "p1"], "/tmp/demo", "Triaging", "no longer matches column filter"),
        ]
        for _name, task, tags, repo, column, expected in cases:
            with self.subTest(_name):
                task = {"id": 91, "title": "Race", "swimlane_id": 1, **task}
                args = self.batch_args(column=["New"]) if "column filter" in _name else self.batch_args()
                with mock.patch.object(self.cli, "get_task_in_project", return_value=task), \
                     mock.patch.object(self.cli, "task_tags", return_value=tags), \
                     mock.patch.object(self.cli, "resolve_repo_path", return_value=repo), \
                     mock.patch.object(self.cli, "column_name", return_value=column), \
                     mock.patch.object(self.cli, "category_name", return_value="bug"):
                    refreshed, reason = self.cli._batch_revalidate_group_for_route(
                        {"endpoint": "http://kanboard.invalid/jsonrpc.php"}, args, base_group, context="send")
                self.assertIsNone(refreshed)
                self.assertIn(expected, reason)

    def test_supervise_batch_aggregates_closeout_per_ticket(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [
                {"id": 84, "title": "First", "severity": "p1", "kind": "bug", "column": "Triaging", "url": "u1"},
                {"id": 85, "title": "Second", "severity": "p2", "kind": "bug", "column": "Triaging", "url": "u2"},
            ],
        }
        routed = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": group["tickets"],
            "route": {"status": "routed", "mode": "contact", "provider": "codex", "session": "safe-demo"},
            "status": "routed",
            "closeouts": {},
        }
        out = io.StringIO()

        def fake_closeout(cfg, tid, **kwargs):
            return {"ok": True, "ticket": int(tid), "checks": []}

        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_batch_route_groups", return_value=[routed]), \
             mock.patch.object(self.cli, "get_task_in_project", side_effect=lambda cfg, tid: {"id": tid, "column_id": 5, "is_active": 0}), \
             mock.patch.object(self.cli, "column_name", return_value="Done"), \
             mock.patch.object(self.cli, "_closeout_report", side_effect=fake_closeout), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(max_polls=0))

        result = json.loads(out.getvalue())
        self.assertTrue(result["ok"])
        closeouts = result["groups"][0]["closeouts"]
        self.assertEqual({"84", "85"}, set(closeouts.keys()))
        self.assertEqual("closed", result["groups"][0]["ticket_status"]["84"]["status"])

    def test_supervise_batch_needs_human_blocks_aggregate_ok(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 86, "title": "Blocked", "severity": "p1", "kind": "bug", "column": "Triaging", "url": "u"}],
        }
        routed = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": group["tickets"],
            "route": {"status": "routed", "mode": "contact", "provider": "codex", "session": "safe-demo"},
            "status": "routed",
            "closeouts": {},
        }
        out = io.StringIO()

        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_batch_route_groups", return_value=[routed]), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 86, "column_id": 4, "is_active": 1}), \
             mock.patch.object(self.cli, "column_name", return_value="Needs human"), \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(max_polls=0))

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("needs_human", result["groups"][0]["ticket_status"]["86"]["status"])

    def test_supervise_batch_open_after_poll_limit_blocks_aggregate_ok(self):
        group = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": [{"id": 92, "title": "Still open", "severity": "p1", "kind": "bug", "column": "Triaging", "url": "u"}],
        }
        routed = {
            "project": "demo",
            "repo": "/tmp/demo",
            "tickets": group["tickets"],
            "route": {"status": "routed", "mode": "contact", "provider": "codex", "session": "safe-demo"},
            "status": "routed",
            "closeouts": {},
        }
        out = io.StringIO()

        with mock.patch.object(self.cli, "_batch_collect_ticket_groups", return_value=([group], [])), \
             mock.patch.object(self.cli, "_batch_route_groups", return_value=[routed]), \
             mock.patch.object(self.cli, "get_task_in_project", return_value={"id": 92, "column_id": 2, "is_active": 1}), \
             mock.patch.object(self.cli, "column_name", return_value="Triaging"), \
             mock.patch.object(self.cli, "_closeout_report") as closeout, \
             mock.patch("sys.stdout", new=out):
            self.cli.cmd_supervise_batch({"endpoint": "http://kanboard.invalid/jsonrpc.php"}, self.batch_args(max_polls=0))

        result = json.loads(out.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual("open", result["groups"][0]["ticket_status"]["92"]["status"])
        self.assertEqual({}, result["groups"][0]["closeouts"])
        closeout.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
