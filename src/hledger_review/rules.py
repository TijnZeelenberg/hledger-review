"""Read hledger CSV rules and data: per-rule statistics, field values per row.

Matching follows hledger: case-insensitive POSIX extended regexes, matcher
lines OR'ed, `&` and `&&` AND'ed, and a later rule overrides the fields an
earlier one assigned. `include` directives are not followed.
"""

import csv
import datetime as dt
import io
import re
import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import cached_property
from pathlib import Path

COMMENT_START = ("#", ";", "*")
IF_LINE = re.compile(r"^if(\s|$)")
IF_TABLE = re.compile(r"^if[^\w\s]")
ASSIGNMENT = re.compile(r"^\s+(\S+)")
MATCHER = re.compile(r"^(!\s*)?(?:%(\S+)\s+)?(.*)$")
# hledger fields a rules file can assign, e.g. `amount2-in` or `balance1`
FIELD = re.compile(
    r"^(date2?|status|code|description|comment\d*|account\d+|currency\d*"
    r"|(amount|balance)\d*(-in|-out)?)$"
)
REFERENCE = re.compile(r"%(\w[\w-]*)")
POSIX_CLASSES = {
    "[:alpha:]": "a-zA-Z",
    "[:digit:]": "0-9",
    "[:alnum:]": "a-zA-Z0-9",
    "[:upper:]": "A-Z",
    "[:lower:]": "a-z",
    "[:space:]": r"\s",
    "[:xdigit:]": "0-9A-Fa-f",
    "[:punct:]": re.escape(string.punctuation),
}
DEFAULT_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d")


