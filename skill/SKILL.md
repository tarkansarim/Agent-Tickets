---
name: agent-tickets
description: "File and manage tickets in the local Kanboard board for problems agents hit while USING the user's apps/tools (crashes, wrong behavior, broken CLI flags, bad docs, harness/runtime misbehavior, friction). FILE a ticket if your job is to report findings back (test/QA/review/exploration worker); if you OWN the project and can fix it now, just fix it — don't file — but do read/triage/close tickets others filed for you. You can also dispatch a filed ticket to the owning repo's agent. NOT for development backlog, feature planning, or your in-flight TODO. Triggers — file a ticket, report this issue, open a ticket, log this for later, check the ticket board, dispatch a ticket to its owner agent, a test subagent reporting back, an agent blocked while running/using something."
---

# Agent Tickets

A **local Kanboard** at **http://localhost:8765** is the shared queue for problems AI agents run into **while using the user's apps, tools, CLIs, and harnesses**. The user triages tickets in that web UI; agents file and update them via the `agent-ticket` CLI. (Local only — no cloud, no account, no git. Data in `~/kanboard-data/`.)

## What this board IS for

You're *operating* something — a tool, a CLI, a built app, the harness, an MCP server, a script — and it goes wrong. File a ticket so the user (or a later agent) can fix it:
- a tool/app **crashed** or produced wrong output
- a **CLI flag / command doesn't work** as documented (or the docs are wrong/missing)
- the **harness / runtime / MCP server misbehaved** — bad responses, hangs, wrong state
- a **blocker** while using something: missing creds, broken dependency, ambiguous behavior you need a human to resolve
- recurring **friction** in a workflow worth smoothing
- a **regression** — something that used to work and now doesn't
- a **request / idea** to improve a tool you were using

## What this board is NOT for

- ❌ **Development backlog / feature planning** — "implement feature X in project Y", "refactor module Z". That's not a usage issue; don't put it here.
- ❌ **Your in-flight TODO** for the current task — use your normal task tracking for work you're actively doing. Tickets are for *handoff*: things you can't or shouldn't fix right now.
- ❌ Generic notes-to-self. A ticket is a reproducible problem or a concrete request, with enough detail for someone else to act on.

Rule of thumb: **were you *using* something when it went wrong? → ticket.** Were you *building* something and just have more to build? → not a ticket.

## Who files vs. who consumes — your role matters

The agent that *finds* an issue and the agent that *fixes* it are usually different here:

