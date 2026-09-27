# HSD

HSD is an integrated workspace for planning, running, and reviewing AI coding
work. Its SQLite-backed board keeps task plans, live harness sessions,
ownership, state transitions, review history, and recovery in one place.

## Install

HSD requires Python 3.12 or newer and uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

The default database is `$XDG_DATA_HOME/hsd/hsd.db`, falling back to
`~/.local/share/hsd/hsd.db`.

## Interfaces

The CLI manages the complete task lifecycle:

```bash
uv run hsd --help
uv run hsd ls
uv run hsd --db /path/to/board.db stats
uv run hsd serve
```

All creation interfaces use one canonical task contract: a kebab-case slug,
non-empty title/destination/source identity, and non-empty `objective` and
`current_state` sections. `plan` is optional at creation so a planning session
can write it after the task exists. For example:

```bash
uv run hsd create add-search "Add search" \
  --source-harness codex --source-model gpt-5.6 \
  --objective "Add task search to the board." \
  --current-state "The board can only be filtered by fixed fields."
```

The CLI, web API, MCP server, and database API all reject tasks that do not
follow that format. Review-only sections remain enforced when work is submitted
for review.

The web dashboard runs at `http://127.0.0.1:8737` by default. Use **+ Task** to
open the authoring workspace: the board task list stays on the left, the
Markdown plan is edited in the center, and a local PTY terminal runs on the
right. The terminal starts in the task's working directory when one is set.

The Settings page stores separate provider/model profiles for planning,
coding, reviewing, bug work, and maintenance. Selecting an activity in the
authoring terminal exports its profile as `HSD_ACTIVITY`, `HSD_PROVIDER`, and
`HSD_MODEL` in the shell, giving harness launchers one consistent place to read
workflow intent.

Every task also receives a stable, persisted named Herdr session
(`hsd-<random-id>`).
**Open / Resume Herdr** starts or reattaches that session inside the web
terminal, so its workspaces, panes, and agent conversations survive browser
disconnects. Herdr supplies the live multi-agent transport (`herdr agent
prompt`, agent status, and terminal attachment); HSD remains the durable task,
plan, profile, and review control plane.

Set `HSD_DB_PATH`, `HSD_HOST`, or `HSD_PORT` when launching `hsd-web` to override
its database or listener. Set `HSD_TERMINAL_ENABLED=0` to disable the PTY
endpoint, or `HSD_TERMINAL_SHELL` to choose a shell other than `$SHELL`.

`hsd-mcp` exposes the same board over stdio for MCP-aware harnesses. Its tools
cover board discovery, task creation and claiming, updates, submission, review,
human escalation and resolution, handoff export, and board statistics.

## Session lifecycle

```text
todo -> in-progress -> done -> reviewed -> closed
                       |         |
                       +-> todo  +-> to-be-revised-by-human -> todo
                       |
                       +-> to-be-revised-by-human -> todo
```

Submissions can include a diff and verification command. Harness review moves a
completed session toward acceptance, revision, or explicit human intervention;
the final human review either closes it or returns it for revision. Every
transition and review is retained in SQLite.

## Verify

```bash
uv run pytest -q
node --check hsd/web/static/app.js
```

The terminal loads pinned xterm.js assets with Subresource Integrity checks and
uses a basic command-console fallback when those assets are unavailable. HSD
binds to `127.0.0.1` by default and refuses terminal clients that are not
loopback. `HSD_TERMINAL_ALLOW_REMOTE=1` removes that terminal guard; do not set
it without putting authenticated TLS in front of HSD.
