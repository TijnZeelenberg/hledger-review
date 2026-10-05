"""The review TUI: give imported transactions a description and an account.

A save never edits a journal: it writes a one-off line (or a rule) to a rules
file, regenerates the affected years and reloads them.
"""

import os
import re
import shlex
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection
from textual_autocomplete import DropdownItem

from hledger_review import completion, hledger, rules, theme
from hledger_review.completion import Candidates, Complete, TagComplete
from hledger_review.config import Config, ConfigError, Source
from hledger_review.history import History, short_account
from hledger_review.importer import (
    Failed,
    Item,
    Shared,
    generate,
    locate,
    rollback,
    year_transactions,
)
from hledger_review.journal import declare_account
from hledger_review.widgets import FormInput, FormRadioSet, FormSelectionList, ListTable


def run_editor(command: list[str]) -> None:
    subprocess.run(command, check=False)


def amount_text(amount: str) -> Text:
    return Text(amount, style="bold red" if "-" in amount else "bold green")


LABEL_WIDTH = 13  # .field in app.tcss, so details line up with the inputs


class SaveError(Exception):
    """Why a save could not be written."""


def rules_safe(*values: str) -> None:
    """Refuse values that would break a rules file line or table."""
    for value in values:
        if "|" in value or "\n" in value:
            raise SaveError(f"'|' and line breaks cannot go in a rules file: {value!r}")