# regexes
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile an hledger (POSIX extended + GNU word boundary) regex, ignoring case."""
    for posix, py in POSIX_CLASSES.items():
        pattern = pattern.replace(posix, py)
    pattern = pattern.replace(r"\<", r"\b").replace(r"\>", r"\b")
    return re.compile(pattern, re.IGNORECASE)


@dataclass(frozen=True)
class Matcher:
    negate: bool
    field: str | None  # CSV field name or number; None matches the whole record
    pattern: str

    @cached_property
    def regex(self) -> re.Pattern[str]:
        return compile_pattern(self.pattern)


def parse_matchers(lines: Iterable[str]) -> list[list[Matcher]]:
    """Matcher lines to OR'ed groups of AND'ed matchers."""
    groups: list[list[Matcher]] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        and_above = text.startswith("&")
        if and_above:
            text = text.lstrip("&").lstrip()
        matchers = []
        for part in re.split(r"\s+&&\s+", text):
            m = MATCHER.match(part.strip())
            assert m is not None  # the pattern matches any string
            matchers.append(Matcher(bool(m.group(1)), m.group(2), m.group(3)))
        if and_above and groups:
            groups[-1].extend(matchers)
        else:
            groups.append(matchers)
    return groups


# rules file
@dataclass(frozen=True)
class Rule:
    """An `if` block, or one line of an if table."""

    line: int  # 1-based, the `if` line or the table line
    groups: list[list[Matcher]]
    assigns: frozenset[str]  # hledger field names, lowercased
    values: tuple[tuple[str, str], ...] = ()  # (field, value template)


@dataclass
class Directives:
    """The top-level rules needed to read the CSV the way hledger does."""

    separator: str = ","
    skip: int = 0
    fields: list[str] = field(default_factory=list)
    encoding: str = "utf-8-sig"
    date_format: str | None = None
    decimal_mark: str | None = None
    assignments: dict[str, str] = field(default_factory=dict)  # top-level fields


@dataclass
class RulesFile:
    rules: list[Rule]
    directives: Directives


def _directive(d: Directives, line: str) -> None:
    name, _, value = line.partition(" ")
    value = value.strip()
    if name == "separator":
        d.separator = {"TAB": "\t", "\\t": "\t", "SPACE": " "}.get(
            value, value[:1] or ","
        )
    elif name == "skip":
        d.skip = int(value) if value.isdigit() else 1
    elif name == "fields":
        d.fields = [f.strip() for f in value.split(",")]
    elif name == "encoding" and value:
        d.encoding = value
    elif name == "date-format" and value:
        d.date_format = value
    elif name == "decimal-mark" and value:
        d.decimal_mark = value[:1]
    elif FIELD.match(name.lower()):
        d.assignments[name.lower()] = value


def parse(text: str) -> RulesFile:
    """The `if` blocks, if table lines and CSV directives of a rules file."""
    lines = text.split("\n")
    result = RulesFile([], Directives())

    def comment(line: str) -> bool:
        return line.lstrip().startswith(COMMENT_START)

    i = 0
    while i < len(lines):
        line = lines[i]
        if IF_TABLE.match(line):
            sep = line[2]
            names = [n.strip().lower() for n in line[3:].split(sep)]
            i += 1
            while i < len(lines) and lines[i].strip():
                if not comment(lines[i]):
                    matcher, *cells = lines[i].split(sep)
                    cell_values = tuple(
                        (n, c.strip()) for n, c in zip(names, cells, strict=False)
                    )
                    rule = Rule(
                        i + 1, parse_matchers([matcher]), frozenset(names), cell_values
                    )
                    result.rules.append(rule)
                i += 1
        elif IF_LINE.match(line):
            start = i
            matchers = [line[2:]]
            values: list[tuple[str, str]] = []
            i += 1
            # matcher lines, then indented assignments; a blank line ends the block
            while i < len(lines) and lines[i].strip() and lines[i][0] not in " \t":
                if not comment(lines[i]):
                    matchers.append(lines[i])
                i += 1
            while i < len(lines) and lines[i].strip() and lines[i][0] in " \t":
                m = ASSIGNMENT.match(lines[i])
                if m and not comment(lines[i]):
                    rest = lines[i][m.end() :].strip()
                    values.append((m.group(1).lower(), rest))
                i += 1
            rule = Rule(
                start + 1,
                parse_matchers(matchers),
                frozenset(n for n, _ in values),
                tuple(values),
            )
            result.rules.append(rule)
        else:
            if line and not line[0].isspace() and not comment(line):
                _directive(result.directives, line)
            i += 1
    return result


# csv data
def normalise(value: str) -> str:
    """Collapse whitespace; hledger turns newlines inside fields into spaces."""
    return " ".join(value.split())


@dataclass(frozen=True)
class Row:
    """One CSV record."""

    values: tuple[str, ...]

    @cached_property
    def key(self) -> tuple[str, ...]:
        return tuple(normalise(v) for v in self.values)

    @cached_property
    def record(self) -> str:
        """What a whole-record matcher sees: the values, comma-separated."""
        return ",".join(self.values)


def read_rows(paths: Iterable[Path], directives: Directives) -> list[Row]:
    """CSV records from PATHS, header lines skipped, duplicates (overlap) dropped."""
    seen: set[tuple[str, ...]] = set()
    rows: list[Row] = []
    for path in paths:
        text = path.read_text(encoding=directives.encoding, errors="replace")
        reader = csv.reader(io.StringIO(text), delimiter=directives.separator)
        records = [r for r in reader if any(r)]
        for values in records[directives.skip :]:
            row = Row(tuple(v.strip() for v in values))
            if row.key not in seen:
                seen.add(row.key)
                rows.append(row)
    return rows


# matching
class Evaluator:
    """Evaluates rules against rows using the file's `fields` names."""

    def __init__(self, directives: Directives) -> None:
        self.index = {
            name.lower(): i
            for i, name in reversed(list(enumerate(directives.fields)))
            if name and name != "_"
        }
        fmt = directives.date_format
        self.date_formats = (fmt.replace("%-", "%"),) if fmt else DEFAULT_DATE_FORMATS

    def value(self, row: Row, name: str | None) -> str:
        if name is None:
            return row.record
        i = int(name) - 1 if name.isdigit() else self.index.get(name.lower(), -1)
        return row.values[i] if 0 <= i < len(row.values) else ""

    def matches(self, rule: Rule, row: Row) -> bool:
        return any(
            all(
                bool(m.regex.search(self.value(row, m.field))) != m.negate
                for m in group
            )
            for group in rule.groups
        )

    def assigned(self, rules: "RulesFile", row: Row) -> dict[str, str]:
        """Every field the rules assign for ROW, as hledger would, interpolated."""
        templates = dict(rules.directives.assignments)
        for rule in rules.rules:
            if rule.values and self.matches(rule, row):
                templates.update(rule.values)
        return {k: self.interpolate(v, row) for k, v in templates.items()}

    def interpolate(self, template: str, row: Row) -> str:
        """Replace `%name` and `%N` references with the row's values."""

        def ref(m: re.Match[str]) -> str:
            name = m.group(1)
            known = name.isdigit() or name.lower() in self.index
            return self.value(row, name) if known else m.group(0)

        return REFERENCE.sub(ref, template)

    def date(self, row: Row) -> dt.date | None:
        """The row's `date` field, if there is one and it parses."""
        value = self.value(row, "date") if "date" in self.index else ""
        for fmt in self.date_formats:
            try:
                return dt.datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None


