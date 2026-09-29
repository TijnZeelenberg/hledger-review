import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

CONFIG = """\
journal = "sample.journal"
accounts_file = "accounts.journal"

[sources.bank]
account = "assets:checking"
csv = "bank.csv"
rule_fields = { payee = "description", notes = "comment" }
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own $LEDGER_FILE and config out of the tests."""
    monkeypatch.delenv("LEDGER_FILE", raising=False)
    monkeypatch.delenv("HLEDGER_REVIEW_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A copy of the fixtures plus an hledger-review.toml, as the cwd."""
    for f in FIXTURES.iterdir():
        shutil.copy(f, tmp_path / f.name)
    (tmp_path / "hledger-review.toml").write_text(CONFIG)
    return tmp_path


needs_hledger = pytest.mark.skipif(
    shutil.which("hledger") is None, reason="hledger not on PATH"
)
