"""Command line: `hledger-review [review|import|generate|rules] ...`.

Installed on PATH, it also works as an hledger add-on: `hledger review`.
"""

import argparse
import datetime as dt
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
from hledger_review.importer import load_items, run_generate, run_import

STALE_DAYS = 365  # no match for longer is flagged; yearly bills still pass


def review(config: Config, since: str | None, visit_all: bool) -> int:
    """Open the TUI over unmarked transactions, then run `hledger check`."""
    from hledger_review.app import ReviewApp  # textual import is slow; import only here

    if not config.journal.is_file():
        raise ConfigError(f"journal {config.journal} does not exist")
    if not config.sources:
        raise ConfigError("no [sources.*] configured: nothing to review")
    console = Console()
    changed = ReviewApp(config, load_items(config, since, visit_all)).run() or 0

    remaining = len(load_items(config, None, False))
    console.print(
        f"[bold]{changed}[/] transaction(s) updated, "
        f"[yellow]{remaining}[/] still on {config.unmarked}."
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
        path = source.rules
    else:
        path = rules_arg
        wanted = rules_arg.resolve()
        source = next(
            (s for s in sources.values() if s.rules.resolve() == wanted),
            None,
        )
    if not csvs and source is None:
        csvs = [path.with_suffix("")]
    elif not csvs and source is not None:
        csvs = [source.data(y) for y in source.years()]
        if not csvs:
            raise ConfigError(f"source {source.name!r} has no data files yet")
    missing = [str(c) for c in csvs if not c.is_file()]
    if missing:
        raise ConfigError(f"no such CSV file: {', '.join(missing)}")

    text = sys.stdin.read() if stdin else path.read_text()
    parsed = rules.parse(text)
    category = source.rule_account if source else None
    report = rules.stats(parsed, rules.read_rows(csvs, parsed.directives), category)

    today = dt.date.today()
    for s in report.rules:
        print(f"{path}:{s.rule.line}: {describe(s, today)}")
    summary = f"{plural(report.rows, 'row')} from {plural(len(csvs), 'CSV file')}"
    if category:
        summary += f", {report.fallthrough} set no {category}"
    print(summary, file=sys.stderr)
    return 0


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def age(date: dt.date, today: dt.date) -> str:
    """How long ago DATE was, compactly: `today`, `5d`, `3w`, `4m`, `2y`."""
    days = (today - date).days
    if days <= 0:
        return "today"
    if days < 14:
        return f"{days}d"
    if days < 60:
        return f"{days // 7}w"
    if days < 730:
        return f"{days // 30}m"
    return f"{days // 365}y"


def describe(s: rules.RuleStats, today: dt.date) -> str:
    """One rule's stats as `level: text`, the way compilers report."""
    if s.error:
        return f"error: {s.error}"
    if not s.rows:
        return "warning: unused"
    parts = [f"{s.rows} {'match' if s.rows == 1 else 'matches'}"]
    level = "note"
    if s.overridden:
        n = len(s.overridden_by)
        if n > 3:
            by = f"overridden ({n} rules)"
        else:
            by = f"overridden by {', '.join(map(str, sorted(s.overridden_by)))}"
        if s.overridden == s.rows:
            level = "warning"
            parts.append(f"all {by}")
        else:
            parts.append(f"{s.overridden} {by}")
    if s.last:
        parts.append(age(s.last, today))
        if (today - s.last).days > STALE_DAYS:
            level = "warning"
            parts.append("stale")
    if s.rows == 1:
        level = "warning"
    return f"{level}: {' · '.join(parts)}"


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
        "import",
        parents=[common],
        help="merge a bank CSV export into the year files and regenerate",
    )
    imp.add_argument("export", type=Path, help="the CSV file downloaded from the bank")
    imp.add_argument(
        "-s", "--source", help="which [sources.NAME] to use (default: the only one)"
    )
    imp.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    gen = sub.add_parser(
        "generate",
        parents=[common],
        help="regenerate the journals from the year files, then check",
    )
    gen.add_argument(
        "year", nargs="*", type=int, help="years to generate (default: all)"
    )
    gen.add_argument("-s", "--source", help="only this [sources.NAME]")
    rls = sub.add_parser(
        "rules",
        help="show how often each rule matches",
        description="Match every rule against CSV data and print one line per "
        "rule: FILE:LINE: note|warning|error: text. `include` is not followed, "
        "so run it on the shared rules; one-off tables are not counted.",
    )
    rls.add_argument(
        "csv",
        nargs="*",
        type=Path,
        help="CSV files (default: all year files of the source)",
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
        elif args.command == "generate":
            sources = (
                [config.source(args.source)]
                if args.source
                else list(config.sources.values())
            )
            if not sources:
                raise ConfigError("no [sources.*] configured")
            code = run_generate(config, sources, args.year)
        else:
            code = review(
                config, getattr(args, "since", None), getattr(args, "all", False)
            )
    except (ConfigError, hledger.HledgerError) as e:
        sys.exit(f"hledger-review: {e}")
    sys.exit(code)
