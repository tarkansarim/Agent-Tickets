# agent-tickets — design decisions & history

Condensed record of the session that produced this project (full chat: `SESSION-2026-05-10.jsonl`, git-ignored, API token scrubbed). Date: 2026-05-10.

## Goal

A ticketing system AI agents can use to report issues — like Jira, but for agents. Hard constraints that emerged:
- **Local / offline.** No cloud service the issues live in.
- **No git-push coupling.** The user works on sensitive commercial projects and will not push them to git, and wants no cloud account.
- **Web UI for the human to triage**, plus a programmatic interface so any agent (Claude Code, Codex, …) can file/query/update tickets.
- **Lives in `MyTools/` as the dev source**, but installs *out of* MyTools — installed artifacts must be self-contained for normal ticket operations (MyTools is the workspace where things are created; the source provenance path is diagnostic only). Live data and secrets stay outside MyTools (it's Dropbox-synced).

## Options evaluated

| Option | Verdict |
|---|---|
| **Beads** (steveyegge/beads) | Rejected. `bd init` git-init'd the whole MyTools tree, injected a CLAUDE.md mandating `git push`, and added Claude hooks — far too git-coupled for this use case. Removed. |
| **Vibe Kanban** (BloopAI) | Rejected. The kanban-board feature is gated behind a cloud login at `api.vibekanban.com`; "local-first" only covers the agent-execution side. Also the company is sunsetting. |
| **GitHub Issues** | Rejected. Online-only; won't put sensitive repos on GitHub. |
| **git-bug** | Rejected. Issues live in git refs, so they travel with `git push` — wrong for "keep tickets off the remote." |
| **taskmd / Tracer / TrackDown / Backlog.md** | Considered. Local & file-based, but no real web UI for human triage. |
| **Kanboard** (self-hosted) | **Chosen.** Mature, self-contained (single app + SQLite), real web UI, full JSON-RPC API, runs locally in Docker, zero cloud / account / git. A community MCP server (`bivex/kanboard-mcp`) and a Python client also exist. |

(For context: there's also an unrelated public "Sortie" at docs.sortie-ai.com — name collision with the user's own local Sortie project; not relevant here.)

## Architecture

- **Kanboard** in Docker (`docker-compose.yml`, image pinned `kanboard/kanboard:v1.2.52`), bound to `127.0.0.1:8765`. Web UI at http://localhost:8765. The CLI derives ticket/board URLs from the configured `endpoint` (so they always carry the right port); Kanboard's own "Application URL" setting is left at its default — it only affects links in the web UI / email notifications and is an optional manual tweak, not something `install.sh` configures.
- **Live data** in `~/kanboard-data/` (override `KANBOARD_DATA_DIR`) — *outside* MyTools, because a live SQLite file + Dropbox sync = corruption risk.
- **Board** "Agent Tickets — app/tool usage issues" (its `project_id` is resolved by `bootstrap-board.py` and stored in the config — typically `1` on a fresh single-project Kanboard, but not assumed): columns `New → Triaging → Agent working → Needs human → Done`; categories (= "kind") `bug/friction/request/blocker/question/idea/regression`; severity `p1–p3` and project/agent recorded as tags (`project:<name>`, `agent:<name>`, optionally `source:test-run`).
- **`bin/agent-ticket`** — small dependency-free Python CLI over the JSON-RPC API (`new`, `list`, `show`, `comment`, `move`, `tag`, `close`, `reopen`, `columns`; `--json` works before or after the subcommand for machine output; mutations refuse a task that isn't in the configured project; human output strips control chars). Reads `~/.config/agent-tickets/config.json` (endpoint + application API token + project_id), env-overridable.
- **`skill/SKILL.md`** — agent skill telling Claude Code, Codex, and other shell-capable agents when/how to file tickets.
- **`bootstrap-board.py`** — idempotently creates/reconciles the project, columns, and categories, and writes `project_id` into the config. Captures the board structure that was originally set up by hand, so a fresh install is reproducible.
- **`install.sh`** — idempotent roll-out: **copies** (never symlinks) `bin/agent-ticket` → `~/.local/bin/`, `skill/SKILL.md` → `~/.claude/skills/agent-tickets/` and `~/.codex/skills/agent-tickets/`, `docker-compose.yml` → `~/.config/agent-tickets/`, writes a non-secret `source.json` provenance manifest, seeds `config.json` from `config.example.json` if missing, brings up Kanboard, and runs `bootstrap-board.py` once a real token is present. Re-run it to push source edits.
- **`config.example.json`** — token-less template. The real token lives only in `~/.config/agent-tickets/config.json` (chmod 600), never in this Dropbox folder. `.gitignore` excludes `data/`, `backups/`, `config.json`, `*.sqlite`, `SESSION-*.jsonl`.

After install normal ticket operations do not reference this folder at runtime — the live system is entirely under `~/.local/bin`, `~/.claude/skills`, `~/.codex/skills`, `~/.config/agent-tickets`, `~/kanboard-data`. `source.json` is only provenance metadata for the local `source-info` diagnostic.

## Scope decision — usage issues, not a dev backlog

This board is for problems agents hit **while using/operating** the user's apps, tools, CLIs, and harnesses (crashes, wrong behavior, broken flags, bad docs, runtime misbehavior, friction). It is **not** a development backlog and **not** an agent's in-flight TODO list. Rule of thumb: *were you using something when it went wrong? → ticket. Were you building something and just have more to build? → not a ticket.* Reflected in the skill, the CLI's `--help`, and the Kanboard project name/description.

## Role decision — who files vs. who consumes

- **Owner / app-builder agent for a project:** fixes issues in place — does **not** file tickets (filing yourself a note you're about to action is noise). It *does* read, triage, and close tickets others filed for it (`agent-ticket list --project <app>` is its inbox).
- **Test / QA / review / exploration subagents** (run in "report mode"): **file** a ticket for each issue (tag `source:test-run`), don't touch code. The builder then drains the queue.
- So during app building the flow is: builder spawns test subagents → subagents file tickets → builder works the board. The skill carries a paste-ready "test/report mode" snippet for briefing those subagents.
- Deliberately **not** hard-blocked at the CLI level (no reliable agent-identity signal; would be brittle) — enforcement is doctrine in the skill + the dispatch prompt.

## Dogfooding result

A subagent was sent to use the CLI and report a bug; it correctly filed one, and surfaced three real bugs in the CLI — all fixed and verified:
1. `show` (and `list`, `tag`) never displayed tags — Kanboard's `getTask` doesn't return tags; now fetched via `getTaskTags`.
2. `--body` passed `\n` through literally — now expanded to real newlines/tabs.
3. `list --all` showed only *closed* tickets — now merges open + closed.

## Hardening pass — Codex adversarial-review loop (2026-05-10)

After the initial build, the repo went through repeated **Codex adversarial reviews**: each round, a fresh-context Codex reviewer tried to break everything; every critical/high/medium finding was fixed and verified; then another fresh review. The loop ran until a round came back clean at those tiers (round 8). ~49 issues fixed across the rounds. The substantive outcomes, by area:

- **`bootstrap-board.py` project identity** — resolves the board by *exact* name only; a configured `project_id` whose project name doesn't match aborts (no silent hijack of an unrelated project, no silent re-create that strands tickets); >1 match aborts; no prefix matching.
- **`bootstrap-board.py` columns** — reconciles by title; aborts on case-insensitive duplicate titles; fixes wrong-cased canonical columns; only ever renames Kanboard's stock default columns (`Backlog`/`Ready`/`Work in progress`) and only on a project with *zero* tasks (renaming once tickets exist would silently reclassify them); user-added columns are never touched; missing canonicals are added.
- **Config writes** — `bootstrap-board.py` writes config atomically (`tempfile.mkstemp(0600)` + `os.replace`) and keeps it `chmod 600`; `install.sh` always `chmod 600`s config.json and `chmod 700`s `~/.config/agent-tickets`; the example config carries no token and no `project_id`.
- **`agent-ticket` CLI** — `--json` works before *or* after the subcommand; URLs are derived from the configured endpoint (independent of Kanboard's "Application URL"); `list` has working `--project`/`--agent` (tag-membership) and `--kind` (no-create) filters; mutations refuse a task id that isn't in the configured project; every mutation result is checked (Kanboard returns `false`, not an HTTP error, on failure); human output strips C0/C1 control chars (so ticket text can't drive the terminal) while `--json` stays raw; malformed config gives a clean error; empty `AGENT_TICKETS_*` env vars are treated as unset.
- **`install.sh`** — `set -euo pipefail`; canonicalizes `KANBOARD_DATA_DIR` and refuses `$HOME` itself / paths under the repo or known cloud-sync roots; Docker preflight (binary + daemon + compose plugin, distinct messages); a python-urllib readiness loop that fails (with a 5xx-specific message) instead of hanging; writes a `.env` next to the installed compose so later `docker compose up` uses the same data dir; bootstrap failure is fatal and the "live" line only prints on success; a static (non-authenticating) reminder to change the default `admin/admin` password (an active probe would risk Kanboard's failed-login lockout on re-runs); honors `AGENT_TICKETS_TOKEN` from the env.
- **Misc** — Kanboard image pinned (`v1.2.52`) with `scripts/check-kanboard-version.sh` + a documented bump procedure; `.gitignore` covers data, secrets, the transcript, and python/log cruft; `.rewind/` has a reviewed exclude policy.

## Cross-agent + auto-notice (2026-05-11)

The CLI was always agent-agnostic; this pass made the *skill* and *proactive noticing* cross-agent too:
- `install.sh` now copies `skill/SKILL.md` into **both** `~/.claude/skills/agent-tickets/` and `~/.codex/skills/agent-tickets/` (same `SKILL.md` format works for both).
- `scripts/notify-hook.sh` (agent-neutral; installed to `~/.config/agent-tickets/`): derives the repo name from the git toplevel, lists open tickets tagged `project:<repo>`, diffs against a per-repo seen-cache (`~/.cache/agent-tickets/`), and prints `🎫 Open/New agent-ticket(s) for <repo>: …` — silent on no-change / Kanboard-down / no-CLI; never blocks.
- `scripts/register-hooks.py` (called by `install.sh`): idempotently wires the hook into **Claude Code** (`SessionStart` → baseline list, `UserPromptSubmit` → new-since-last) and **Codex** (`SessionStart` → baseline, `Stop` → new-since-last — Codex has no per-prompt hook event, so `Stop`, which fires after each turn, is the per-turn checkpoint). Existing hooks (Rewind's `Stop`, the C++/CUDA build-verify/safety hooks, etc.) are preserved. Codex validates hooks by hash, so the first `codex` run after install prompts the user to trust the new hook.

So in a long-running session on either agent: open tickets show up at session start, and a ticket a test/QA subagent files mid-session surfaces on the next prompt (Claude) or after the next turn (Codex) — once, not on a loop.

## Dispatch — handing tickets to repo agents (2026-05-11)

The queue + notify-hook handle "agent files a ticket → owner eventually sees it"; this adds an active handoff. Codex proposed an `agent-ticket dispatch <id>` dispatcher; the user accepted it with one key change — **dispatch is always explicit, never an automatic side-effect of `new`** (Codex wanted p1/p2 to auto-dispatch on filing; rejected as too noisy — a test-subagent filing N tickets would ping N workers).

- **`agent-ticket dispatch <id>`** + **`agent-ticket new … --dispatch`** (opt-in). Resolves `project:<name>` → a source repo path via `repo_roots` (config; default `["~/Dropbox/work/MyTools"]`, first hit wins). Then probes both providers with `agent-contact send --provider {codex,claude} --dry-run --json` (which finds the tmux-managed session for that repo+provider and applies all the safety: refuses attached / busy / pending-composer-text / dead / multiple-candidate sessions, rejects control-byte messages). Exactly-one contactable session → real send (a polite "triage this; move to Agent working if taking it; fix from SOURCE not the installed copy; validate; comment evidence; `agent-ticket close <id>`" message — and crucially **not** "run dispatch yourself", so no fan-out beyond depth 1), then record a `dispatched to <provider> session <name> at <utc>` comment (recorded first, so the double-dispatch guard survives even if the next step fails — a later `dispatch` on the same ticket sees that comment and no-ops), then move `New → Triaging`.
- **Guarded everywhere, never guesses:** hard-error on a `Needs human` ticket; skip p3 by default so low-priority tickets do not interrupt owner agents; no-op on a ticket already in `Agent working`/`Done`/already-dispatched. Zero contactable / 2+ providers / unresolvable repo / `AGENT_CONTACT_TRUSTED_PROVIDER_ROOTS`+`AGENT_CONTACT_TRUSTED_LAUNCHER_ROOTS` not set in the env → record a comment explaining why and **exit 0** (the ticket just stays in the queue for the notify-hook to surface). `agent-contact`'s `--json` schema: `{"status":"refused","stage":"discovery|pre_send_state","reason":...,["session":...,"pane_state":...]}` on refusal (rc≠0), success is rc==0 — so `dispatch` trusts rc.
- **Dry-run is explicit:** `agent-ticket dispatch <id> --dry-run` resolves/probes the same route and reports whether it would dispatch, but does not write comments, move tickets, or contact an agent. This gives workers a safe smoke-test lane instead of relying on refusal side effects or touching a real owner chat.
- **`agent-ticket new` duplicate warning:** before creating, scan open tickets with the same `project:` tag; if a title is a "strong" match (case-insensitive: identical, or one is a ≥12-char substring of the other, or they share a ≥4-token contiguous run), print `⚠ possible duplicate of #N …` and don't create unless `--force`. Conservative so a short generic title never trips it. (A warn, not a hard block, to avoid false-positive lockouts.)
- All of this lands in `bin/agent-ticket` (now imports `subprocess`, `time`; shells out to `agent-contact` only) and the SKILL.md/config/README. No changes to `bootstrap-board.py`; `install.sh` gains a one-time tip about the `agent-contact trust-roots` setup needed for live dispatch and supervised contact/probe routes.

## Source ownership diagnostic (2026-05-11)

Ticket #23 exposed an ownership ambiguity: the installed `~/.local/bin/agent-ticket` can be byte-identical to this source tree while the source folder itself may not have usable git metadata. To make maintenance unambiguous without making normal runtime depend on MyTools:

- `install.sh` writes `~/.config/agent-tickets/source.json`, a non-secret manifest with the source directory, copy-vs-symlink mode (`copy`), installed/source CLI hashes, rollout command, and the source git status visible at install time.
- `agent-ticket source-info` reads that manifest without loading Kanboard credentials or contacting Kanboard, recomputes the current installed/source CLI hash comparison, and reports source git status directly. If `.git` is absent or invalid, the command says so instead of implying a clean repo.
- The installed CLI remains a real copy. Editing happens in this source tree, then `./install.sh` rolls the source-owned files out again.

## Close lifecycle and SQLite lock retry (2026-05-11)

Tickets #25 and #26 exposed two multi-agent workflow frictions:

- `agent-ticket close <id>` previously only called Kanboard `closeTask`, leaving the task's stored column unchanged. A closed ticket could therefore read back as `status: closed / column: Agent working`, which made resolved work look in-progress. `close` now moves the task to `Done` first and then closes it; if the task is already inactive, it still moves to `Done` and skips the redundant close call.
- Concurrent workers can make Kanboard's SQLite backend return transient `database is locked` JSON-RPC errors. The CLI now retries only explicitly recognized SQLite lock errors with short backoff; non-lock API failures still fail immediately. Same-ticket read-modify-write operations such as tag edits remain non-atomic and should still be serialized by agents.

## Supervised owner-agent orchestration and closeout gate (2026-05-12)

Tickets #32 and #33 turned the manual supervision loop into source-owned CLI behavior:

- **`agent-ticket supervise <id>`** resolves the ticket's `project:<name>` repo, selects an owner lane from guarded `agent-contact` probes or a fresh deterministic Codex fallback, sends to an existing contactable tmux session or launches a Codex owner through `agent-tmux`, supports visible Codex full-permission flags with `--full-permission`, polls ticket state, and runs closeout checks after the owner closes the ticket. `--dry-run` reports the planned route without writes/contact/launch.
- Ticket #41 fixed a session-name collision in that fallback path. Existing tmux panes are reusable only through guarded `agent-contact`; when supervision falls back to Codex launch, the requested tmux session is a new deterministic ticket-scoped name derived from `--session-prefix`, the project tag, and the ticket id. That keeps stale or existing `owner-<project>` sessions from causing the AgentTerminalContact wrapper to refuse with "requested session already exists."
- Ticket #60 fixed the next collision class for that same fallback path. Before `supervise` asks `agent-tmux` to launch the deterministic ticket-scoped session, it checks `agent-tmux codex-existing <repo> <session>` for that exact requested name. If that session is already present, the session may only be reused through guarded exact-session `agent-contact`; when guarded contact refuses, dry-run and live supervision block with `existing session not contactable` instead of retrying a launch that the wrapper must refuse or creating an unsafe duplicate lane.
- Ticket #62 removed implicit `codex-latest`/`codex-resume-latest` from supervised routing. Latest-chat metadata is not owner-lane identity and can point at stale review/subagent context, so fallback now launches a fresh deterministic Codex lane after the exact-session guard reports absence. Existing contactable panes are reused only when their provider/session identity is tied to explicit `--session` or an active same-owner supervision claim; unbound contactable panes block as stale/unsafe context. Dry-run/JSON/audit route evidence records the fresh launch and `resume_latest: skipped`; unsafe provider refusals block before a fresh launch.
- Ticket #99 narrowed single-ticket supervision to the deterministic ticket-scoped session before contact or launch. Unrelated same-repo panes can still be unsafe to contact, but they are not route identity for a specific ticket and should not block a fresh ticket-scoped lane when the exact requested session is absent. Batch supervision keeps the stricter repo-wide same-repo guard because it owns a queue for the repo, not one ticket.
- Supervision refusal states are explicit. If routing/contact/launch cannot proceed, the command reports the blocker, comments on the original ticket, and by default files/reuses a local `agent-tickets` blocker in `Needs human` so tool friction is not silently worked around. `--no-tool-ticket` disables that filing when a caller wants report-only behavior.
- **`agent-ticket closeout-check <id>`** is the independent supervisor gate: closed state, `Done` column, repo resolution, git worktree cleanliness when git is available, validation evidence in comments, commit/HEAD evidence, and optional install/sync evidence. `--strict` makes closed state plus validation evidence blocking; `--require-clean`, `--require-commit`, and `--require-install` make those checks blocking when the ticket's risk warrants it.
- **`agent-ticket reopen <id>`** now opens into `Triaging` by default, accepts `--column`, and records an audit comment. Reopened tickets no longer sit closed/open in `Done` until a human remembers a second move.
- Dispatch and supervision prompts now include the closeout gate: validation evidence, clean worktree/commit evidence when source changed, and install/sync evidence when installed artifacts changed.

## Watched-ticket close callbacks (2026-05-12)

Ticket #36 added an opt-in async notification path for supervised tickets without turning closeout into unconditional agent resume:

- `agent-ticket supervise <id> --watch-origin` and `agent-ticket new --dispatch --watch-origin` register one origin watcher for the ticket. The watcher metadata is local and durable under `~/.config/agent-tickets/callbacks/`: origin repo, provider when supplied, optional tmux session, created/expires timestamps, callback reason, correlation id, and close revision state. A new watcher replaces the previous watcher for that ticket to avoid fan-out.
- `agent-ticket close <id>` still moves to `Done` before closing. If a watcher exists, close preflights the watcher/outbox state and reserves a local `ticket.closed` outbox JSON record before the Kanboard mutation. Delivery runs `agent-contact send --dry-run` to the origin and sends only if the guarded probe accepts one safe idle tmux-managed target. If no provider was supplied, delivery probes both Codex and Claude and sends only when exactly one provider accepts.
- Unsafe delivery is a first-class state. Expiration, missing `agent-contact`, busy/attached/ambiguous panes, ambiguous provider probes, and refused sends leave the outbox record pending and add an audit comment to the ticket. Interrupted `delivering` records age back to pending instead of disappearing, while missing/corrupt watcher-referenced outbox records fail loudly. The notify hook calls `agent-ticket callbacks --pending --repo <repo>` so pending callback records surface in later owner/supervisor sessions even when Kanboard is unreachable. `agent-ticket callbacks retry <event_key>` reruns guarded delivery, and `agent-ticket callbacks ack <event_key>` removes a handled pending callback from hook surfacing after the receiver runs closeout-check.
- Callback idempotency is close-state based: repeated `close` calls while a ticket remains closed reuse the same `ticket:<id>:closed:<revision>` record; `reopen` supersedes any unacknowledged prior close event and clears the closed-state bit so a later close emits the next revision. Watcher and event files use local locks so concurrent close/retry/ack paths cannot duplicate sends or overwrite acknowledgement state. `source-info` now reports installed parity for the CLI, Codex skill, Claude skill, and notify hook because callback behavior depends on all four artifacts.

## User-level batch supervision (2026-05-12)

Ticket #39 added a first-class board-drain workflow for supervising multiple open tickets without making it specific to any one project family:

- **`agent-ticket supervise-batch`** scans open tickets across the local Agent Tickets board, defaulting to open non-`Done` work and skipping `p3` plus `Needs human`. It can filter by exact `project:<repo>`, severity, kind, tag, and column, and it can explicitly include `p3` or `Needs human` when the caller wants those in scope.
- Tickets must carry exactly one `project:<repo>` tag, but grouping is keyed by canonical resolved source repo path, using the same `repo_roots` resolution rules as dispatch/supervise. The exact project tag is preserved per ticket in the queue, and aliases resolving to one source tree merge into one repo group so the system still starts at most one owner worker per repo. For otherwise in-scope tickets, missing, multiple, or unresolvable project tags are reported as blocking skipped items; they are never guessed and they make the batch result fail because the board still has undrainable open work.
- Routing is one owner lane per repo group. Same-repo tickets are placed into a single ordered prompt queue, including each ticket's exact `project:<repo>` tag, that tells the owner worker to process them sequentially in one worktree: move each ticket to `Agent working` when starting, fix from source, validate, install/roll out source-owned installed artifacts if changed, comment evidence, close the ticket, then continue to the next. Different repo groups route in parallel.
- Safety stays on the existing guarded surfaces only. Batch supervision first probes `agent-contact send --dry-run` across provider lanes; exactly one safe contactable lane is reused only when it is tied to an active same-owner provider/session claim identity. Known `no tmux-managed ... pane found` refusals for the same canonical repo are treated as absence, while busy, attached, pending-input, ambiguous, wrong-repo, unknown provider refusals, or contactable panes without matching provider/session claim identity block the repo group before any contact or launch so a same-repo worker is not duplicated across providers. If no safe bound lane exists and no unsafe provider state exists, Codex is the only launchable provider and the command uses `agent-tmux` with fresh deterministic sessions named from `--session-prefix` plus the repo basename only while holding a local per-repo batch lock through polling/closeout. It requires `codex-existing` to return a recognized no-existing-session result for the same canonical repo at route selection and again immediately before launch; missing tools, timeouts, permission errors, unrecognized inspection output, or a same-repo session appearing between those checks block the group. Latest-chat metadata is not queried for routing; launch route evidence records `resume_latest: skipped`, never an implicit resume target. If `agent-tmux codex-existing` reports an existing same-repo session that `agent-contact` would not safely accept, the group blocks instead of launching a duplicate lane. `--full-permission` uses the visible `agent-tmux` full-permission aliases.
- `--dry-run` shows grouping, skipped tickets, refusals, repo paths, provider/session decisions, and launch/contact plans without comments, moves, sends, or launches. Real routing comments each queued ticket, moves `New` tickets to `Triaging`, polls all routed tickets without serializing repo groups, and aggregates per-ticket `closeout-check` reports. A routed ticket that remains open after the polling limit, moves to `Needs human`, or cannot be moved `New -> Triaging` after route blocks the aggregate batch result.
- Batch supervision does not register watched-ticket callbacks by default. It is already the supervising process polling all tickets, so per-ticket callbacks would create notification spam rather than improve closeout.

## Durable supervision claims (2026-05-12)

Ticket #51 added a durable local claim layer so a second supervisor can see that a repo/ticket set is already being coordinated before it starts another owner worker:

- Claims are JSON records under `~/.config/agent-tickets/supervision/`, guarded by a local `flock`, keyed by canonical repo path plus ticket ids. They store owner id, origin provider/repo/session when known, worker provider/session/mode, ticket ids, created/updated/last-heartbeat timestamps, expiry, and whether the current caller owns the claim in public output.
- `agent-ticket supervise` and `agent-ticket supervise-batch` check active claims before normal route planning and acquire/update a claim after route selection but before any real `agent-contact` send, `agent-tmux` launch, ticket comment, or ticket move. Same-owner reentry is allowed via `--supervisor-id` or `AGENT_TICKETS_SUPERVISOR_ID`; active other-owner claims for the same repo or overlapping tickets fail closed with `already supervised` unless `--force-supervision` is explicit.
- Routed supervisors heartbeat the claim while polling. When all routed tickets close or move to `Needs human`, the claim is released. If the supervisor exits while tickets remain open or is interrupted, the claim remains visible until expiry and can be recovered explicitly.
- `agent-ticket supervision status|release|adopt|steal` is the recovery surface. Status lists active and stale claims; release/adopt/steal require a claim/repo/ticket/all filter and only act on claims owned by the current supervisor or stale claims unless `--force` is supplied.
- Default process-bound owners use `owner_id=pid:<host>:<pid>`. For claims from this host, a dead owner PID makes the claim stale immediately, so later `supervise`/`supervise-batch` calls do not wait for TTL or require `--force-supervision`; explicit stable `--supervisor-id` claims remain TTL/recovery based.
- Ticket #149 removed the stale `idle_empty_prompt` override after `agent-contact` made tmux-managed worker composers a disposable control surface. Supervision now treats any successful guarded dry-run (`rc=0`, including `status=would_send` with an informational `pane_reason`) as contactable and leaves pane-state safety and cleanup policy to `agent-contact`; nonzero guarded refusals still block through the existing refusal paths.

## Open / possible follow-ups (not done)

- Set up the `bivex/kanboard-mcp` MCP server so MCP-capable agents get native tools (CLI works fine without it).
- A periodic DB backup (copy `~/kanboard-data/data/db.sqlite` into `agent-tickets/backups/` — git-ignored — for portability; never the live file).
