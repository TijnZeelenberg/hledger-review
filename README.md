# hledger-review

Import bank CSV exports into an [hledger](https://hledger.org) journal, then
categorise whatever your CSV rules did not recognise, in a terminal UI. Every
categorisation can teach the rules file a new rule, so the next import needs
less review.

It does one thing: the import → review loop. For browsing and reports, use
`hledger-ui` or `hledger` itself.

## Install

```sh
uv tool install hledger-review     # or: pipx install hledger-review
```

Needs hledger >= 1.42 on `PATH`. Once installed, `hledger review` works too
(hledger runs `hledger-*` executables as add-on commands).

## Use

```sh
hledger-review import ~/Downloads/export.csv   # dry run, confirm, append, check
hledger-review                                  # review what is still unmarked
```

**import** copies the export to the source's stable CSV path, so hledger's
`.latest.*` state skips transactions it has already seen (overlapping exports are
fine). It shows a dry run, asks for confirmation, imports, then runs
`hledger check`. If the check fails, for example on a balance assertion from the
bank's running balance, the journal and the state file are rolled back.

**review** lists every transaction that has a posting to the unmarked account
(`expenses:unknown` by default). For each one you set:

- a description,
- an account, with completion from your existing accounts; a new account is
  confirmed first and can be declared in an accounts file,
- optionally, the same result for every other transaction with the same name,
- optionally, a rule appended to the rules file, matching one CSV field
  (for example `%payee`), with the pattern prefilled.

The journal is rewritten in place after every save: only the description and
the account change, so amounts, alignment, balance assertions and everything
else stay byte for byte the same. Quitting halfway loses nothing.

| Key    | Action |
|--------|--------|
| ctrl+s | save (Enter in the account field too) |
| ctrl+n | skip |
| ctrl+q | quit |

Use `--since YYYY-MM-DD` to limit the review. Use `--all` to also revisit
transactions that are already categorised.

## Configure

`hledger-review.toml` is looked up in the current directory and its parents,
then next to the journal. You can also point to it with `--config` or
`$HLEDGER_REVIEW_CONFIG`. Paths are relative to the config file, and
`{year}` expands to the current year.

```toml
journal = "{year}.journal"          # overridden by -f and $LEDGER_FILE
unmarked = "expenses:unknown"       # the account your rules file defaults to
accounts_file = "accounts.journal"  # optional: new accounts are declared here

[sources.bank]                      # one table per bank export
account = "assets:checking"         # the account the CSV describes
csv = "imports/bank.csv"            # stable path; rules default to <csv>.rules
rule_fields = { payee = "description", notes = "comment" }
rule_account = "account2"           # the accountN your rules categorise with
```

`rule_fields` names the CSV fields the review can write rules for. For each
one it says where that field ended up in the imported transaction (its
description or its comment), so the rule pattern can be prefilled. A source
without `rule_fields` never gets rules written for it.

Without a config file, review still works on `-f`/`$LEDGER_FILE` with
`expenses:unknown`, but rules and import need a source.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
