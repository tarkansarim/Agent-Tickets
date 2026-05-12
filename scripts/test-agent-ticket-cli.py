#!/usr/bin/env python3
"""Focused tests for agent-ticket CLI behavior that does not need Kanboard."""
import argparse
import io
import json
import pathlib
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
            "json": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

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

    def test_supervise_dry_run_uses_codex_latest_without_contactable_session(self):
        task = {"id": 47, "title": "Fix demo", "column_id": 1, "swimlane_id": 1, "is_active": 1}

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {"ok": False, "rc": 3, "json": {"reason": "no pane"}, "stderr": "", "raw": ""}

        def fake_tmux(argv, timeout=25):
            if argv[0] == "codex-latest":
                return {"ok": True, "rc": 0, "stdout": "thread\tname\tdate\t/path.jsonl", "stderr": "", "argv": ["agent-tmux"] + argv}
            if argv[0] == "codex-existing":
                return {"ok": False, "rc": 1, "stdout": "", "stderr": "none", "argv": ["agent-tmux"] + argv}
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
        self.assertEqual("resume-latest", result["route"]["mode"])
        self.assertEqual("owner-demo-47", result["route"]["session"])

    def test_supervise_launch_uses_new_ticket_scoped_session_not_existing_session(self):
        task = {"id": 47, "title": "Fix demo", "column_id": 1, "swimlane_id": 1, "is_active": 1}
        tmux_calls = []

        def fake_contact(repo, provider, message, dry_run=False, session=None):
            return {"ok": False, "rc": 3, "json": {"reason": "no pane"}, "stderr": "", "raw": ""}

        def fake_tmux(argv, timeout=25):
            tmux_calls.append(argv)
            if argv[0] == "codex-latest":
                return {"ok": True, "rc": 0, "stdout": "thread\tname\tdate\t/path.jsonl", "stderr": "", "argv": ["agent-tmux"] + argv}
            if argv[0] == "codex-existing":
                return {"ok": True, "rc": 0, "stdout": "owner-demo", "stderr": "", "argv": ["agent-tmux"] + argv}
            if argv[0] == "codex-resume-latest" and argv[1] == "owner-demo-47":
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
        self.assertEqual("codex-resume-latest", tmux_calls[-1][0])
        self.assertEqual("owner-demo-47", tmux_calls[-1][1])

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
        group = {
            "project": "demo",
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

        def fake_tmux(argv, timeout=25):
            self.assertEqual("codex-existing", argv[0])
            return {"ok": True, "stdout": "old-codex", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([
            {"provider": "claude", "session": "safe-claude", "probe": {"ok": True}},
        ], [
            {"provider": "codex", "reason": "no tmux-managed codex pane found for /tmp/demo", "probe": {}},
        ])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux) as tmux:
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("existing session unsafe", route["reason"])
        self.assertEqual("old-codex", route["session"])
        tmux.assert_called_once()

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

    def test_supervise_batch_blocks_empty_successful_latest_lookup(self):
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
            self.assertEqual("codex-latest", argv[0])
            return {"ok": True, "stdout": "", "stderr": "", "rc": 0, "argv": ["agent-tmux"] + argv}

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux latest lookup failed", route["reason"])

    def test_supervise_batch_blocks_malformed_successful_latest_lookup(self):
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
            self.assertEqual("codex-latest", argv[0])
            return {
                "ok": True,
                "stdout": "malformed-success-line-without-tabs",
                "stderr": "",
                "rc": 0,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux latest lookup failed", route["reason"])

    def test_supervise_batch_rejects_no_session_text_with_wrong_latest_rc(self):
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
            self.assertEqual("codex-latest", argv[0])
            return {
                "ok": False,
                "stdout": "",
                "stderr": "agent-tmux: no Codex session found for workdir: /tmp/demo",
                "rc": 2,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux latest lookup failed", route["reason"])

    def test_supervise_batch_rejects_no_latest_session_text_for_wrong_repo(self):
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
            self.assertEqual("codex-latest", argv[0])
            return {
                "ok": False,
                "stdout": "",
                "stderr": "agent-tmux: no Codex session found for workdir: /tmp/other",
                "rc": 1,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux latest lookup failed", route["reason"])

    def test_supervise_batch_blocks_latest_success_with_extra_line(self):
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
            self.assertEqual("codex-latest", argv[0])
            return {
                "ok": True,
                "stdout": "Thread\tid\t2026-05-12T00:00:00Z\t/tmp/session.jsonl\nwarning",
                "stderr": "",
                "rc": 0,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux latest lookup failed", route["reason"])

    def test_supervise_batch_blocks_latest_success_with_stderr_warning(self):
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
            self.assertEqual("codex-latest", argv[0])
            return {
                "ok": True,
                "stdout": "Thread\tid\t2026-05-12T00:00:00Z\t/tmp/session.jsonl",
                "stderr": "warning: stale index",
                "rc": 0,
                "argv": ["agent-tmux"] + argv,
            }

        with mock.patch.object(self.cli, "_contactable_providers", return_value=([], [])), \
             mock.patch.object(self.cli, "_agent_tmux", side_effect=fake_tmux):
            route = self.cli._batch_select_route(args, group, "message")

        self.assertEqual("blocked", route["status"])
        self.assertEqual("agent-tmux latest lookup failed", route["reason"])

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
        contactable = [
            {"provider": "codex", "session": "safe-codex", "probe": {"ok": True}},
            {"provider": "claude", "session": "safe-claude", "probe": {"ok": True}},
        ]

        with mock.patch.object(self.cli, "_contactable_providers", return_value=(contactable, [])):
            route = self.cli._batch_select_route(args, group, "message")

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
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
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
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "agent-tmux: no Codex session found for workdir: /tmp/demo",
                    "rc": 1,
                    "argv": ["agent-tmux"] + argv,
                }
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
        self.assertEqual(["codex-existing", "codex-latest", "codex-existing", "codex-latest", "codex-existing"],
                         [call[0] for call in tmux_calls])
        self.assertNotIn("codex", [call[0] for call in tmux_calls])

    def test_supervise_batch_contacts_one_worker_for_same_repo_queue(self):
        group = {
            "project": "demo",
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
