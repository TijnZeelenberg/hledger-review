"""Year data files: `import` merges exports into them, `generate` prints them.

Each source keeps one CSV per year with every row the bank ever exported for
it, and generates that year's journal from it with hledger:

    hledger -f YEAR.csv --rules one-offs.rules print

An import adds only rows whose `row_key` is not in the year file yet, in the
place the export has them relative to rows the file already has, so the file
stays in the bank's order (newest first; oldest-first exports are reversed).
Nothing changes when an export adds nothing. Every write is followed by
`hledger check` and rolled back if it fails.
"""

import codecs
import csv
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from hledger_review import hledger, rules
from hledger_review.config import Config, ConfigError, Source
from hledger_review.journal import Txn, transactions, write_atomic

HEADER = """\
; GENERATED, do not edit: {label} {year} from {data} and {one_offs}
;   hledger -f {data_path} --rules {one_offs_path} print{style}

decimal-mark {mark}

"""


class Failed(Exception):
    """A step failed; what was written is rolled back."""


# csv files
@dataclass
class Table:
    """A CSV file as raw records, so rewriting it keeps every byte of every row."""

    header: list[str]
    rows: list[str]
    newline: str = "\n"
    bom: bool = False

    def text(self) -> str:
        return "".join(r + self.newline for r in self.header + self.rows)


def split_records(text: str) -> list[str]:
    """CSV records without their line ends; quoted fields may span lines."""
    records: list[str] = []
    pending: str | None = None
    for line in text.split("\n"):
        record = line if pending is None else pending + "\n" + line
        if record.count('"') % 2:
            pending = record
            continue
        pending = None
        if record.rstrip("\r"):
            records.append(record.removesuffix("\r"))
    if pending:
        records.append(pending)
    return records


def _utf8(encoding: str) -> bool:
    return codecs.lookup(encoding).name in ("utf-8", "utf-8-sig")


