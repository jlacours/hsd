"""Terminal UI command; Textual is imported only when the UI is launched."""

import click

from hsd.cli._shared import cli


@cli.command()
@click.pass_context
def tui(ctx: click.Context) -> None:
    """Open the interactive task board."""
    # Keep Textual and its startup cost out of every non-TUI command.
    from hsd.tui.app import BoardApp

    BoardApp(ctx.obj).run()
