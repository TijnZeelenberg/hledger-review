"""Earlier bookings per bank payee: the history panel and pre-filled suggestions.

A generated transaction's description may be a friendly name set by a rule
or a one-off, so the raw bank name comes from its CSV row: the shared rules'
top-level `description` template (e.g. `%payee`), filled in from that row.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from hledger_review import rules
from hledger_review.config import Config, ConfigError, Source
from hledger_review.importer import Shared, locate_all, year_transactions
from hledger_review.journal import Roles, Txn

# a word and a colon, the value runs to a comma or the line end, as in hledger;
# but not `Omschrijving: text` with a space after the colon, like bank text
TAG = re.compile(r"(?<![^\s,])([^\s,:]+):(?![ \t])([^,\n]*)")
LIMIT = 5


@dataclass(frozen=True)
class Booking:
    """One generated transaction, with its raw bank name."""

    key: str  # the review Item's key, `source:year:index`
    date: str
    amount: str  # bank side, e.g. `€-23.99`
    number: Decimal | None
    account: str
    description: str
    tags: str  # `reis:gent, vast`
    payee: str
    categorised: bool


@dataclass(frozen=True)
class Suggestion:
    """The most frequent earlier booking of a payee."""

    booking: Booking
    count: int  # bookings with the same description, account and tags
    total: int  # bookings of the payee

    @property
    def hint(self) -> str:
        if self.count == self.total:
            return f"suggested from {self.count} earlier"
        return f"suggested from {self.count} of {self.total} earlier"


def comment_tags(comment: str) -> list[tuple[str, str]]:
    """The `name:value` tags of a comment, as hledger reads them."""
    return [(n, v.strip()) for n, v in TAG.findall(comment)]


def format_tags(tags: Iterable[tuple[str, str]]) -> str:
    """Tags as the form takes them: `reis:gent, vast`."""
    return ", ".join(f"{n}:{v}" if v else n for n, v in dict.fromkeys(tags))


def short_account(account: str) -> str:
    """Parents to their first letter, to fit a narrow table: `e:f:groceries`."""
    *parents, leaf = account.split(":")
    return ":".join([p[:1] for p in parents] + [leaf])


def normal(payee: str) -> str:
    """A bank name to compare: whitespace collapsed, case folded."""
    return " ".join(payee.split()).casefold()


def bookings(config: Config, source: Source, year: int) -> list[Booking]:
    """Every transaction of a generated year journal, with its bank name."""
    txns = year_transactions(source, year)
    if not txns or not source.data(year).is_file():
        return []
    shared = Shared(source)
    template = shared.directives.assignments.get("description", "")
    rows = locate_all(shared, year, txns)
    mark = shared.output_mark()
    found = []
    for i, (t, row) in enumerate(zip(txns, rows, strict=True)):
        payee = shared.evaluator.interpolate(template, row) if row else ""
        found.append(booking(config.roles, f"{source.name}:{year}:{i}", t, payee, mark))
    return found


def booking(roles: Roles, key: str, txn: Txn, payee: str, mark: str) -> Booking:
    """TXN as a Booking under KEY; MARK is the journal's decimal mark."""
    amount = txn.amount(roles)
    account = txn.account(roles)
    return Booking(
        key=key,
        date=txn.date,
        amount=amount,
        number=rules.number(amount, mark),
        account=account,
        description=txn.description,
        tags=format_tags(comment_tags(txn.comment)),
        payee=payee,
        categorised=account != roles.unmarked,
    )


class History:
    """Bookings of every source and year, looked up by payee or amount."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.years: dict[tuple[str, int], list[Booking]] = {}
        for source in config.sources.values():
            for year in source.years():
                self.refresh(source, year)

    def refresh(self, source: Source, year: int) -> None:
        """Re-read one regenerated year."""
        try:
            self.years[source.name, year] = bookings(self.config, source, year)
        except (ConfigError, re.error):  # the review works without history
            self.years[source.name, year] = []
        self.by_key = {b.key: b for bs in self.years.values() for b in bs}

    def earlier(self, key: str) -> tuple[list[Booking], bool]:
        """Categorised bookings like KEY's, newest first, and whether by payee.

        Same bank name if there are any, else the same amount give or take a cent.
        """
        me = self.by_key.get(key)
        if me is None:
            return [], False
        others = [b for b in self.by_key.values() if b.categorised and b.key != key]
        others.sort(key=lambda b: b.date, reverse=True)
        payee = normal(me.payee)
        same = [b for b in others if payee and normal(b.payee) == payee]
        if same:
            return same, True
        if me.number is None:
            return [], False
        cent = Decimal("0.01")
        return [
            b
            for b in others
            if b.number is not None and abs(b.number - me.number) <= cent
        ], False

    def suggest(self, key: str) -> Suggestion | None:
        """The suggestion for KEY, from bookings of the same payee only."""
        found, by_payee = self.earlier(key)
        return suggest(found) if by_payee else None


def suggest(found: list[Booking]) -> Suggestion | None:
    """The most frequent description, account and tags; ties go to the newest."""
    if not found:
        return None
    counts = Counter((b.description, b.account, b.tags) for b in found)
    newest = max(
        found,
        key=lambda b: (counts[b.description, b.account, b.tags], b.date),
    )
    count = counts[newest.description, newest.account, newest.tags]
    return Suggestion(newest, count, len(found))
