# HSD

HSD is a SQLite-backed session handoff board for AI coding harnesses. It keeps
session discovery, ownership, state transitions, review history, and recovery
in one place without trying to become a generic project manager.

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

The web dashboard runs at `http://127.0.0.1:8737` by default. Set
`HSD_DB_PATH`, `HSD_HOST`, or `HSD_PORT` when launching `hsd-web` to override
its database or listener.

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
