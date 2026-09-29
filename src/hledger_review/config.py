"""Settings from `hledger-review.toml`, merged with the command line.

    journal = "{year}.journal"              # default: -f, then $LEDGER_FILE
    unmarked = "expenses:unknown"           # hledger's own default for CSV imports
    accounts_file = "accounts.journal"      # optional: declare new accounts here

    [sources.ing]                           # one table per bank export
    account = "assets:checking"             # the account the CSV describes
    csv = "imports/ing.csv"                 # stable path, hledger keys state on it
    rules = "imports/ing.csv.rules"         # default: <csv>.rules
    rule_fields = { payee = "description", notes = "comment" }
    rule_account = "account2"               # which accountN your rules categorise with

`rule_fields` maps a CSV field name used in the rules file (`if %payee ...`)
to the part of an imported transaction its value ended up in (`description`
or `comment`), so a learned rule can be prefilled. Relative paths are
relative to the config file.
"""

import datetime as dt
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from hledger_review.journal import Roles

CONFIG_NAME = "hledger-review.toml"
DEFAULT_UNMARKED = "expenses:unknown"
TxnPart = Literal["description", "comment"]


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Source:
    """One bank export: where it is copied to, and the account it describes."""

    name: str
    account: str
    csv: Path | None = None
    rules: Path | None = None
    rule_fields: dict[str, TxnPart] = field(default_factory=dict)
    rule_account: str = "account2"

    @property
    def state(self) -> Path | None:
        """hledger's "already imported" state file for this CSV."""
        return self.csv.with_name(f".latest.{self.csv.name}") if self.csv else None


@dataclass(frozen=True)
class Config:
    journal: Path
    unmarked: str = DEFAULT_UNMARKED
    accounts_file: Path | None = None
    sources: dict[str, Source] = field(default_factory=dict)
    path: Path | None = None

    @property
    def roles(self) -> Roles:
        return Roles(self.unmarked, frozenset(s.account for s in self.sources.values()))

    def source_for(self, account: str | None) -> Source | None:
        return next((s for s in self.sources.values() if s.account == account), None)

    def source(self, name: str | None) -> Source:
        """The named source, or the only one when no name is given."""
        return pick_source(self.sources, name)


def pick_source(sources: dict[str, Source], name: str | None) -> Source:
    """The named source, or the only one when no name is given."""
    if not sources:
        raise ConfigError(f"no [sources.*] configured (looked for {CONFIG_NAME})")
    if name is None:
        if len(sources) > 1:
            names = ", ".join(sources)
            raise ConfigError(f"several sources configured, pick one: {names}")
        return next(iter(sources.values()))
    if name not in sources:
        raise ConfigError(f"unknown source {name!r}")
    return sources[name]


# discovery
def find_config(explicit: str | None, journal: Path | None) -> Path | None:
    """--config, $HLEDGER_REVIEW_CONFIG, cwd and its parents, the journal's dir."""
    if explicit or (explicit := os.environ.get("HLEDGER_REVIEW_CONFIG")):
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"config file {path} does not exist")
        return path
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        if (d / CONFIG_NAME).is_file():
            return d / CONFIG_NAME
    if journal and (journal.parent / CONFIG_NAME).is_file():
        return journal.parent / CONFIG_NAME
    return None


# parsing
def _path(base: Path, value: object, key: str) -> Path:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return base / Path(value.format(year=dt.date.today().year)).expanduser()


def _str(value: object, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _account_field(value: object, key: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"account\d+", value):
        raise ConfigError(f'{key} must look like "account2"')
    return value


def _source(name: str, raw: object, base: Path) -> Source:
    if not isinstance(raw, dict):
        raise ConfigError(f"[sources.{name}] must be a table")
    t = cast(dict[str, Any], raw)
    key = f"sources.{name}"
    csv = _path(base, t["csv"], f"{key}.csv") if "csv" in t else None
    rules = _path(base, t["rules"], f"{key}.rules") if "rules" in t else None
    if rules is None and csv is not None:
        rules = csv.with_name(csv.name + ".rules")
    fields = t.get("rule_fields", {})
    if not isinstance(fields, dict) or not all(
        v in ("description", "comment") for v in fields.values()
    ):
        raise ConfigError(
            f'{key}.rule_fields values must be "description" or "comment"'
        )
    return Source(
        name,
        _str(t.get("account"), f"{key}.account"),
        csv,
        rules,
        cast(dict[str, TxnPart], fields),
        _account_field(t.get("rule_account", "account2"), f"{key}.rule_account"),
    )


def _read(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


def _sources(raw: dict[str, Any], base: Path) -> dict[str, Source]:
    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        raise ConfigError("[sources] must be a table of tables")
    return {n: _source(n, s, base) for n, s in sources.items()}


def load_sources(config_arg: str | None = None) -> dict[str, Source]:
    """Only the sources, for commands that do not need a journal."""
    ledger_file = os.environ.get("LEDGER_FILE")
    path = find_config(config_arg, Path(ledger_file) if ledger_file else None)
    return _sources(_read(path), path.parent if path else Path.cwd())


def load(config_arg: str | None = None, journal_arg: str | None = None) -> Config:
    """Build the config: CLI -f wins over $LEDGER_FILE, which wins over the file."""
    given = journal_arg or os.environ.get("LEDGER_FILE")
    cli_journal = Path(given).expanduser() if given else None
    path = find_config(config_arg, cli_journal)

    raw = _read(path)
    base = path.parent if path else Path.cwd()

    journal = cli_journal or (
        _path(base, raw["journal"], "journal") if "journal" in raw else None
    )
    if journal is None:
        raise ConfigError(
            f"no journal: pass -f, set $LEDGER_FILE or add `journal` to {CONFIG_NAME}"
        )
    return Config(
        journal=journal,
        unmarked=_str(raw.get("unmarked", DEFAULT_UNMARKED), "unmarked"),
        accounts_file=(
            _path(base, raw["accounts_file"], "accounts_file")
            if "accounts_file" in raw
            else None
        ),
        sources=_sources(raw, base),
        path=path,
    )
