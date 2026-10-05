"""Shared widgets: a vim-like list and the form's text fields.

The list moves with vim keys; the form is a plain form: a focused field
takes text right away, Tab moves between fields and Esc goes back to the list.
"""

from typing import ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.widgets import DataTable, Input, RadioSet, SelectionList


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


class FormInput(Input):
    """An Input that knows whether its text came from the keyboard."""

    # False after the review fills the field, so that opens no completion
    typed = False

    async def _on_key(self, event: events.Key) -> None:
        self.typed = True

    def _on_paste(self, event: events.Paste) -> None:
        self.typed = True

    def _on_blur(self, event: events.Blur) -> None:
        self.typed = False


def is_text(character: str | None) -> bool:
    return character is not None and character.isprintable()


class FormSelectionList(SelectionList[str]):
    """A checklist whose letters and digits are never the app's commands."""

    def check_consume_key(self, key: str, character: str | None) -> bool:
        return is_text(character)

    def _on_focus(self, event: events.Focus) -> None:
        if self.highlighted is None and self.option_count:
            self.highlighted = 0  # so Space ticks a row straight away


class FormRadioSet(RadioSet):
    """A radio set whose letters and digits are never the app's commands."""

    def check_consume_key(self, key: str, character: str | None) -> bool:
        return is_text(character)
