#!/usr/bin/env python3
"""bootstrap-board.py — idempotently set up the Kanboard "Agent Tickets" board
to match what the `agent-ticket` CLI expects: a project, the standard columns,
the standard "kind" categories. Writes the resolved project_id into the config.

Run after Kanboard is up and ~/.config/agent-tickets/config.json has a real
application API token (Settings -> API -> "API token" for the `jsonrpc` user).
Safe to re-run (called automatically by install.sh).
"""
import base64, json, os, sys, tempfile, urllib.request, urllib.error

CONFIG_PATH = os.path.expanduser("~/.config/agent-tickets/config.json")
PROJECT_NAME = "Agent Tickets — app/tool usage issues"
PROJECT_DESC = ("Problems AI agents hit while USING / OPERATING the users apps and tools: "
                "crashes, wrong behavior, broken CLI flags, misleading docs, harness/runtime "
                "misbehavior, friction. NOT a development backlog and NOT an agents in-flight TODO list.")
COLUMNS = ["New", "Triaging", "Agent working", "Needs human", "Done"]
# Fresh-Kanboard default columns that aren't canonical — the ONLY columns we will
# ever rename, and only on a project that currently has zero tasks. User-added
# columns are never touched.
KANBOARD_DEFAULT_COLUMNS = ["Backlog", "Ready", "Work in progress"]
CATEGORIES = ["bug", "friction", "request", "blocker", "question", "idea", "regression"]
PLACEHOLDER_TOKEN = "PUT-YOUR-KANBOARD-API-TOKEN-HERE"


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            sys.exit("bootstrap-board: %s is not valid JSON (%s) — fix it and re-run." % (CONFIG_PATH, e))
    cfg["endpoint"] = os.environ.get("AGENT_TICKETS_ENDPOINT") or cfg.get("endpoint", "http://127.0.0.1:8765/jsonrpc.php")
    cfg["token"] = os.environ.get("AGENT_TICKETS_TOKEN") or cfg.get("token")
    cfg["project_id"] = cfg.get("project_id")
    if not cfg.get("token") or cfg["token"] == PLACEHOLDER_TOKEN:
        sys.exit("bootstrap-board: no real API token in %s (or AGENT_TICKETS_TOKEN) — get the application "
                 "API token from Kanboard (Settings -> API -> 'API token') and put it there, then re-run." % CONFIG_PATH)
    return cfg


