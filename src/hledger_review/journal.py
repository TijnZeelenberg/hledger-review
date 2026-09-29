"""A minimal journal model: transaction blocks and the fields the review shows.

Only transaction blocks are parsed; every other line is kept verbatim.
"""

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

DATE_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# indent, account (ends at 2+ whitespace chars, like hledger), separator, rest
POSTING = re.compile(r"^(\s+)(\S.*?)(?:(\s{2,})(.*))?$")
# date (+ secondary date), optional status, optional (code), then description
HEAD = re.compile(r"^(\S+\s+(?:[*!]\s+)?(?:\([^)]*\)\s+)?)(.*)$")


class Roles(NamedTuple):
    """Which accounts mean "not yet categorised" and "the imported bank account"."""

    unmarked: str
    assets: frozenset[str]


class Posting(NamedTuple):
    line: int
    indent: str
    account: str
    sep: str
    rest: str

    @property
    def amount(self) -> str:
        """Amount without balance assertion or comment, e.g. `€-23.99`."""
        return self.rest.split(";")[0].split("=")[0].strip()

    @property
    def assertion(self) -> str:
        """The balance assertion's amount, e.g. `€976.01`, or an empty string."""
        return self.rest.split(";")[0].partition("=")[2].lstrip("=*").strip()


class Txn:
    """One transaction block: the date line plus its indented continuation lines."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def _split_head(self) -> tuple[str, str, str]:
        line = self.lines[0]
        idx = line.find(";")
        head, comment = (line, "") if idx < 0 else (line[:idx].rstrip(), line[idx:])
        m = HEAD.match(head)
        if m:
            return m.group(1), m.group(2), comment
        return head + " ", "", comment

    @property
    def date(self) -> str:
        return self.lines[0].split()[0].split("=")[0]

    @property
    def description(self) -> str:
        return self._split_head()[1]

    @property
    def comment(self) -> str:
        return self._split_head()[2].lstrip("; ").strip()

    def postings(self) -> Iterator[Posting]:
        for i, line in enumerate(self.lines[1:], start=1):
            if line.lstrip().startswith(";") or not line.strip():
                continue
            m = POSTING.match(line)
            if m:
                yield Posting(
                    i, m.group(1), m.group(2), m.group(3) or "", m.group(4) or ""
                )

    def accounts(self) -> list[str]:
        return [p.account for p in self.postings()]

    # roles
    def asset_posting(self, roles: Roles) -> Posting | None:
        return next((p for p in self.postings() if p.account in roles.assets), None)

    def target_posting(self, roles: Roles) -> Posting | None:
        """The posting to recategorise: the unmarked one, else the first non-asset."""
        ps = list(self.postings())
        for p in ps:
            if p.account == roles.unmarked:
                return p
        return next((p for p in ps if p.account not in roles.assets), None)

    def account(self, roles: Roles) -> str:
        p = self.target_posting(roles)
        return p.account if p else roles.unmarked

    def amount(self, roles: Roles) -> str:
        """Amount on the bank side, falling back to the first explicit amount."""
        p = self.asset_posting(roles)
        if p and p.amount:
            return p.amount
        return next((p.amount for p in self.postings() if p.amount), "")

    def text(self) -> str:
        return "\n".join(self.lines)


def parse_journal(text: str) -> list[str | Txn]:
    """Split journal text into verbatim lines and Txn blocks."""
    segments: list[str | Txn] = []
    block: Txn | None = None
    for line in text.split("\n"):
        if DATE_LINE.match(line):
            block = Txn([line])
            segments.append(block)
        elif block is not None and line[:1] in (" ", "\t") and line.strip():
            block.lines.append(line)
        else:
            block = None
            segments.append(line)
    return segments


def transactions(text: str) -> list[Txn]:
    """Only the transactions of a journal, in order."""
    return [s for s in parse_journal(text) if isinstance(s, Txn)]


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a crash never leaves half a journal."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def declare_account(path: Path, account: str) -> None:
    """Add an `account` directive to an accounts file, keeping the plain ones sorted.

    Directives with a comment (e.g. `; type: A`) and everything before the first
    plain directive stay where they are; plain directives after it are sorted.
    """
    lines = path.read_text().rstrip("\n").split("\n") if path.exists() else []
    first = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith("account ") and ";" not in line
        ),
        len(lines),
    )
    decls = sorted({*(line for line in lines[first:] if line), f"account {account}"})
    write_atomic(path, "\n".join(lines[:first] + decls) + "\n")
