import datetime as dt
from pathlib import Path

import pytest

from hledger_review.config import DEFAULT_UNMARKED, ConfigError, load


def test_load_resolves_paths_relative_to_config(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = workdir / "deeper"
    sub.mkdir()
    monkeypatch.chdir(sub)
    config = load()
    assert config.path == workdir / "hledger-review.toml"
    assert config.journal == workdir / "sample.journal"
    source = config.source(None)
    assert source.csv == workdir / "bank.csv"
    assert source.rules == workdir / "bank.csv.rules"
    assert source.state == workdir / ".latest.bank.csv"
    assert source.rule_account == "account2"
    assert config.unmarked == DEFAULT_UNMARKED
    assert config.roles.assets == frozenset({"assets:checking"})


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
    config = load(journal_arg=str(workdir / "sample.journal"))
    assert config.path == workdir / "hledger-review.toml"


def test_year_placeholder(tmp_path: Path) -> None:
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
        'journal = "j"\n[sources.a]\naccount = "x"\n[sources.b]\naccount = "y"\n'
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
        ('journal = "j"\n[sources.a]\ncsv = "x"\n', "account must be"),
        (
            'journal = "j"\n[sources.a]\naccount = "x"\nrule_fields = { p = "memo" }\n',
            "rule_fields values",
        ),
        (
            'journal = "j"\n[sources.a]\naccount = "x"\nrule_account = "acct"\n',
            "rule_account must",
        ),
        ("journal = \n", "hledger-review.toml"),
    ],
)
def test_invalid_config(tmp_path: Path, toml: str, message: str) -> None:
    (tmp_path / "hledger-review.toml").write_text(toml)
    with pytest.raises(ConfigError, match=message):
        load()
