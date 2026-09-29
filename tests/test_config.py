import datetime as dt
from pathlib import Path

import pytest

from hledger_review.config import DEFAULT_UNMARKED, ConfigError, load

SOURCE = """\
rules = "r.rules"
data = "{year}/{year}.csv"
one_offs = "{year}/one-offs.rules"
output = "{year}/{year}.journal"
row_key = ["date"]
"""


def test_load_resolves_paths_relative_to_config(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = workdir / "deeper"
    sub.mkdir()
    monkeypatch.chdir(sub)
    config = load()
    assert config.path == workdir / "hledger-review.toml"
    assert config.journal == workdir / "main.journal"
    source = config.source(None)
    assert source.rules == workdir / "bank.rules"
    assert source.data(2026) == workdir / "2026" / "2026.csv"
    assert source.one_offs(2027) == workdir / "2027" / "one-offs.rules"
    assert source.output(2026) == workdir / "2026" / "2026.journal"
    assert source.row_key == ("date", "saldo", "bedrag", "direction")
    assert (source.label, source.rule_account) == ("bank", "account1")
    assert config.unmarked == DEFAULT_UNMARKED
    assert config.roles.assets == frozenset({"assets:checking"})


def test_years_come_from_data_files(workdir: Path) -> None:
    (workdir / "2024").mkdir()
    (workdir / "2024" / "2024.csv").write_text("")
    (workdir / "2025").mkdir()
    (workdir / "2025" / "2026.csv").write_text("")  # not the 2025 pattern
    assert load().source(None).years() == [2024, 2026]


def test_cli_and_env_journal_win(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEDGER_FILE", "env.journal")
    assert load().journal == Path("env.journal")
    assert load(journal_arg="cli.journal").journal == Path("cli.journal")


def test_config_next_to_journal(
    workdir: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    config = load(journal_arg=str(workdir / "main.journal"))
    assert config.path == workdir / "hledger-review.toml"


def test_journal_year_is_the_current_year(tmp_path: Path) -> None:
    (tmp_path / "hledger-review.toml").write_text('journal = "{year}.journal"\n')
    assert load().journal == tmp_path / f"{dt.date.today().year}.journal"


def test_no_journal_is_an_error() -> None:
    with pytest.raises(ConfigError, match="no journal"):
        load()


def test_no_config_uses_defaults() -> None:
    config = load(journal_arg="x.journal")
    assert config.path is None
    assert config.sources == {}
    with pytest.raises(ConfigError, match="no \\[sources"):
        config.source(None)


def test_source_selection(tmp_path: Path) -> None:
    (tmp_path / "hledger-review.toml").write_text(
        f'journal = "j"\n[sources.a]\naccount = "x"\n{SOURCE}'
        f'[sources.b]\naccount = "y"\n{SOURCE}'
    )
    config = load()
    with pytest.raises(ConfigError, match="pick one: a, b"):
        config.source(None)
    assert config.source("b").account == "y"
    with pytest.raises(ConfigError, match="unknown source"):
        config.source("c")


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        (SOURCE, "account must be"),
        ('account = "x"\n' + SOURCE.replace("{year}/{year}.csv", "a.csv"), "data mu"),
        ('account = "x"\n' + SOURCE.replace("{year}.csv", "{y}.csv"), "only"),
        ('account = "x"\n' + SOURCE.replace('["date"]', "[]"), "row_key must"),
        ('account = "x"\n' + SOURCE.replace('["date"]', '["nope"]'), "nope not in"),
        ('account = "x"\nrule_fields = { p = "memo" }\n' + SOURCE, "rule_fields"),
        ('account = "x"\nrule_account = "acct"\n' + SOURCE, "rule_account must"),
    ],
)
def test_invalid_source(tmp_path: Path, toml: str, message: str) -> None:
    (tmp_path / "r.rules").write_text("fields date, amount\n")
    (tmp_path / "hledger-review.toml").write_text(f'journal = "j"\n[sources.a]\n{toml}')
    with pytest.raises(ConfigError, match=message):
        load()


def test_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "hledger-review.toml").write_text("journal = \n")
    with pytest.raises(ConfigError, match=r"hledger-review\.toml"):
        load()
