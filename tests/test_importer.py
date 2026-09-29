from pathlib import Path

import pytest

from hledger_review import hledger
from hledger_review.config import load
from hledger_review.importer import run_import
from tests.conftest import needs_hledger

pytestmark = needs_hledger


def download(tmp_path: Path, text: str | None = None) -> Path:
    """A bank export outside the stable CSV path, as a user would pass it."""
    path = tmp_path / "Downloads" / "export-2026-01.csv"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text or (tmp_path / "bank.csv").read_text())
    return path


def test_import_appends_and_is_idempotent(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load()
    export = download(workdir)
    (workdir / "bank.csv").unlink()

    assert run_import(config, export, config.source(None), assume_yes=True) == 0
    journal = (workdir / "sample.journal").read_text()
    assert "2026-01-12 Salary" in journal
    assert "2026-01-13 Mystery Shop  ; card 42" in journal
    assert hledger.check(config.journal)[0]
    assert (workdir / ".latest.bank.csv").read_text().startswith("2026-01-13")
    assert "6 transaction(s) still on expenses:unknown" in capsys.readouterr().out

    assert run_import(config, export, config.source(None), assume_yes=True) == 0
    assert (workdir / "sample.journal").read_text() == journal
    assert "no new transactions" in capsys.readouterr().out


def test_failed_check_rolls_back(workdir: Path) -> None:
    config = load()
    bad = (workdir / "bank.csv").read_text().replace("2919.02", "2919.03")
    before = (workdir / "sample.journal").read_text()

    assert run_import(config, download(workdir, bad), config.source(None), True) == 1
    assert (workdir / "sample.journal").read_text() == before
    assert not (workdir / ".latest.bank.csv").exists()


def test_declined_prompt_writes_nothing(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load()
    before = (workdir / "sample.journal").read_text()
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run_import(config, download(workdir), config.source(None), False) == 1
    assert (workdir / "sample.journal").read_text() == before
