import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

CONFIG = """\
journal = "main.journal"
accounts_file = "accounts.journal"

[sources.bank]
account = "assets:checking"
rules = "bank.rules"
data = "{year}/{year}.csv"
one_offs = "{year}/one-offs.rules"
output = "{year}/{year}.journal"
row_key = ["date", "saldo", "bedrag", "direction"]
commodity_style = "€1,000.00"
rule_fields = { payee = "description", notes = "comment" }
rule_account = "account1"
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own $LEDGER_FILE, config and theme out of the tests."""
    monkeypatch.delenv("LEDGER_FILE", raising=False)
    monkeypatch.delenv("HLEDGER_REVIEW_CONFIG", raising=False)
    monkeypatch.setenv("HLEDGER_REVIEW_THEME_DIR", str(tmp_path / "no-theme"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A copy of the fixtures plus an hledger-review.toml, as the cwd."""
    shutil.copytree(FIXTURES, tmp_path, dirs_exist_ok=True)
    (tmp_path / "hledger-review.toml").write_text(CONFIG)
    return tmp_path


needs_hledger = pytest.mark.skipif(
    shutil.which("hledger") is None, reason="hledger not on PATH"
)


@pytest.fixture
def history_workdir(workdir: Path) -> Path:
    """The workdir plus a categorised 2025, included in the main journal."""
    shutil.copytree(workdir / "history" / "2025", workdir / "2025")
    main = workdir / "main.journal"
    include = "include 2025/2025.journal\ninclude 2026/"
    main.write_text(main.read_text().replace("include 2026/", include))
    return workdir
