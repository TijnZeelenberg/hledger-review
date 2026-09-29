"""Command line: `hledger-review [review] ...` and `hledger-review import ...`.

Installed on PATH, it also works as an hledger add-on: `hledger review`.
"""

import argparse
import sys
from pathlib import Path

from rich.console import Console

from hledger_review import __version__, hledger
from hledger_review.config import Config, ConfigError, load
from hledger_review.importer import run_import
from hledger_review.journal import Txn, parse_journal


def review(config: Config, since: str | None, visit_all: bool) -> int:
    """Open the TUI over unmarked transactions, then run `hledger check`."""
    from hledger_review.app import ReviewApp  # textual import is slow; import only here

    if not config.journal.is_file():
        raise ConfigError(f"journal {config.journal} does not exist")
    roles = config.roles
    segments = parse_journal(config.journal.read_text())
    txns = [s for s in segments if isinstance(s, Txn)]

    def selected(t: Txn) -> bool:
        if since and t.date < since:
            return False
        if roles.unmarked in t.accounts():
            return True
        return (
            visit_all
            and t.asset_posting(roles) is not None
            and t.target_posting(roles) is not None
        )

    todo = [t for t in txns if selected(t)]
    console = Console()
    changed = ReviewApp(config, segments, todo).run() or 0

    remaining = sum(1 for t in txns if roles.unmarked in t.accounts())
    console.print(
        f"[bold]{changed}[/] transaction(s) updated, "
        f"[yellow]{remaining}[/] still on {roles.unmarked}."
    )
    if changed and hledger.executable():
        ok, msg = hledger.check(config.journal)
        if msg:
            console.print(msg, markup=False)
        console.print("hledger check: " + ("[green]OK[/]" if ok else "[red]FAILED[/]"))
        return 0 if ok else 1
    return 0


def parser() -> argparse.ArgumentParser:
    # shared options; SUPPRESS so a subcommand does not reset what came before it
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-f",
        "--file",
        default=argparse.SUPPRESS,
        help="journal (default: $LEDGER_FILE)",
    )
    common.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="config file (default: nearest hledger-review.toml)",
    )
    review_opts = argparse.ArgumentParser(add_help=False)
    review_opts.add_argument(
        "--since",
        default=argparse.SUPPRESS,
        help="only transactions on or after this date (YYYY-MM-DD)",
    )
    review_opts.add_argument(
        "--all",
        action="store_true",
        default=argparse.SUPPRESS,
        help="also visit already categorised transactions of a source account",
    )

    ap = argparse.ArgumentParser(
        prog="hledger-review",
        description="Import bank CSVs into an hledger journal and categorise what "
        "the rules missed. Without a command, runs `review`.",
        parents=[common, review_opts],
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser(
        "review",
        parents=[common, review_opts],
        help="categorise unmarked transactions in a TUI (default)",
    )
    imp = sub.add_parser(
        "import", parents=[common], help="import a bank CSV export into the journal"
    )
    imp.add_argument("export", type=Path, help="the CSV file downloaded from the bank")
    imp.add_argument(
        "-s", "--source", help="which [sources.NAME] to use (default: the only one)"
    )
    imp.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    return ap


def main(argv: list[str] | None = None) -> None:
    """Entry point."""
    args = parser().parse_args(argv)
    try:
        config = load(getattr(args, "config", None), getattr(args, "file", None))
        if args.command == "import":
            code = run_import(config, args.export, config.source(args.source), args.yes)
        else:
            code = review(
                config, getattr(args, "since", None), getattr(args, "all", False)
            )
    except (ConfigError, hledger.HledgerError) as e:
        sys.exit(f"hledger-review: {e}")
    sys.exit(code)
