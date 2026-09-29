"""Settings from `hledger-review.toml`, merged with the command line.

    journal = "main.journal"                # default: -f, then $LEDGER_FILE
    unmarked = "expenses:unknown"           # hledger's own default for CSV imports
    accounts_file = "accounts.journal"      # optional: declare new accounts here

    [sources.ing]                           # one table per bank account
    account = "assets:checking"             # the account the CSV describes
    rules = "ing.rules"                     # shared rules, for every year
    data = "{year}/{year}.csv"              # all of a year's rows, merged
    one_offs = "{year}/one-offs.rules"      # includes the shared rules
    output = "{year}/{year}.journal"        # generated, include it in the journal
    row_key = ["date", "saldo", "amountraw", "debitcredit"]
    commodity_style = "€1,000.00"           # optional: `print -c` for the output
    label = "ING checking"                  # optional: named in the output header
    rule_fields = { payee = "description", notes = "comment" }
    rule_account = "account2"               # which accountN your rules categorise with

In source paths `{year}` is the year of the data; in `journal` it is the
current year. `row_key` names the CSV fields (from the rules' `fields`) that
identify a row: imports skip rows whose key is already there, and one-off
rules match on it. `rule_fields` maps a CSV field name used in the rules file
(`if %payee ...`) to the part of a transaction its value ended up in
(`description` or `comment`), so a learned rule can be prefilled. Relative
paths are relative to the config file.
"""

import datetime as dt
import glob
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from hledger_review import rules
from hledger_review.journal import Roles

CONFIG_NAME = "hledger-review.toml"
DEFAULT_UNMARKED = "expenses:unknown"
TxnPart = Literal["description", "comment"]


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Source:
    """One bank account: its rules, its yearly data and generated journals."""

    name: str
    account: str
    rules: Path
    data_pattern: str  # absolute, with {year}
    one_offs_pattern: str
    output_pattern: str
    row_key: tuple[str, ...]
    commodity_style: str | None = None
    label: str = ""
    rule_fields: dict[str, TxnPart] = field(default_factory=dict)
    rule_account: str = "account2"

    def data(self, year: int) -> Path:
        return Path(self.data_pattern.format(year=year))

    def one_offs(self, year: int) -> Path:
        return Path(self.one_offs_pattern.format(year=year))

    def output(self, year: int) -> Path:
        return Path(self.output_pattern.format(year=year))

    def years(self) -> list[int]:
        """Every year that has a data file, oldest first."""
        first, *rest = (re.escape(p) for p in self.data_pattern.split("{year}"))
        regex = re.compile(first + r"(\d{4})" + r"\1".join(rest))
        matches = glob.glob(glob.escape(self.data_pattern).format(year="*"))
        return sorted(int(m.group(1)) for f in matches if (m := regex.fullmatch(f)))


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


def _pattern(base: Path, value: object, key: str) -> str:
    """A path with a `{year}` placeholder, made absolute but not yet expanded."""
    if not isinstance(value, str) or "{year}" not in value:
        raise ConfigError(f"{key} must be a path containing {{year}}")
    if re.search(r"\{(?!year\})|(?<!\{year)\}", value):
        raise ConfigError(f"{key} may only use the {{year}} placeholder")
    return str(base / Path(value).expanduser())


def _row_key(value: object, key: str, rules_file: Path) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v for v in value)
    ):
        raise ConfigError(f"{key} must be a list of CSV field names")
    if rules_file.is_file():
        fields = rules.parse(rules_file.read_text()).directives.fields
        missing = [v for v in value if v not in fields]
        if missing:
            raise ConfigError(
                f"{key}: {', '.join(missing)} not in the `fields` of {rules_file}"
            )
    return tuple(value)


def _source(name: str, raw: object, base: Path) -> Source:
    if not isinstance(raw, dict):
        raise ConfigError(f"[sources.{name}] must be a table")
    t = cast(dict[str, Any], raw)
    key = f"sources.{name}"
    rules_file = _path(base, t.get("rules"), f"{key}.rules")
    style = t.get("commodity_style")
    if style is not None:
        style = _str(style, f"{key}.commodity_style")
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
        rules_file,
        _pattern(base, t.get("data"), f"{key}.data"),
        _pattern(base, t.get("one_offs"), f"{key}.one_offs"),
        _pattern(base, t.get("output"), f"{key}.output"),
        _row_key(t.get("row_key"), f"{key}.row_key", rules_file),
        style,
        _str(t.get("label", name), f"{key}.label"),
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
