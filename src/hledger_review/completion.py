"""Completion for the form's text fields, from what the journals already use.

The candidates come from one `hledger print` at startup, ranked by how often
they occur. Dropdowns only open in insert mode, so a pre-filled or copied
value does not pop one up.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from textual_autocomplete import AutoComplete, DropdownItem, TargetState

from hledger_review import hledger
from hledger_review.history import comment_tags
from hledger_review.journal import Txn, transactions
from hledger_review.widgets import ModalInput


@dataclass
class Candidates:
    """Descriptions and tags, most frequent first."""

    descriptions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def txn_comment(txn: Txn) -> str:
    """The transaction's comment lines (not its postings'), joined by newlines."""
    lines = [txn.comment]
    for line in txn.lines[1:]:
        if not line.lstrip().startswith(";"):
            break
        lines.append(line.lstrip()[1:].strip())
    return "\n".join(lines)


def candidates(txns: list[Txn], unmarked: str) -> Candidates:
    """Rank descriptions and tag names and `name:value` pairs by frequency.

    Descriptions of transactions still on UNMARKED are raw bank names: skipped.
    """
    descriptions: Counter[str] = Counter(
        t.description for t in txns if t.description and unmarked not in t.accounts()
    )
    tags: Counter[str] = Counter()
    for t in txns:
        for name, value in dict.fromkeys(comment_tags(txn_comment(t))):
            if name.startswith("_"):
                continue
            tags[name] += 1
            if value:
                tags[f"{name}:{value}"] += 1
    return Candidates(
        [d for d, _ in descriptions.most_common()], [t for t, _ in tags.most_common()]
    )


def load(journal: Path, unmarked: str) -> Candidates:
    """Candidates from the whole journal; none without hledger or on an error."""
    if not hledger.executable():
        return Candidates()
    proc = hledger.run(journal, "print")
    if proc.returncode != 0:
        return Candidates()
    return candidates(transactions(proc.stdout), unmarked)


def rank(query: str, pool: list[str]) -> list[tuple[str, int]]:
    """POOL entries containing QUERY, prefix matches first, else in POOL's order.

    Returns each match with where QUERY starts in it, ignoring case.
    """
    q = query.casefold()
    if not q:
        return []
    found = [(c, c.casefold().find(q)) for c in pool]
    hits = [(c, i) for c, i in found if i >= 0 and c.casefold() != q]
    return sorted(hits, key=lambda h: h[1] != 0)


def tag_token(text: str, cursor: int) -> tuple[int, str]:
    """Where the comma-separated token before CURSOR starts, and the token."""
    start = text.rfind(",", 0, cursor) + 1
    while start < cursor and text[start] == " ":
        start += 1
    return start, text[start:cursor]


class ModalAutoComplete(AutoComplete):
    """An AutoComplete that only opens while its field is in insert mode."""

    def should_show_dropdown(self, search_string: str) -> bool:
        target = self.target
        editing = isinstance(target, ModalInput) and target.editing
        return editing and super().should_show_dropdown(search_string)


class Complete(ModalAutoComplete):
    """A dropdown over ranked candidates: prefix matches, then substring ones."""

    def __init__(self, target: str, pool: Callable[[], list[str]]) -> None:
        super().__init__(target, candidates=None)
        self.pool = pool

    def get_candidates(self, target_state: TargetState) -> list[DropdownItem]:
        return [DropdownItem(c) for c in self.pool()]

    def get_matches(
        self,
        target_state: TargetState,
        candidates: list[DropdownItem],
        search_string: str,
    ) -> list[DropdownItem]:
        hits = rank(search_string, [c.value for c in candidates])
        n = len(search_string)
        return [
            DropdownItem(
                self.apply_highlights(DropdownItem(c).main, tuple(range(i, i + n)))
            )
            for c, i in hits[:50]
        ]


class TagComplete(Complete):
    """Completes the tag under the cursor, keeping the others."""

    def get_search_string(self, target_state: TargetState) -> str:
        return tag_token(target_state.text, target_state.cursor_position)[1]

    def apply_completion(self, value: str, state: TargetState) -> None:
        start, _ = tag_token(state.text, state.cursor_position)
        end = state.text.find(",", state.cursor_position)
        rest = "" if end < 0 else state.text[end:]
        target = self.target
        target.value = state.text[:start] + value + rest
        target.cursor_position = start + len(value)
        new_state = self._get_target_state()
        self._rebuild_options(new_state, self.get_search_string(new_state))
