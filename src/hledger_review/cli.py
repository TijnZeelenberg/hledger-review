"""Command line: `hledger-review [review|import|rules] ...`.

Installed on PATH, it also works as an hledger add-on: `hledger review`.
"""

import argparse
import sys
from pathlib import Path

from rich.console import Console

from hledger_review import __version__, hledger, rules
from hledger_review.config import (
    Config,
    ConfigError,
    Source,
    load,
    load_sources,
    pick_source,
)
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


def rule_stats(
    config_arg: str | None,
    source_name: str | None,
    rules_arg: Path | None,
    csvs: list[Path],
    stdin: bool,
) -> int:
    """Print how often each rule matches, one `FILE:LINE: level: text` per rule."""
    sources = load_sources(config_arg)
    source: Source | None
    if rules_arg is None:
        source = pick_source(sources, source_name)
        if source.rules is None:
            raise ConfigError(f"source {source.name!r} has no rules file")
        path = source.rules
    else:
        path = rules_arg
        wanted = rules_arg.resolve()
        source = next(
            (s for s in sources.values() if s.rules and s.rules.resolve() == wanted),
            None,
        )
    if not csvs:
        default = source.csv if source and source.csv else path.with_suffix("")
        csvs = [default]
    missing = [str(c) for c in csvs if not c.is_file()]
    if missing:
        raise ConfigError(f"no such CSV file: {', '.join(missing)}")

    text = sys.stdin.read() if stdin else path.read_text()
    parsed = rules.parse(text)
    category = source.rule_account if source else None
    report = rules.stats(parsed, rules.read_rows(csvs, parsed.directives), category)

    for s in report.rules:
        print(f"{path}:{s.rule.line}: {describe(s)}")
    summary = f"{plural(report.rows, 'row')} from {plural(len(csvs), 'CSV file')}"
    if category:
        summary += f", {report.fallthrough} set no {category}"
    print(summary, file=sys.stderr)
    return 0


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def describe(s: rules.RuleStats) -> str:
    """One rule's stats as `level: text`, the way compilers report."""
    if s.error:
        return f"error: {s.error}"
    if not s.rows:
        return "warning: no rows"
    parts = [plural(s.rows, "row")]
    level = "note"
    if s.overridden:
        n = len(s.overridden_by)
        if n > 3:
            by = f"overridden by {n} later rules"
        else:
            lines = ", ".join(map(str, sorted(s.overridden_by)))
            by = f"overridden by line{'s' if n > 1 else ''} {lines}"
        if s.overridden == s.rows:
            level = "warning"
            parts.append(f"all {by}")
        else:
            parts.append(f"{s.overridden} {by}")
    if s.last:
        parts.append(f"last {s.last}")
    return f"{level}: {', '.join(parts)}"


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
    rls = sub.add_parser(
        "rules",
        help="show how often each rule matches",
        description="Match every rule against CSV data and print one line per "
        "rule: FILE:LINE: note|warning|error: text.",
    )
    rls.add_argument(
        "csv", nargs="*", type=Path, help="CSV files (default: the source's CSV)"
    )
    rls.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="config file (default: nearest hledger-review.toml)",
    )
    rls.add_argument("-s", "--source", help="which [sources.NAME] to use")
    rls.add_argument(
        "-r",
        "--rules",
        type=Path,
        help="rules file (default: the source's); picks the source that uses it",
    )
    rls.add_argument(
        "--stdin",
        action="store_true",
        help="read the rules text from stdin, e.g. an unsaved editor buffer",
    )
    return ap


def main(argv: list[str] | None = None) -> None:
    """Entry point."""
    args = parser().parse_args(argv)
    try:
        config_arg = getattr(args, "config", None)
        if args.command == "rules":
            sys.exit(
                rule_stats(config_arg, args.source, args.rules, args.csv, args.stdin)
            )
        config = load(config_arg, getattr(args, "file", None))
        if args.command == "import":
            code = run_import(config, args.export, config.source(args.source), args.yes)
        else:
            code = review(
                config, getattr(args, "since", None), getattr(args, "all", False)
            )
    except (ConfigError, hledger.HledgerError) as e:
        sys.exit(f"hledger-review: {e}")
    sys.exit(code)
