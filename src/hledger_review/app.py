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
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
)
from textual_autocomplete import AutoComplete, DropdownItem

from hledger_review import hledger, rules
from hledger_review.config import Config, ConfigError, Source
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
from hledger_review.widgets import ListTable, ModalInput, ModeChanged


def run_editor(command: list[str]) -> None:
    subprocess.run(command, check=False)


def amount_text(amount: str) -> Text:
    return Text(amount, style="bold red" if "-" in amount else "bold green")


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
        # vim-like and modal: fields only take text in insert mode (i on the
        # field, Esc to leave), so letters are commands everywhere else
        Binding("i", "insert", "Insert"),
        Binding("escape", "normal", "Normal", show=False),
        Binding("h", "to_list", "List", show=False),
        Binding("l", "to_form", "Form", show=False),
        Binding("j", "field(1)", "Next field", show=False),
        Binding("k", "field(-1)", "Previous field", show=False),
        Binding("w", "save", "Write"),
        Binding("n", "skip", "Next"),
        Binding("e", "edit_rules", "Edit rules"),
        Binding("E", "edit_one_offs", "Edit one-offs", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config: Config,
        todo: list[Item],
        accounts: list[str] | None = None,
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

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListTable(id="list")
            with Vertical(id="detail"):
                yield Static(id="info")
                yield Label("Description", classes="field")
                yield ModalInput(id="desc")
                yield Label("Account", classes="field")
                yield ModalInput(id="account")
                yield Label("", id="new-account")
                yield Checkbox("", id="same")
                yield Label(
                    "Or instead a shared rule", classes="field", id="rule-label"
                )
                with RadioSet(id="rule-field"):
                    yield RadioButton("No rule", value=True)
                    for f in self.rule_fields[1:]:
                        yield RadioButton(f"Match %{f}")
                yield ModalInput(id="pattern", placeholder="regex", disabled=True)
                yield Label("", id="rule-error")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.config.journal.name
        self.mount(
            AutoComplete(
                "#account",
                candidates=lambda _: [DropdownItem(a) for a in self.accounts],
            )
        )
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

    def load(self, item: Item) -> None:
        self.current = item
        t = item.txn
        info = Table.grid(padding=(0, 2))
        info.add_column(style="dim")
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
        same = self.same_name(item)
        checkbox = self.query_one("#same", Checkbox)
        checkbox.label = (
            f"Apply to {len(same)} other transaction(s) named '{t.description}'"
        )
        checkbox.value = bool(same)
        checkbox.display = bool(same)
        self.load_rule_fields(item.source)

    def load_rule_fields(self, source: Source) -> None:
        """Offer only the rule fields of this transaction's source."""
        fields = source.rule_fields
        buttons = list(self.query_one("#rule-field", RadioSet).query(RadioButton))
        for f, button in zip(self.rule_fields, buttons, strict=True):
            button.display = not f or f in fields
        buttons[0].value = True
        for widget in ("#rule-label", "#rule-field", "#pattern", "#rule-error"):
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

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "account":
            self.confirm_new = None
            self.query_one("#new-account", Label).update("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "desc":
            self.query_one("#account", Input).focus()
        elif event.input.id in ("account", "pattern"):
            self.action_save()

    # modes and movement
    def list_table(self) -> DataTable[object]:
        return self.query_one("#list", DataTable)

    def form_fields(self) -> list[Widget]:
        """Focusable widgets of the form, in focus order."""
        form = self.query_one("#detail")
        return [w for w in self.screen.focus_chain if form in w.ancestors]

    def on_mode_changed(self, event: ModeChanged) -> None:
        name = self.config.journal.name
        self.sub_title = f"{name}  -- INSERT --" if event.editing else name

    def action_insert(self) -> None:
        """i: on a field, start typing in it; on the list, go to the form."""
        if isinstance(self.focused, ModalInput):
            self.focused.set_editing(True)
        else:
            self.action_to_form()

    def action_normal(self) -> None:
        """Esc: leave insert mode, staying on the field."""
        if isinstance(self.focused, ModalInput):
            self.focused.set_editing(False)

    def action_to_list(self) -> None:
        self.list_table().focus()

    def action_to_form(self) -> None:
        fields = self.form_fields()
        if fields and self.focused not in fields:
            fields[0].focus()

    def action_field(self, delta: int) -> None:
        fields = self.form_fields()
        if self.focused in fields:
            i = fields.index(self.focused) + delta
            fields[max(0, min(i, len(fields) - 1))].focus()

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

    def save_one_offs(self, group: list[Item], account: str, desc: str) -> None:
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
                hledger.set_one_off(path, matcher, values)
            self.regenerate(years)

    def save_rule(
        self, item: Item, field: str, pattern: str, account: str, desc: str
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
                source.rules, field, pattern, account, desc, source.rule_account
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
            self.query_one("#new-account", Label).update(
                f"'{account}' is a new account; save again (Enter or w) to create it"
            )
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
            if field:
                item_years = self.save_rule(t, field, pattern, account, desc)
            else:
                if self.query_one("#same", Checkbox).value:
                    group += self.same_name(t)
                self.save_one_offs(group, account, desc)
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
