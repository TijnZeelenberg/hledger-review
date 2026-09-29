"""Shared widgets: vim-like lists and modal text fields.

Text fields start in normal mode: letters are commands, not text. `i` on a
field enters insert mode for that field; Esc (or leaving the field) ends it.
"""

from typing import ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import DataTable, Input


class ModeChanged(Message):
    """A field entered or left insert mode."""

    def __init__(self, editing: bool) -> None:
        super().__init__()
        self.editing = editing


class ListTable(DataTable[object]):
    """A row-cursor table that also moves with vim's j/k and g/G."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "scroll_top", "First", show=False),
        Binding("G", "scroll_bottom", "Last", show=False),
    ]

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id, cursor_type="row", zebra_stripes=True)


class ModalInput(Input):
    """An Input that only accepts text in insert mode."""

    editing = False

    def set_editing(self, editing: bool) -> None:
        if editing != self.editing:
            self.editing = editing
            self.set_class(editing, "-editing")
            self.post_message(ModeChanged(editing))

    def check_consume_key(self, key: str, character: str | None) -> bool:
        return self.editing and super().check_consume_key(key, character)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # the Input's own bindings (backspace, arrows, enter...) need insert mode
        return self.editing

    async def _on_key(self, event: events.Key) -> None:
        if not self.editing:
            event.prevent_default()  # skip Input's handler that inserts text

    def _on_paste(self, event: events.Paste) -> None:
        if not self.editing:
            event.prevent_default()

    def _on_blur(self, event: events.Blur) -> None:
        self.set_editing(False)