# amounts
def number(text: str, decimal_mark: str = ".") -> Decimal | None:
    """The number in an hledger amount like `-€1.234,50`; None if there is none."""
    body = text.split("@")[0]
    digits = "".join(c for c in body if c.isdigit() or c == decimal_mark)
    if not any(c.isdigit() for c in digits):
        return None
    sign = -1 if body.count("-") % 2 else 1
    try:
        return sign * Decimal(digits.replace(decimal_mark, "."))
    except InvalidOperation:
        return None


def posting(values: dict[str, str], account: str) -> int | None:
    """Which posting N the rules give ACCOUNT, from the `accountN` assignments."""
    found = [int(k[7:]) for k, v in values.items() if k[7:].isdigit() and v == account]
    return min(found) if found else None


def posting_amount(values: dict[str, str], n: int, mark: str) -> Decimal | None:
    """Posting N's amount from `amountN`, `amountN-in/-out` or `amount`."""
    for suffix in (str(n), "") if n in (1, 2) else (str(n),):
        sign = -1 if not suffix and n == 2 else 1
        if f"amount{suffix}" in values:
            amount = number(values[f"amount{suffix}"], mark)
            return None if amount is None else sign * amount
        amount_in = number(values.get(f"amount{suffix}-in", ""), mark)
        amount_out = number(values.get(f"amount{suffix}-out", ""), mark)
        if amount_in:
            return sign * amount_in
        if amount_out:
            return -sign * amount_out
    return None


def posting_balance(values: dict[str, str], n: int, mark: str) -> Decimal | None:
    """Posting N's balance assertion, from `balanceN` (or `balance` for 1)."""
    text = values.get(f"balance{n}", values.get("balance", "") if n == 1 else "")
    return number(text, mark)


# statistics
@dataclass
class RuleStats:
    rule: Rule
    rows: int = 0
    overridden: int = 0  # rows where later rules reassign every field it sets
    overridden_by: set[int] = field(default_factory=set)  # their lines
    last: dt.date | None = None
    error: str | None = None


@dataclass
class Report:
    rules: list[RuleStats]
    rows: int
    fallthrough: int  # rows where no rule assigns the category field


def stats(rules: RulesFile, rows: list[Row], category: str | None = None) -> Report:
    """Match every rule against every row, as hledger would."""
    evaluator = Evaluator(rules.directives)
    result = [RuleStats(r) for r in rules.rules]
    for s in result:
        try:
            for group in s.rule.groups:
                for m in group:
                    _ = m.regex
        except re.error as e:
            s.error = f"regex not understood: {e}"
    live = [s for s in result if s.error is None]

    fallthrough = 0
    for row in rows:
        matched = [s for s in live if evaluator.matches(s.rule, row)]
        date = evaluator.date(row)
        if category and not any(category in s.rule.assigns for s in matched):
            fallthrough += 1
        for i, s in enumerate(matched):
            s.rows += 1
            if date and (s.last is None or date > s.last):
                s.last = date
            later = [t for t in matched[i + 1 :] if t.rule.assigns & s.rule.assigns]
            reassigned = frozenset().union(*(t.rule.assigns for t in later))
            if s.rule.assigns and s.rule.assigns <= reassigned:
                s.overridden += 1
                s.overridden_by.update(t.rule.line for t in later)
    return Report(result, len(rows), fallthrough)
