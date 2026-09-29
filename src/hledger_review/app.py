"""The review TUI: give imported transactions a description and an account."""

import re
from typing import ClassVar

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
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

from hledger_review import hledger
from hledger_review.config import Config, Source
from hledger_review.journal import Txn, declare_account, render, write_atomic


def amount_text(amount: str) -> Text:
    return Text(amount, style="bold red" if "-" in amount else "bold green")


class ReviewApp(App[int]):
    """Review transactions one by one; returns how many were changed."""

    TITLE = "hledger-review"
    CSS_PATH = "app.tcss"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+n", "skip", "Skip", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        config: Config,
        segments: list[str | Txn],
        todo: list[Txn],
        accounts: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.roles = config.roles
        self.segments = segments
        self.todo = todo
        self.accounts = (
            accounts if accounts is not None else hledger.accounts(config.journal)
        )
        # "" = no rule, then every rule field any source knows, in config order
        self.rule_fields = [
            "",
            *dict.fromkeys(f for s in config.sources.values() for f in s.rule_fields),
        ]
        self.status: dict[int, str] = {}
        self.current: Txn | None = None
        self.changed = 0
        self.confirm_new: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="list", cursor_type="row", zebra_stripes=True)
            with Vertical(id="detail"):
                yield Static(id="info")
                yield Label("Description", classes="field")
                yield Input(id="desc")
                yield Label("Account", classes="field")
                yield Input(id="account")
                yield Label("", id="new-account")
                yield Checkbox("", id="same")
                yield Label(
                    "Rule for the next import", classes="field", id="rule-label"
                )
                with RadioSet(id="rule-field"):
                    yield RadioButton("No rule", value=True)
                    for f in self.rule_fields[1:]:
                        yield RadioButton(f"Match %{f}")
                yield Input(id="pattern", placeholder="regex", disabled=True)
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
        for t in self.todo:
            table.add_row(
                "",
                t.date,
                amount_text(t.amount(self.roles)),
                t.description,
                key=str(id(t)),
            )
        self.update_title()
        table.focus()
        if self.todo:
            self.load(self.todo[0])

    # state helpers
    def source_of(self, t: Txn) -> Source | None:
        p = t.asset_posting(self.roles)
        return self.config.source_for(p.account if p else None)

    def update_title(self) -> None:
        done = sum(1 for s in self.status.values() if s == "done")
        table = self.query_one("#list", DataTable)
        table.border_title = f"To review ({done}/{len(self.todo)})"

    def txn_for_key(self, key: str) -> Txn:
        return next(t for t in self.todo if str(id(t)) == key)

    def same_name(self, t: Txn) -> list[Txn]:
        return [
            o
            for o in self.todo
            if o is not t and o.raw_name == t.raw_name and id(o) not in self.status
        ]

    def load(self, t: Txn) -> None:
        self.current = t
        info = Table.grid(padding=(0, 2))
        info.add_column(style="dim")
        info.add_column()
        info.add_row("date", Text(t.date, style="bold"))
        info.add_row("amount", amount_text(t.amount(self.roles)))
        info.add_row("name", t.raw_name)
        info.add_row("notes", t.notes or Text("-", style="dim"))
        info.add_row("account", Text(t.account(self.roles), style="cyan"))
        state = self.status.get(id(t))
        if state:
            style = "green" if state == "done" else "yellow"
            info.add_row("status", Text(state, style=style))
        self.query_one("#detail", Vertical).border_title = t.raw_name
        self.query_one("#info", Static).update(info)
        self.query_one("#desc", Input).value = t.description
        account = self.query_one("#account", Input)
        account.value = ""
        account.placeholder = t.account(self.roles)
        same = self.same_name(t)
        checkbox = self.query_one("#same", Checkbox)
        checkbox.label = (
            f"Apply to {len(same)} other transaction(s) named '{t.raw_name}'"
        )
        checkbox.value = bool(same)
        checkbox.display = bool(same)
        self.load_rule_fields(t)

    def load_rule_fields(self, t: Txn) -> None:
        """Offer only the rule fields of this transaction's source, if it has rules."""
        source = self.source_of(t)
        fields = source.rule_fields if source and source.rules else {}
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
            if id(self.todo[i]) not in self.status:
                table.move_cursor(row=i)
                self.load(self.todo[i])
                self.query_one("#desc", Input).focus()
                return
        self.notify("All transactions reviewed. ctrl+q to quit.")

    def mark(self, t: Txn, state: str) -> None:
        self.status[id(t)] = state
        table = self.query_one("#list", DataTable)
        mark = (
            Text("✓", style="bold green")
            if state == "done"
            else Text("·", style="yellow")
        )
        table.update_cell(str(id(t)), "status", mark)
        table.update_cell(str(id(t)), "name", t.description)

    # events
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value is not None:
            self.load(self.txn_for_key(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.query_one("#desc", Input).focus()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        pattern = self.query_one("#pattern", Input)
        field = self.rule_fields[event.index]
        pattern.disabled = not field
        self.query_one("#rule-error", Label).update("")
        source = self.source_of(self.current) if self.current else None
        if self.current is None or source is None or not field:
            pattern.value = ""
            return
        part = source.rule_fields.get(field, "description")
        text = self.current.raw_name if part == "description" else self.current.notes
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

    # actions
    def action_save(self) -> None:
        t = self.current
        if t is None:
            return
        desc = self.query_one("#desc", Input).value.strip() or t.raw_name
        account = self.query_one("#account", Input).value.strip() or t.account(
            self.roles
        )
        if account not in self.accounts and account != self.confirm_new:
            self.confirm_new = account
            self.query_one("#new-account", Label).update(
                f"'{account}' is a new account; ctrl+s again to create it"
            )
            return

        radio = self.query_one("#rule-field", RadioSet)
        field = self.rule_fields[max(radio.pressed_index, 0)]
        pattern = self.query_one("#pattern", Input).value.strip()
        source = self.source_of(t)
        if field:
            error = "" if pattern else "pattern is empty"
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as e:
                    error = f"invalid regex: {e}"
            if error:
                self.query_one("#rule-error", Label).update(error)
                return

        group = [t]
        if self.query_one("#same", Checkbox).value:
            group += self.same_name(t)
        for o in group:
            o.set_description(desc, keep_comment=(desc == o.raw_name))
            o.set_account(account, self.roles)
            self.mark(o, "done")
            self.changed += 1
        write_atomic(self.config.journal, render(self.segments))
        if account not in self.accounts:
            self.accounts.append(account)
            if self.config.accounts_file:
                declare_account(self.config.accounts_file, account)

        msg = f"Saved {len(group)} transaction(s) → {account}"
        if field and source and source.rules:
            hledger.append_rule(
                source.rules, field, pattern, account, desc, source.rule_account
            )
            msg += f"\nRule added: %{field} {pattern}"
        self.notify(msg, title=desc)
        self.update_title()
        self.advance()

    def action_skip(self) -> None:
        if self.current is None:
            return
        if id(self.current) not in self.status:
            self.mark(self.current, "skipped")
        self.advance()

    async def action_quit(self) -> None:
        self.exit(self.changed)