class ReviewApp(App[int]):
    """Review transactions one by one; returns how many were changed."""

    TITLE = "hledger-review"
    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        # vim-like on the list; the form is a plain form (Tab between fields,
        # fields take text right away), so its letters are never commands
        Binding("escape", "escape", "List", show=False),
        Binding("l", "to_form", "Form", show=False),
        Binding("w", "save", "Write"),
        Binding("ctrl+s", "save", "Write", show=False),
        # n/N find the next/previous match while a search is shown, else n skips
        Binding("n", "search_next(1)", "Next match"),
        Binding("N", "search_next(-1)", "Previous match", show=False),
        Binding("n", "skip", "Next"),
        Binding("slash", "search", "Search"),
        Binding("e", "edit_rules", "Edit rules"),
        Binding("E", "edit_one_offs", "Edit one-offs", show=False),
        Binding("q", "quit", "Quit"),
        # digits copy a row of the history panel into the form
        Binding("1", "copy(1)", "Copy", key_display="1-5"),
        *(Binding(str(n), f"copy({n})", show=False) for n in range(2, 6)),
    ]

    def __init__(
        self,
        config: Config,
        todo: list[Item],
        accounts: list[str] | None = None,
        history: History | None = None,
        candidates: Candidates | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.roles = config.roles
        self.todo = todo
        self.accounts = (
            accounts if accounts is not None else hledger.accounts(config.journal)
        )
        # "" = no rule, then every rule field any source knows, in config order
        self.rule_fields = [
            "",
            *dict.fromkeys(f for s in config.sources.values() for f in s.rule_fields),
        ]
        self.status: dict[str, str] = {}
        self.current: Item | None = None
        self.changed = 0
        self.confirm_new: str | None = None
        self.search_active = False
        self.search_origin = 0
        self.search_return: Widget | None = None
        self.history = history if history is not None else History(config)
        self.candidates = (
            candidates
            if candidates is not None
            else completion.load(config.journal, config.unmarked)
        )
        self.shown: list[str] = []  # keys of the history panel's rows

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield ListTable(id="list")
                yield DataTable(id="history", cursor_type="none")
            with Vertical(id="detail"):
                yield Static(id="info")
                yield Label("", id="suggestion", classes="note")
                with Horizontal(classes="row"):
                    yield Label("Description", classes="field")
                    yield FormInput(id="desc")
                with Horizontal(classes="row"):
                    yield Label("Account", classes="field")
                    yield FormInput(id="account")
                yield Label("", id="new-account", classes="note")
                with Horizontal(classes="row"):
                    yield Label("Tags", classes="field")
                    yield FormInput(id="tags", placeholder="e.g. reis:gent, vast")
                with Horizontal(classes="row", id="same-row"):
                    yield Label("Same name", classes="field")
                    yield FormSelectionList(id="same")
                with Horizontal(classes="row", id="rule-row"):
                    yield Label("Shared rule", classes="field")
                    with FormRadioSet(id="rule-field"):
                        yield RadioButton("none", value=True)
                        for f in self.rule_fields[1:]:
                            yield RadioButton(f"%{f}")
                with Horizontal(classes="row", id="pattern-row"):
                    yield Label("Pattern", classes="field")
                    yield FormInput(id="pattern", placeholder="regex", disabled=True)
                yield Label("", id="rule-error", classes="note")
        with Horizontal(id="statusbar"):
            yield Static("LIST", id="mode")
            with Horizontal(id="search-bar"):
                yield Label("/", id="slash")
                yield Input(id="search")
                yield Static(id="matches")
            yield Footer()

    def apply_theme(self) -> None:
        """Follow the active Omarchy theme, else keep a built-in one."""
        palette = theme.load_palette(theme.theme_dir())
        if palette is None:
            self.theme = theme.FALLBACK
            return
        # the ANSI colours first: the theme change rebuilds the filter using them
        ansi = theme.terminal_theme(palette)
        self.ansi_theme_dark = ansi
        self.ansi_theme_light = ansi
        self.register_theme(theme.textual_theme(palette))
        self.theme = "omarchy"

    def on_mount(self) -> None:
        self.apply_theme()
        self.sub_title = self.config.journal.name
        self.mount(
            completion.TypedAutoComplete(
                "#account",
                candidates=lambda _: [DropdownItem(a) for a in self.accounts],
            ),
            Complete("#desc", lambda: self.candidates.descriptions),
            TagComplete("#tags", lambda: self.candidates.tags),
        )
        history = self.query_one("#history", DataTable)
        history.can_focus = False
        history.display = False
        table = self.query_one("#list", DataTable)
        table.add_column(" ", key="status", width=1)
        table.add_column("Date", key="date")
        table.add_column("Amount", key="amount")
        table.add_column("Name", key="name")
        for item in self.todo:
            table.add_row(
                "",
                item.txn.date,
                amount_text(item.txn.amount(self.roles)),
                item.txn.description,
                key=item.key,
            )
        self.update_title()
        self.watch(self.screen, "focused", self.show_mode)
        table.focus()
        if self.todo:
            self.load(self.todo[0])
        else:
            self.show_empty()

    def show_empty(self) -> None:
        """Nothing to review: say so instead of showing an empty form."""
        detail = self.query_one("#detail", Vertical)
        detail.border_title = "Nothing to review"
        for widget in detail.children:
            widget.display = widget.id == "info"
        self.query_one("#info", Static).update(
            Text.assemble(
                ("All caught up. ", "bold green"),
                f"No transactions on {self.roles.unmarked}.\n\n",
                ("q to quit", "dim"),
            )
        )

    # state helpers
    def update_title(self) -> None:
        done = sum(1 for s in self.status.values() if s == "done")
        table = self.query_one("#list", DataTable)
        table.border_title = f"To review ({done}/{len(self.todo)})"

    def item_for_key(self, key: str) -> Item:
        return next(i for i in self.todo if i.key == key)

    def same_name(self, item: Item) -> list[Item]:
        return [
            o
            for o in self.todo
            if o is not item
            and o.source is item.source
            and o.txn.description == item.txn.description
            and o.key not in self.status
        ]

    def same_list(self) -> SelectionList[str]:
        return self.query_one("#same", SelectionList)

    def update_same_title(self) -> None:
        checklist = self.same_list()
        count = checklist.option_count
        checklist.border_title = f"Also apply to {len(checklist.selected)}/{count}"

    def load(self, item: Item) -> None:
        self.current = item
        t = item.txn
        info = Table.grid(padding=(0, 2))
        info.add_column(style="dim", width=LABEL_WIDTH)
        info.add_column()
        info.add_row("date", Text(t.date, style="bold"))
        info.add_row("amount", amount_text(t.amount(self.roles)))
        info.add_row("name", t.description)
        info.add_row("notes", t.comment or Text("-", style="dim"))
        info.add_row("account", Text(t.account(self.roles), style="cyan"))
        state = self.status.get(item.key)
        if state:
            style = "green" if state == "done" else "yellow"
            info.add_row("status", Text(state, style=style))
        self.query_one("#detail", Vertical).border_title = t.description
        self.query_one("#info", Static).update(info)
        self.query_one("#desc", Input).value = t.description
        account = self.query_one("#account", Input)
        account.value = ""
        account.placeholder = t.account(self.roles)
        self.query_one("#tags", Input).value = ""
        self.untype()
        same = self.same_name(item)
        checklist = self.same_list()
        checklist.clear_options()
        checklist.add_options(
            Selection(
                Text.assemble(
                    (o.txn.date, "bold"),
                    "  ",
                    amount_text(o.txn.amount(self.roles)),
                    "  ",
                    (o.txn.comment, "dim"),
                ),
                o.key,
            )
            for o in same
        )
        self.update_same_title()
        self.query_one("#same-row").display = bool(same)
        self.load_rule_fields(item.source)
        self.show_history(item)

    # history
    def show_history(self, item: Item) -> None:
        """Fill the history panel and pre-fill the form's untouched fields."""
        found, by_payee = self.history.earlier(item.key)
        found = found[:5]
        self.shown = [b.key for b in found]
        table = self.query_one("#history", DataTable)
        table.clear(columns=True)  # else columns keep the widest width ever shown
        table.add_columns("#", "Date", "Amount", "Account")
        # at most 20 wide, to leave room for the tags
        longest = max((len(b.description) for b in found), default=0)
        table.add_column("Description", width=max(11, min(longest, 20)))
        table.add_column("Tags")
        for n, b in enumerate(found, start=1):
            table.add_row(
                Text(str(n), style="dim"),
                b.date,
                amount_text(b.amount),
                Text(short_account(b.account)),  # Text: `:a:` is no emoji code
                Text(b.description, no_wrap=True, overflow="ellipsis"),
                Text(b.tags),
            )
        table.display = bool(found)
        what = "Same name" if by_payee else "Same amount ±€0.01"
        table.border_title = f"{what} · 1-{len(found)} to copy"
        hint = self.query_one("#suggestion", Label)
        suggestion = self.history.suggest(item.key)
        if suggestion is None or item.txn.account(self.roles) != self.roles.unmarked:
            hint.update("")
            hint.display = False
            return
        b = suggestion.booking
        hint.update(suggestion.hint)
        hint.display = True
        self.fill(b.description, b.account, b.tags, only_untouched=True)

    def fill(
        self, desc: str, account: str, tags: str, only_untouched: bool = False
    ) -> None:
        """Put values in the form; ONLY_UNTOUCHED skips fields changed since load."""
        t = self.current.txn if self.current else None
        defaults = {"#desc": t.description if t else "", "#account": "", "#tags": ""}
        for selector, value in (
            ("#desc", desc),
            ("#account", account),
            ("#tags", tags),
        ):
            field = self.query_one(selector, Input)
            if not only_untouched or field.value == defaults[selector]:
                field.value = value
        self.untype()

    def untype(self) -> None:
        """Values set by the review are not typed: they open no completion."""
        for field in self.query(FormInput):
            field.typed = False

    def action_copy(self, n: int) -> None:
        """1-5: copy that history row's account, description and tags."""
        if self.current is None or n > len(self.shown):
            self.bell()
            return
        b = self.history.by_key[self.shown[n - 1]]
        self.fill(b.description, b.account, b.tags)
        hint = self.query_one("#suggestion", Label)
        hint.update(f"copied from {b.date}")
        hint.display = True

    def load_rule_fields(self, source: Source) -> None:
        """Offer only the rule fields of this transaction's source."""
        fields = source.rule_fields
        buttons = list(self.query_one("#rule-field", RadioSet).query(RadioButton))
        for f, button in zip(self.rule_fields, buttons, strict=True):
            button.display = not f or f in fields
        buttons[0].value = True
        for widget in ("#rule-row", "#pattern-row", "#rule-error"):
            self.query_one(widget).display = bool(fields)
        self.query_one("#rule-error", Label).update("")

    def advance(self) -> None:
        table = self.query_one("#list", DataTable)
        start = table.cursor_row
        for offset in range(1, len(self.todo) + 1):
            i = (start + offset) % len(self.todo)
            if self.todo[i].key not in self.status:
                in_form = isinstance(self.focused, Input)
                table.move_cursor(row=i)
                self.load(self.todo[i])
                # stay in the "mode" we were in: the form, or the list
                (self.query_one("#desc", Input) if in_form else table).focus()
                return
        self.notify("All transactions reviewed. q to quit.")

    def mark(self, item: Item, state: str) -> None:
        self.status[item.key] = state
        table = self.query_one("#list", DataTable)
        mark = (
            Text("✓", style="bold green")
            if state == "done"
            else Text("·", style="yellow")
        )
        table.update_cell(item.key, "status", mark)

    def reload(self, years: Iterable[tuple[Source, int]]) -> None:
        """Re-read regenerated years; items that changed count as done."""
        table = self.query_one("#list", DataTable)
        for source, year in {(s.name, y): (s, y) for s, y in years}.values():
            self.history.refresh(source, year)
            fresh = year_transactions(source, year)
            for item in self.todo:
                if item.source is not source or item.year != year:
                    continue
                if item.index >= len(fresh):
                    self.notify("The journal changed shape; restart the review.")
                    continue
                txn = fresh[item.index]
                if txn.text() == item.txn.text():
                    continue
                item.txn = txn
                table.update_cell(item.key, "name", txn.description)
                table.update_cell(
                    item.key, "amount", amount_text(txn.amount(self.roles))
                )
                if item.key not in self.status:
                    self.mark(item, "done")
                    self.changed += 1
        self.update_title()
        if self.current:
            self.load(self.current)

    # events
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "list" and event.row_key.value is not None:
            self.load(self.item_for_key(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "list":
            self.query_one("#desc", Input).focus()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        pattern = self.query_one("#pattern", Input)
        field = self.rule_fields[event.index]
        pattern.disabled = not field
        self.query_one("#rule-error", Label).update("")
        if self.current is None or not field:
            pattern.value = ""
            return
        t = self.current.txn
        part = self.current.source.rule_fields.get(field, "description")
        text = t.description if part == "description" else t.comment
        pattern.value = hledger.rules_pattern(text)

    def on_selection_list_selected_changed(
        self, event: SelectionList.SelectedChanged[str]
    ) -> None:
        self.update_same_title()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            if self.search_active:
                self.search_jump(self.search_origin, 1)
        elif event.input.id == "account":
            self.confirm_new = None
            self.query_one("#new-account", Label).display = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.confirm_search()
        elif event.input.id == "desc":
            self.query_one("#account", Input).focus()
        elif event.input.id in ("account", "tags", "pattern"):
            self.action_save()

    # modes and movement
    def list_table(self) -> DataTable[object]:
        return self.query_one("#list", DataTable)

    def form_fields(self) -> list[Widget]:
        """Focusable widgets of the form, in focus order."""
        form = self.query_one("#detail")
        return [w for w in self.screen.focus_chain if form in w.ancestors]

    def in_form(self) -> bool:
        return self.focused in self.form_fields()

    def show_mode(self) -> None:
        """The status line says where keys go: the list's commands or the form."""
        mode = self.query_one("#mode", Static)
        in_form = self.in_form()
        mode.update("FORM" if in_form else "LIST")
        mode.set_class(in_form, "-form")
        self.refresh_bindings()

    def action_escape(self) -> None:
        """Esc: from the form back to the list, else cancel or clear the search."""
        if self.in_form():
            self.list_table().focus()
        elif self.focused is self.query_one("#search", Input):
            self.list_table().move_cursor(row=self.search_origin)
            self.clear_search()
        elif self.search_active:
            self.clear_search()

    def action_to_form(self) -> None:
        fields = self.form_fields()
        if fields and self.focused not in fields:
            fields[0].focus()

    # search
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "search_next":
            return self.search_active
        return True

    def search_matches(self, query: str) -> list[int]:
        """Rows whose name, notes or amount contain QUERY, ignoring case."""

        def norm(text: str) -> str:
            return text.casefold().replace(",", ".")

        q = norm(query)
        return [
            i
            for i, item in enumerate(self.todo)
            if any(
                q in norm(text)
                for text in (
                    item.txn.description,
                    item.txn.comment,
                    item.txn.amount(self.roles),
                )
            )
        ]

    def search_jump(self, start: int, step: int) -> None:
        """Move to the first match from row START on, in direction STEP."""
        search = self.query_one("#search", Input)
        query = search.value
        search.styles.width = len(query) + 2  # grows as you type, like vim's
        matches = self.search_matches(query) if query else []
        table = self.list_table()
        count = len(self.todo)
        target = next(
            (
                i
                for i in ((start + step * n) % count for n in range(count))
                if i in matches
            ),
            self.search_origin,
        )
        table.move_cursor(row=target)
        label = self.query_one("#matches", Static)
        label.set_class(bool(query) and not matches, "-none")
        if not query:
            label.update("")
        elif not matches:
            label.update("no match")
        else:
            label.update(f"{matches.index(target) + 1}/{len(matches)}")

    def action_search(self) -> None:
        """/: jump through the list as you type a name, notes or amount."""
        if not self.todo:
            return
        self.search_origin = self.list_table().cursor_row
        if self.focused is not self.query_one("#search", Input):
            self.search_return = self.focused
        self.search_active = True
        self.refresh_bindings()
        self.query_one("#search-bar").display = True
        self.query_one("#matches", Static).update("")
        search = self.query_one("#search", Input)
        search.value = ""
        search.focus()

    def confirm_search(self) -> None:
        """Enter: keep the match and the search, for n/N; clear a failed one."""
        if self.query_one("#matches", Static).has_class("-none"):
            self.notify("No match.", severity="warning")
            self.clear_search()
        elif not self.query_one("#search", Input).value:
            self.clear_search()
        else:
            self.list_table().focus()

    def clear_search(self) -> None:
        self.search_active = False
        self.refresh_bindings()
        search = self.query_one("#search", Input)
        if self.focused is search:
            back = self.search_return
            (back if back is not None and back.display else self.list_table()).focus()
        search.value = ""
        self.query_one("#search-bar").display = False

    def action_search_next(self, step: int) -> None:
        """n/N: the next/previous match of the search."""
        self.search_jump(self.list_table().cursor_row + step, step)

    def edit(self, path: Path, years: list[tuple[Source, int]]) -> None:
        """Open PATH in $VISUAL/$EDITOR, then regenerate YEARS and reload."""
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with self.suspend():
            run_editor([*shlex.split(editor), str(path)])
        try:
            self.regenerate(years)
        except (Failed, ConfigError, hledger.HledgerError) as e:
            self.notify(str(e), title="Not regenerated", severity="error")
            return
        self.reload(years)

    def current_source(self) -> Source:
        if self.current:
            return self.current.source
        return next(iter(self.config.sources.values()))

    def action_edit_rules(self) -> None:
        """e: edit the shared rules of this transaction's source."""
        source = self.current_source()
        self.edit(source.rules, [(source, y) for y in source.years()])

    def action_edit_one_offs(self) -> None:
        """E: edit the one-offs of this transaction's year."""
        if self.current is None:
            self.notify("No transaction selected.", severity="warning")
            return
        source, year = self.current.source, self.current.year
        self.edit(source.one_offs(year), [(source, year)])

    # saving
    def regenerate(self, years: Iterable[tuple[Source, int]]) -> None:
        by_source: dict[str, tuple[Source, list[int]]] = {}
        for source, year in years:
            by_source.setdefault(source.name, (source, []))[1].append(year)
        for source, source_years in by_source.values():
            generate(self.config, source, sorted(set(source_years)))

    def save_one_offs(
        self, group: list[Item], account: str, desc: str, tags: str = ""
    ) -> None:
        """Write a one-off line per item, then regenerate their years."""
        lines: list[tuple[Path, str, dict[str, str]]] = []
        shared: dict[str, Shared] = {}
        for item in group:
            source = item.source
            s = shared.setdefault(source.name, Shared(source))
            txns = year_transactions(source, item.year)
            row = locate(s, item.year, txns, item.index)
            if row is None:
                raise SaveError(f"no CSV row found for {item.txn.date} {desc}")
            key = s.key(row)
            comment = "" if desc != item.txn.description else item.txn.comment
            comment = hledger.with_tags(comment, tags)
            rules_safe(*key, comment)
            values = {
                source.rule_account: account,
                "description": desc,
                "comment": comment,
            }
            matcher = hledger.one_off_matcher(zip(source.row_key, key, strict=True))
            lines.append((source.one_offs(item.year), matcher, values))
        years = [(i.source, i.year) for i in group]
        paths = [p for p, _, _ in lines] + [src.output(y) for src, y in years]
        with rollback(paths):
            for path, matcher, values in lines:
                hledger.set_one_off(path, matcher, values, ["comment"] if tags else [])
            self.regenerate(years)

    def save_rule(
        self,
        item: Item,
        field: str,
        pattern: str,
        account: str,
        desc: str,
        tags: str = "",
    ) -> list[tuple[Source, int]]:
        """Append a rule to the shared rules, then regenerate every year."""
        source = item.source
        s = Shared(source)
        row = locate(s, item.year, year_transactions(source, item.year), item.index)
        value = s.evaluator.value(row, field) if row else ""
        if row is None or not rules.compile_pattern(pattern).search(value):
            raise SaveError(f"the pattern does not match %{field} {value!r}")
        years = [(source, y) for y in source.years()]
        with rollback([source.rules] + [source.output(y) for _, y in years]):
            hledger.append_rule(
                source.rules, field, pattern, account, desc, source.rule_account, tags
            )
            self.regenerate(years)
        return years

    # actions
    def action_save(self) -> None:
        t = self.current
        if t is None:
            return
        desc = self.query_one("#desc", Input).value.strip() or t.txn.description
        account = self.query_one("#account", Input).value.strip() or t.txn.account(
            self.roles
        )
        if account not in self.accounts and account != self.confirm_new:
            self.confirm_new = account
            note = self.query_one("#new-account", Label)
            note.update(f"'{account}' is new; save again (Enter or w) to create it")
            note.display = True
            return

        radio = self.query_one("#rule-field", RadioSet)
        field = self.rule_fields[max(radio.pressed_index, 0)]
        pattern = self.query_one("#pattern", Input).value.strip()
        if field:
            error = "" if pattern else "pattern is empty"
            if pattern:
                try:
                    rules.compile_pattern(pattern)
                except re.error as e:
                    error = f"invalid regex: {e}"
            if error:
                self.query_one("#rule-error", Label).update(error)
                return

        group = [t]
        try:
            rules_safe(desc, account, pattern)
            try:
                tags = hledger.normalise_tags(self.query_one("#tags", Input).value)
            except ValueError as e:
                raise SaveError(str(e)) from e
            if field:
                item_years = self.save_rule(t, field, pattern, account, desc, tags)
            else:
                group += [self.item_for_key(k) for k in self.same_list().selected]
                self.save_one_offs(group, account, desc, tags)
                item_years = [(o.source, o.year) for o in group]
        except SaveError as e:
            self.query_one("#rule-error", Label).update(str(e))
            self.notify(str(e), title="Not saved", severity="error")
            return
        except (Failed, ValueError, ConfigError, hledger.HledgerError) as e:
            self.notify(str(e), title="Not saved", severity="error")
            return
        if account not in self.accounts:
            self.accounts.append(account)
            if self.config.accounts_file:
                declare_account(self.config.accounts_file, account)

        self.reload(item_years)
        for o in group:
            if self.status.get(o.key) != "done":
                self.mark(o, "done")
                self.changed += 1
        msg = f"Saved {len(group)} one-off(s) → {account}"
        if field:
            msg = f"Rule added: %{field} {pattern} → {account}"
        self.notify(msg, title=desc)
        self.update_title()
        self.advance()

    def action_skip(self) -> None:
        if self.current is None:
            return
        if self.current.key not in self.status:
            self.mark(self.current, "skipped")
        self.advance()

    async def action_quit(self) -> None:
        self.exit(self.changed)