def rpc(cfg, method, params=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    auth = base64.b64encode(("jsonrpc:" + cfg["token"]).encode()).decode()
    req = urllib.request.Request(cfg["endpoint"], data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
    except urllib.error.URLError as e:
        sys.exit("bootstrap-board: cannot reach Kanboard at %s (%s) — is the container up? "
                 "`docker compose up -d`" % (cfg["endpoint"], e))
    if "error" in body:
        sys.exit("bootstrap-board: API error on %s: %s" % (method, json.dumps(body["error"])))
    return body.get("result")


def _must(res, what):
    """Kanboard returns `false` (not an HTTP error) when a mutation fails; bail
    loudly rather than continuing with a bogus value (e.g. int(False) == 0)."""
    if res is False or res is None:
        sys.exit("bootstrap-board: %s failed (Kanboard returned %r)." % (what, res))
    return res


def ensure_project(cfg):
    """Identify the agent-tickets project by EXACT name. If a project_id is
    configured but that project's name isn't PROJECT_NAME, ABORT (don't silently
    create a new board — that could clobber an unrelated project or strand the
    real board's tickets). Otherwise: use the configured id; else the sole project
    named PROJECT_NAME; else create one. >1 named that aborts."""
    projects = rpc(cfg, "getAllProjects") or []
    by_id = {int(p["id"]): p for p in projects}
    pid_cfg = cfg.get("project_id")
    if pid_cfg is not None and int(pid_cfg) in by_id:
        name = by_id[int(pid_cfg)].get("name")
        if name != PROJECT_NAME:
            sys.exit("bootstrap-board: configured project_id %s is project %r, not %r.\n"
                     "  If you renamed the board, rename it back. If project_id is stale/wrong, fix it.\n"
                     "  To have bootstrap resolve/create the board by name, remove 'project_id' from %s.\n"
                     "  (Not auto-resolving — that could clobber an unrelated project or strand tickets.)"
                     % (pid_cfg, name, PROJECT_NAME, CONFIG_PATH))
        pid = int(pid_cfg)
        print("  project: using configured #%d" % pid)
    else:
        exact = [p for p in projects if p.get("name") == PROJECT_NAME]
        if len(exact) == 1:
            pid = int(exact[0]["id"]); print("  project: found #%d (exact name)" % pid)
        elif len(exact) > 1:
            sys.exit("bootstrap-board: multiple projects named %r — resolve the duplicate in the Kanboard UI." % PROJECT_NAME)
        else:
            pid = int(_must(rpc(cfg, "createProject", {"name": PROJECT_NAME, "description": PROJECT_DESC}), "createProject"))
            print("  project: created #%d %r" % (pid, PROJECT_NAME))
    _must(rpc(cfg, "updateProject", {"project_id": pid, "id": pid, "name": PROJECT_NAME, "description": PROJECT_DESC}),
          "updateProject #%d" % pid)
    return pid


def _columns_with_tasks(cfg, pid):
    cols = set()
    for status_id in (1, 0):  # open, then closed
        for t in (rpc(cfg, "getAllTasks", {"project_id": pid, "status_id": status_id}) or []):
            cols.add(int(t["column_id"]))
    return cols


def ensure_columns(cfg, pid):
    """Reconcile columns by TITLE (the CLI matches column titles case-insensitively):
      * abort on duplicate titles (case-insensitive);
      * fix the casing of any canonical column present with the wrong case;
      * create missing canonical columns — or, ONLY on a project that currently has
        zero tasks, rename one of Kanboard's stock default columns (Backlog/Ready/
        Work in progress) instead; never rename anything once tickets exist;
      * normalise order (canonical first; extras keep trailing)."""
    canonical = list(COLUMNS)
    canonical_lower = {c.lower(): c for c in COLUMNS}
    current = sorted(rpc(cfg, "getColumns", {"project_id": pid}) or [], key=lambda c: c["position"])
    low = [c["title"].lower() for c in current]
    dup_low = {x for x in low if low.count(x) > 1}
    if dup_low:
        dups = sorted({c["title"] for c in current if c["title"].lower() in dup_low})
        sys.exit("bootstrap-board: project #%d has duplicate column titles (case-insensitive): %s. "
                 "Rename or merge the duplicates in the Kanboard UI, then re-run." % (pid, ", ".join(dups)))
    for c in current:                              # fix wrong-cased canonical columns ("new" -> "New")
        canon = canonical_lower.get(c["title"].lower())
        if canon and c["title"] != canon:
            _must(rpc(cfg, "updateColumn", {"column_id": int(c["id"]), "title": canon}), "rename column to %r" % canon)
            c["title"] = canon
    have = {c["title"] for c in current}
    project_has_tasks = bool(_columns_with_tasks(cfg, pid))
    spare = [] if project_has_tasks else [c for c in current if c["title"] in KANBOARD_DEFAULT_COLUMNS]
    actions = []
    for want in COLUMNS:
        if want in have:
            continue
        if spare:
            c = spare.pop(0)
            _must(rpc(cfg, "updateColumn", {"column_id": int(c["id"]), "title": want}), "rename %r -> %r" % (c["title"], want))
            actions.append("%s->%s" % (c["title"], want)); have.add(want)
        else:
            _must(rpc(cfg, "addColumn", {"project_id": pid, "title": want}), "addColumn %r" % want)
            actions.append("+%s" % want); have.add(want)
    cols = {c["title"]: int(c["id"]) for c in (rpc(cfg, "getColumns", {"project_id": pid}) or [])}
    for pos, title in enumerate(COLUMNS, start=1):
        if title in cols:
            _must(rpc(cfg, "changeColumnPosition", {"project_id": pid, "column_id": cols[title], "position": pos}),
                  "reorder column %r" % title)
    extra = [t for t in cols if t not in canonical]
    note = ("; extra columns left in place: " + ", ".join(extra)) if extra else ""
    print("  columns: %s%s" % (", ".join(actions) if actions else "already " + " -> ".join(COLUMNS), note))


def ensure_categories(cfg, pid):
    have = {c["name"].lower() for c in (rpc(cfg, "getAllCategories", {"project_id": pid}) or [])}
    added = []
    for name in CATEGORIES:
        if name.lower() not in have:
            _must(rpc(cfg, "createCategory", {"project_id": pid, "name": name}), "createCategory %r" % name)
            added.append(name)
    print("  categories: %s" % ("added " + ", ".join(added) if added else "all present"))


def write_project_id(cfg, pid):
    data = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    changed = data.get("project_id") != pid
    if changed:
        data["project_id"] = pid
        d = os.path.dirname(CONFIG_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".config-", suffix=".tmp")  # mkstemp => mode 0600
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp, CONFIG_PATH)  # atomic; the new file is already 0600
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError as e:
        sys.stderr.write("bootstrap-board: warning: could not chmod 600 %s (%s)\n" % (CONFIG_PATH, e))
    print("  config: project_id %s %d" % ("set ->" if changed else "already", pid))


def main():
    cfg = load_config()
    print("bootstrap-board: %s" % cfg["endpoint"])
    pid = ensure_project(cfg)
    ensure_columns(cfg, pid)
    ensure_categories(cfg, pid)
    write_project_id(cfg, pid)
    print("bootstrap-board: done. Smoke test: agent-ticket columns")


if __name__ == "__main__":
    main()
