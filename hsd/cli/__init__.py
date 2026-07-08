"""CLI for HSD — human and scripting interface to the task board.

The commands live in family modules under :mod:`hsd.cli`; this package is a
thin facade that registers them on the shared ``cli`` group and re-exports the
public surface (``main`` and the click group) so the ``hsd.cli:main`` entry
point and ``from hsd.cli import main`` continue to work.
"""

from hsd.cli._shared import PASS_DB, _parse_sections, _resolve_task, cli
from hsd.cli import draft, flow, ops, read  # noqa: F401  (registers commands)


def main() -> None:
    cli()


__all__ = ["main", "cli", "PASS_DB", "_resolve_task", "_parse_sections"]