def read_table(path: Path, directives: rules.Directives) -> Table:
    """A CSV file, split into `skip` header records and data records."""
    data = path.read_bytes()
    utf8 = _utf8(directives.encoding)
    bom = utf8 and data.startswith(codecs.BOM_UTF8)
    text = data.decode("utf-8-sig" if utf8 else directives.encoding, errors="replace")
    records = split_records(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    return Table(records[: directives.skip], records[directives.skip :], newline, bom)


def write_table(path: Path, table: Table, directives: rules.Directives) -> None:
    """Write via a temp file and rename, keeping the byte order mark if any."""
    utf8 = _utf8(directives.encoding)
    data = table.text().encode("utf-8" if utf8 else directives.encoding)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes((codecs.BOM_UTF8 if table.bom else b"") + data)
    os.replace(tmp, path)


@dataclass
class Shared:
    """A source's parsed shared rules, to read its rows the way hledger does."""

    source: Source
    parsed: rules.RulesFile = field(init=False)
    evaluator: rules.Evaluator = field(init=False)

    def __post_init__(self) -> None:
        if not self.source.rules.is_file():
            raise ConfigError(f"rules file {self.source.rules} does not exist")
        self.parsed = rules.parse(self.source.rules.read_text())
        self.evaluator = rules.Evaluator(self.parsed.directives)

    @property
    def directives(self) -> rules.Directives:
        return self.parsed.directives

    def row(self, record: str) -> rules.Row:
        values = next(csv.reader([record], delimiter=self.directives.separator), [])
        return rules.Row(tuple(v.strip() for v in values))

    def key(self, row: rules.Row) -> tuple[str, ...]:
        return tuple(self.evaluator.value(row, f) for f in self.source.row_key)

    def date(self, record: str) -> str:
        """The record's date as YYYY-MM-DD, or an empty string."""
        d = self.evaluator.date(self.row(record))
        return d.isoformat() if d else ""

    def output_mark(self) -> str:
        """The decimal mark of the generated journal's amounts."""
        style = self.source.commodity_style
        if style is None:
            return self.directives.decimal_mark or "."
        marks = [c for c in style if c in ".,"]
        if not marks:
            return "."
        if style.count(marks[-1]) > 1:
            return "," if marks[-1] == "." else "."
        return marks[-1]


# merging
@dataclass
class YearPlan:
    year: int
    table: Table  # merged: the year file with the new rows in place
    new: list[str]
    exists: bool


def merge(shared: Shared, table: Table, export: list[str]) -> tuple[Table, list[str]]:
    """Add the EXPORT rows whose key TABLE lacks, where the export has them.

    A new row goes above the next export row the file already has; new rows
    at the end of the export go below the last one it has. Without any shared
    row, rows are merged by date, newest first.
    """
    positions: dict[tuple[str, ...], list[int]] = {}
    for i, record in enumerate(table.rows):
        positions.setdefault(shared.key(shared.row(record)), []).append(i)
    matched: list[int | None] = []
    for record in export:
        found = positions.get(shared.key(shared.row(record)))
        matched.append(found.pop(0) if found else None)
    new = [r for r, m in zip(export, matched, strict=True) if m is None]
    if not new:
        return table, []
    if all(m is None for m in matched):
        rows = sorted(new + table.rows, key=shared.date, reverse=True)
        return Table(table.header, rows, table.newline, table.bom), new

    before: dict[int, list[str]] = {}
    after: dict[int, list[str]] = {}
    for i, (record, m) in enumerate(zip(export, matched, strict=True)):
        if m is not None:
            continue
        below = next((x for x in matched[i + 1 :] if x is not None), None)
        if below is not None:
            before.setdefault(below, []).append(record)
        else:
            above = next(x for x in reversed(matched[:i]) if x is not None)
            after.setdefault(above, []).append(record)
    rows = []
    for i, record in enumerate(table.rows):
        rows += [*before.get(i, []), record, *after.get(i, [])]
    return Table(table.header, rows, table.newline, table.bom), new


def plan(shared: Shared, export: Table, where: str) -> list[YearPlan]:
    """What an export adds to each year file, newest year first."""
    dates = [shared.date(r) for r in export.rows]
    bad = next((r for r, d in zip(export.rows, dates, strict=True) if not d), None)
    if bad is not None:
        raise ConfigError(f"{where}: cannot read the date of: {bad}")
    rows = list(zip(dates, export.rows, strict=True))
    if dates and dates[0] < dates[-1]:
        rows.reverse()  # oldest first: keep year files newest first
    plans = []
    for year in sorted({d[:4] for d in dates}, reverse=True):
        path = shared.source.data(int(year))
        exists = path.is_file()
        table = (
            read_table(path, shared.directives)
            if exists
            else Table(export.header, [], export.newline, export.bom)
        )
        records = [r for d, r in rows if d.startswith(year)]
        merged, new = merge(shared, table, records)
        plans.append(YearPlan(int(year), merged, new, exists))
    return plans


# rollback
@contextmanager
def rollback(paths: Iterable[Path]) -> Iterator[None]:
    """Restore PATHS as they were if the block raises; new files are removed."""
    with tempfile.TemporaryDirectory() as backup:
        saved: dict[Path, Path | None] = {}
        created: set[Path] = set()
        for i, path in enumerate(dict.fromkeys(paths)):
            created.update(d for d in path.parents if not d.exists())
            copy = Path(backup) / str(i)
            saved[path] = copy if path.exists() else None
            if path.exists():
                shutil.copy2(path, copy)
        try:
            yield
        except BaseException:
            for path, saved_copy in saved.items():
                if saved_copy is not None:
                    shutil.copy2(saved_copy, path)
                else:
                    path.unlink(missing_ok=True)
            for d in sorted(created, key=lambda p: len(p.parts), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            raise


# regenerate
def relative(path: Path, config: Config) -> str:
    base = config.path.parent if config.path else Path.cwd()
    return os.path.relpath(path, base)


def generate(config: Config, source: Source, years: Iterable[int]) -> None:
    """Write each year's journal from its data file and one-offs; raise Failed."""
    mark = Shared(source).output_mark()
    style = source.commodity_style
    for year in years:
        data, one_offs = source.data(year), source.one_offs(year)
        missing = [str(p) for p in (data, one_offs) if not p.is_file()]
        if missing:
            raise Failed(f"no such file: {', '.join(missing)}")
        proc = hledger.run(
            data, "--rules", str(one_offs), "print", *(["-c", style] if style else [])
        )
        if proc.returncode != 0:
            raise Failed((proc.stdout + proc.stderr).strip())
        text = HEADER.format(
            label=source.label,
            year=year,
            data=data.name,
            one_offs=one_offs.name,
            data_path=relative(data, config),
            one_offs_path=relative(one_offs, config),
            style=f" -c {style}" if style else "",
            mark=mark,
        )
        text += proc.stdout
        output = source.output(year)
        if not output.is_file() or output.read_text() != text:
            output.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(output, text)


def check(config: Config) -> None:
    """`hledger check` on the main journal; raise Failed if it fails."""
    ok, msg = hledger.check(config.journal)
    if not ok:
        raise Failed(msg)


def not_included(config: Config, outputs: Iterable[Path]) -> list[Path]:
    """Generated journals the main journal does not include."""
    proc = hledger.run(config.journal, "files")
    files = {Path(f).resolve() for f in proc.stdout.split("\n") if f}
    return [p for p in outputs if p.resolve() not in files]


def skeleton(source: Source, year: int) -> str:
    """A new year's one-offs file; the table is added with its first line."""
    shared_rules = os.path.relpath(source.rules, source.one_offs(year).parent)
    return (
        f"# {year} one-offs: single transactions the shared rules get wrong.\n"
        f"# Later rules win, so the table below overrides {shared_rules}.\n"
        f"\ninclude {shared_rules}\n"
    )


# import
def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _unmarked(config: Config, source: Source, p: YearPlan) -> int:
    """How many of the new rows the rules leave on the unmarked account."""
    one_offs = source.one_offs(p.year)
    rules_file = one_offs if one_offs.is_file() else source.rules
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "new.csv"
        csv_path.write_text(Table(p.table.header, p.new, p.table.newline).text())
        proc = hledger.run(csv_path, "--rules", str(rules_file), "print")
    if proc.returncode != 0:
        raise Failed((proc.stdout + proc.stderr).strip())
    return sum(1 for t in transactions(proc.stdout) if config.unmarked in t.accounts())


def run_import(config: Config, export: Path, source: Source, assume_yes: bool) -> int:
    """Merge EXPORT into SOURCE's year files and regenerate; returns an exit code."""
    if not export.is_file():
        raise ConfigError(f"{export} does not exist")
    if not config.journal.is_file():
        raise ConfigError(f"journal {config.journal} does not exist")
    hledger.require()
    shared = Shared(source)
    todo = [
        p
        for p in plan(shared, read_table(export, shared.directives), str(export))
        if p.new
    ]
    print(f"journal:  {config.journal}")
    print(f"export:   {export}")
    print(f"source:   {source.name} ({source.account})\n")
    if not todo:
        print("no new transactions")
        return 0
    try:
        for p in todo:
            unmarked = _unmarked(config, source, p)
            where = relative(source.data(p.year), config)
            print(
                f"{p.year}: {len(p.new)} new row(s) for {where}"
                f"{'' if p.exists else ' (new year)'}, "
                f"{unmarked} on {config.unmarked}"
            )
    except Failed as e:
        print(e, file=sys.stderr)
        return 1
    print()
    if not assume_yes and not _confirm("Add these and regenerate?"):
        print("aborted, nothing written")
        return 1

    years = [p.year for p in todo]
    paths = [f(y) for y in years for f in (source.data, source.one_offs, source.output)]
    try:
        with rollback(paths):
            for p in todo:
                data, one_offs = source.data(p.year), source.one_offs(p.year)
                data.parent.mkdir(parents=True, exist_ok=True)
                write_table(data, p.table, shared.directives)
                if not one_offs.exists():
                    one_offs.parent.mkdir(parents=True, exist_ok=True)
                    one_offs.write_text(skeleton(source, p.year))
            generate(config, source, years)
            check(config)
    except Failed as e:
        print(e, file=sys.stderr)
        print(
            "\nerror: the import failed its check, so it was rolled back.\n\n"
            f"A failing balance assertion means the journal's {source.account}\n"
            "balance did not match the bank's running balance at that point:\n"
            "a transaction before it is missing, duplicated or has the wrong\n"
            "amount. Find it with\n"
            f"    hledger -f {config.journal} bal {source.account} -DH -b <date>",
            file=sys.stderr,
        )
        return 1

    for path in not_included(config, [source.output(y) for y in years]):
        print(f"warning: {config.journal.name} does not include {path}")
    added = sum(len(p.new) for p in todo)
    print(f"Added {added} row(s). Review what is on {config.unmarked}:")
    print("    hledger-review")
    return 0


def run_generate(config: Config, sources: Iterable[Source], years: list[int]) -> int:
    """Regenerate YEARS (default: all) of SOURCES, then check; an exit code."""
    hledger.require()
    todo = [(s, y) for s in sources for y in years or s.years() if s.data(y).is_file()]
    if not todo:
        print("nothing to generate: no data files found", file=sys.stderr)
        return 1
    try:
        with rollback(s.output(y) for s, y in todo):
            for s, y in todo:
                generate(config, s, [y])
                print(f"generated {relative(s.output(y), config)}")
            check(config)
    except Failed as e:
        print(e, file=sys.stderr)
        print(
            "\nerror: the check failed, the journals were rolled back.", file=sys.stderr
        )
        return 1
    return 0


# review
Key = tuple[str, Decimal | None, Decimal | None]  # date, amount, balance


@dataclass
class Item:
    """One transaction to review, and where it came from."""

    source: Source
    year: int
    index: int  # position in the year's generated journal
    txn: Txn

    @property
    def key(self) -> str:
        return f"{self.source.name}:{self.year}:{self.index}"


def year_transactions(source: Source, year: int) -> list[Txn]:
    path = source.output(year)
    return transactions(path.read_text()) if path.is_file() else []


def load_items(config: Config, since: str | None, visit_all: bool) -> list[Item]:
    """Transactions of the generated journals on the unmarked account (or all)."""
    items = []
    for source in config.sources.values():
        for year in source.years():
            for i, t in enumerate(year_transactions(source, year)):
                if since and t.date < since:
                    continue
                if visit_all or config.unmarked in t.accounts():
                    items.append(Item(source, year, i, t))
    return items


def locate(shared: Shared, year: int, txns: list[Txn], index: int) -> rules.Row | None:
    """The CSV row behind TXNS[INDEX]; see `locate_all`."""
    txn = txns[index]
    peers = [t for t in txns if t.date == txn.date]  # rows only pair within a date
    k = next(i for i, t in enumerate(peers) if t is txn)
    return locate_all(shared, year, peers)[k]


def locate_all(shared: Shared, year: int, txns: list[Txn]) -> list[rules.Row | None]:
    """The CSV row behind each of TXNS: same date, amount and balance assertion.

    Rows that tie on all three pair up in order, the way hledger reads them.
    Without balance assertions only the date and the amount are compared.
    """
    source, ev = shared.source, shared.evaluator
    out_mark, csv_mark = shared.output_mark(), shared.directives.decimal_mark or "."

    def journal_key(t: Txn) -> Key | None:
        p = next((p for p in t.postings() if p.account == source.account), None)
        if p is None:
            return None
        amount = rules.number(p.amount, out_mark)
        return t.date, amount, rules.number(p.assertion, out_mark)

    rows = [
        shared.row(r) for r in read_table(source.data(year), shared.directives).rows
    ]
    if len(rows) > 1 and str(ev.date(rows[0])) > str(ev.date(rows[-1])):
        rows.reverse()  # hledger reads a newest-first file bottom up

    keys = [journal_key(t) for t in txns]
    wanted = {k for k in keys if k is not None}
    dates = {k[0] for k in wanted}
    asserted = {k for k in wanted if k[2] is not None}
    queues: dict[Key, list[rules.Row]] = {}
    for row in rows:
        date = ev.date(row)
        if date is None or date.isoformat() not in dates:
            continue
        values = ev.assigned(shared.parsed, row)
        n = rules.posting(values, source.account)
        if n is None:
            continue
        amount = rules.posting_amount(values, n, csv_mark)
        balance = rules.posting_balance(values, n, csv_mark)
        key = (date.isoformat(), amount, balance)
        if key not in asserted:
            key = (date.isoformat(), amount, None)
        queues.setdefault(key, []).append(row)
    return [queues[k].pop(0) if k is not None and queues.get(k) else None for k in keys]