- **If your job is to BUILD or FIX a project** (you're the owner/primary agent for it) and you can resolve the issue now — **fix it. Do not file a ticket.** Filing yourself a note you're about to action is just noise. You *should* still **read, triage, and close** tickets others filed for you — that queue is your inbox: `agent-ticket list --project <yourproject>`.
- **If your job is to REPORT** — you're a test / QA / exploration / review worker exercising someone else's built app and handing findings back — **file a ticket** for each issue instead of touching the code. Tag it `source:test-run` (or `source:review`, etc.) so the owner can tell worker-filed tickets apart, and always set `--project` and `--agent`.
- **If it's out of your scope/authority** regardless of role (needs a human decision, touches a project you don't own, requires creds you don't have) — file it, column `Needs human` if a person must look first.

You always know which side of this you're on from your task. When in doubt: are you handing findings *back to someone*? → file. Are you the one who'd do the fix? → just fix.

### Dispatching test/QA subagents (for builder agents)

When you (a builder) spawn subagents to test a built app, put them in **report mode** so they file tickets instead of fixing. Paste something like this into the subagent's prompt:

> You are in **test/report mode**. Exercise <the app/tool> as instructed. For every bug, crash, wrong behavior, or friction you hit, file a ticket with the `agent-tickets` skill:
> `agent-ticket new --title "..." --kind bug --severity p2 --project <PROJECT> --agent <your-name> --tag source:test-run --body 'what you did / expected / actual'`
> Do **NOT** attempt fixes — your job is to find and report, not to change code. Report the ticket IDs back to me when done.

Then drain the queue yourself: `agent-ticket list --project <PROJECT> --kind bug` → fix → `agent-ticket close <id>`.

## Dispatching a filed ticket to its owner agent

When you hit an issue in **another** repo, file it (`agent-ticket new --project X …`) and optionally **dispatch** it — `agent-ticket dispatch <id>` (or `agent-ticket new … --dispatch`). Dispatch is **always explicit**; filing a ticket never auto-pings anyone. What it does:

1. Resolves `project:<name>` → the source repo path (`~/Dropbox/work/MyTools/<name>` by default; `repo_roots` in config is searched in order). The `--project` value **must be the exact repo directory name** or dispatch can't resolve it.
2. Tries to contact the repo's owner agent via `agent-contact` (which handles Codex *and* Claude tmux-managed workers, and refuses unsafe targets: attached / busy / pending-composer-text / dead / multiple-candidate sessions). It needs `AGENT_CONTACT_TRUSTED_PROVIDER_ROOTS` / `AGENT_CONTACT_TRUSTED_LAUNCHER_ROOTS` set in the environment (one-time setup via `agent-contact trust-roots …`); if they're not set, dispatch degrades to an audit comment and exits cleanly. With `--dry-run`, it resolves/probes the same route but does not write comments, move tickets, or contact an agent.
3. **Exactly one** contactable owner session → sends a "please triage this ticket; move it to 'Agent working' if you're taking it; fix from SOURCE not the installed copy; validate; comment evidence; then `agent-ticket close <id>`" message, records a `dispatched to <provider> session <name> at <utc>` comment, and moves the ticket `New → Triaging`.
4. **Zero / two-or-more / unresolvable repo / no trusted-roots** → it does *not* guess: it records a comment explaining why and exits 0 (the ticket stays where it is; the notify hook will surface it to the owner at session start anyway).

Refusals: `dispatch` **hard-errors on a `Needs human` ticket** (a person must route those), **skips p3 by default** (low priority should not interrupt an owner agent), and **won't re-dispatch** a ticket already in `Agent working`/`Done`/already-dispatched. It **never tells the woken worker to run `dispatch` itself** — no fan-out.

So the full cross-repo loop: agent in repo A hits a bug in repo B → `agent-ticket new --project B … [--dispatch]` → B's owner agent gets pinged (or the notify hook surfaces it next session) → owner moves it to `Agent working`, **fixes from B's source tree (not any installed copy)**, validates, `agent-ticket comment <id> 'evidence…'`, `agent-ticket close <id>`.

## CLI: `agent-ticket`

At `~/.local/bin/agent-ticket` (on PATH). Config (endpoint + application API token + project id) in `~/.config/agent-tickets/config.json`. `--json` works before or after the subcommand for machine-readable output (`agent-ticket --json list` or `agent-ticket list --json`).

```bash
# File a ticket — describe what you were using, what you did, what you expected, what happened
agent-ticket new --title "CudaGroomTool2 segfaults loading .groom over 2GB" \
  --kind bug --severity p1 --project CudaGroomTool2 --agent claude \
  --body 'While using CudaGroomTool2 to load a scene:\n1. ./groom open big.groom  (2.3 GB)\n→ segfault in GroomIO::mmap.\nExpected: loads or a clean error. Out of scope for my current task.'
# (if it warns "⚠ possible duplicate of #N" — comment on #N instead, or re-run with --force to file anyway)

# File AND immediately ping the owning repo's agent (best-effort; see "Dispatching a filed ticket" below):
agent-ticket new --title "..." --kind bug --severity p2 --project sortie2 --agent claude --body '...' --dispatch

# Ping the owner agent for an already-filed ticket:
agent-ticket dispatch 42
agent-ticket dispatch 42 --dry-run

# Verify where the installed CLI came from before source-owned maintenance:
agent-ticket source-info

# Query
agent-ticket list                       # open tickets
agent-ticket list --column "Needs human"
agent-ticket list --kind blocker --project sortie2
agent-ticket list --all                 # include closed
agent-ticket show 42

# Update
agent-ticket comment 42 "Repro'd on the Linux build too."
agent-ticket move 42 "Agent working"    # columns: New, Triaging, "Agent working", "Needs human", Done
agent-ticket tag 42 --add agent:codex --remove p3
agent-ticket close 42                   # moves to Done, then closes
agent-ticket reopen 42                  # reopens to Triaging by default, with audit comment
agent-ticket closeout-check 42 --strict
agent-ticket supervise 42 --full-permission --max-polls 120
agent-ticket supervise-batch --dry-run
agent-ticket supervise-batch --full-permission --max-polls 120
agent-ticket supervise 42 --watch-origin --origin-provider codex --full-permission
agent-ticket callbacks --pending --repo <repo>
agent-ticket callbacks retry ticket:42:closed:1
agent-ticket callbacks ack ticket:42:closed:1
agent-ticket columns
```

### Fields / conventions
- `--kind`: `bug`, `friction`, `request`, `blocker`, `question`, `idea`, `regression`
- `--severity`: `p1` (urgent / blocking) … `p3` (low) — stored as a tag
- `--project`: the app/tool/codebase the issue is about (directory name, e.g. `sortie2`, `CudaGroomTool2`) — stored as tag `project:<name>`
- `--agent`: which agent filed it (`claude`, `codex`, …) — stored as tag `agent:<name>`
- `--body`: markdown; `\n` and `\t` are expanded to real newlines/tabs
- `--column`: where it lands (default `New`). Use `Needs human` if it needs a user decision before any agent should touch it.
- `--tag`: any extra free-form tag (repeatable). Convention: `source:test-run` / `source:review` when a test/QA/review worker files on behalf of a builder, so the owner can spot worker-filed tickets.

### Board columns (lifecycle)
`New` → `Triaging` → `Agent working` → `Needs human` → `Done`. Move to `Agent working` when you start on it, `Needs human` when blocked on a user decision, and run `agent-ticket close <id>` when finished; `close` moves the ticket to `Done` before closing it. `reopen` moves reopened tickets to `Triaging` by default and records an audit comment; use `--column New` or another live column when that is the intended landing point.

## Notes
- **You may be told about open tickets automatically.** A notify hook (Claude Code: `SessionStart` + `UserPromptSubmit`; Codex: `SessionStart` + `Stop`) surfaces `🎫 Open/New agent-ticket(s) for <repo>: …` for the repo you're working in — at session start, and when a new one appears. When you see that, triage per the role rules above (fix-in-place if you own the repo, otherwise act on / hand off). You can always run `agent-ticket list --project <repo>` yourself too.
- `agent-ticket dispatch` is **best-effort and explicit** — it records an audit comment on the ticket either way (`dispatched to … session …`, or the reason it didn't), except when `--dry-run` is used. "0 contactable / 2+ providers / no trusted-roots / unresolvable repo" all → comment + exit 0, not an error. p3 tickets are not dispatched by default. `repo_roots` (config) is the list of dirs searched for the `<project>` subdir; `--project` must be the exact repo directory name. Dispatch never instructs the woken worker to dispatch — no fan-out.
- `agent-ticket supervise <id>` is the deterministic owner-agent loop for higher-stakes cross-repo tickets. It resolves the owner repo, selects a contactable/latest Codex owner lane when available, sends or launches through guarded `agent-contact`/`agent-tmux`, can use visible full-permission Codex flags with `--full-permission`, polls ticket state, and runs closeout checks after closure. Existing tmux panes are reused only through guarded `agent-contact`; Codex launch/resume fallback uses a new deterministic ticket-scoped session name from `--session-prefix`, the project tag, and the ticket id unless `--session` is supplied. Use `--dry-run` first when checking route selection. Refusal/tooling states are reported and can file a local blocker unless `--no-tool-ticket` is set. Live `dispatch`, `supervise`, and `supervise-batch` contact/probe paths need `AGENT_CONTACT_TRUSTED_PROVIDER_ROOTS` and `AGENT_CONTACT_TRUSTED_LAUNCHER_ROOTS` in the environment.
- `agent-ticket supervise-batch` is the user-level board drain for multiple open tickets. It scans open non-`Done` tickets by default, skips `p3` and `Needs human` unless explicitly included, requires one exact `project:<repo>` tag per ticket, resolves repos with the same `repo_roots` rule, and groups by canonical resolved repo path so aliases for one source tree still route to one owner worker. For otherwise in-scope tickets, missing/multiple project tags and unresolved repos are blocking skipped tickets because they leave open work undrainable. Same-repo tickets go to that repo worker as one sequential queue with each item's exact project tag; different repos route in parallel. It uses only guarded `agent-contact` and `agent-tmux`: provider lanes are probed before choosing a route, exactly one safe contactable owner lane is reused, known no-pane refusals count as absence, and busy/attached/pending/ambiguous/unknown provider refusals block the group. If no safe lane and no unsafe provider state exists, one deterministic Codex session is launched/resumed from `--session-prefix` and the repo basename only while holding a local per-repo batch lock through polling/closeout, and only after `agent-tmux codex-existing` returns a recognized no-existing-session result at route selection and again immediately before launch. Unknown inspection failures block. Unsafe existing same-repo sessions block the group instead of spawning a duplicate. Start with `--dry-run` to see grouping, skipped tickets, refusals, repo paths, provider/session choices, and launch/contact plans without ticket comments, moves, sends, or launches. After routing, it polls and aggregates per-ticket closeout-check results; a ticket still open after the polling limit, moved to `Needs human`, or failed post-route `New -> Triaging` move makes the batch result fail.
- Add `--watch-origin` to `supervise` or `new --dispatch` only when the caller wants a close callback; `new --watch-origin` without `--dispatch` is rejected. Include `--origin-provider codex` or `--origin-provider claude` when known, and `--origin-session <tmux-session>` when the exact tmux-managed origin pane is known. If no provider is supplied, callback delivery probes both providers and sends only when exactly one guarded target accepts. The CLI writes origin watcher metadata locally, preflights watcher/outbox state before closing, reserves a durable `ticket.closed` outbox record before the Kanboard mutation, probes the origin with `agent-contact send --dry-run`, and sends only if the guarded probe accepts a safe target. If delivery is unsafe, the callback stays pending and the notify hook surfaces `agent-ticket callbacks --pending --repo <repo>` later; interrupted stale `delivering` records also become pending again instead of disappearing. Use `agent-ticket callbacks retry <event_key>` after making the target safe; when receiving such a callback, run `agent-ticket closeout-check <id> --strict`, inspect the ticket evidence, then acknowledge with `agent-ticket callbacks ack <event_key>` before resuming the original task. If the ticket is reopened before the callback is acknowledged, the prior close event is superseded and a later close emits a new revision.
- `agent-ticket closeout-check <id> --strict` is the supervisor proof gate before accepting owner closeout. It checks closed state, Done column, repo resolution, dirty git worktree when available, validation evidence in comments, commit/HEAD evidence, and optional install/sync evidence with `--require-install`.
- `agent-ticket source-info` is local-only and does not contact Kanboard. Use it when installed artifact source ownership is unclear; it reports the source manifest, installed/source CLI match, Codex/Claude skill match, notify-hook match, rollout command, and source git status or exact git-unavailable reason.
- `agent-ticket new` warns about a possible duplicate (same `project:` tag, strongly-matching title) and won't create it unless you pass `--force`; prefer commenting on the existing ticket.
- If the CLI can't reach Kanboard, the container may be stopped: `docker start kanboard`.
- The CLI retries Kanboard JSON-RPC responses that explicitly report transient SQLite database locks. Keep same-ticket tag edits serialized anyway, because retries do not make read-modify-write tag updates atomic.
- `agent-ticket tag` is read-modify-write (it fetches the current tags, applies your add/remove, writes the set back) and is **not atomic** — if two agents edit the same ticket's tags at the same moment, one set of changes can be lost. Serialize tag edits on the same ticket if that matters.
- Mutating commands (`comment`/`move`/`close`/`reopen`/`tag`/`show`) refuse a task id that isn't in the configured project, so a mistyped id can't act on someone else's board.
- Works for any agent with shell access (not Claude-specific). Or hit the JSON-RPC API directly at `http://localhost:8765/jsonrpc.php` (HTTP Basic: user `jsonrpc`, password = the application API token in the config file). A community MCP server (`bivex/kanboard-mcp`) also exists.
