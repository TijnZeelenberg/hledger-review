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
hledger-review rules [CSV...]                   # how often each rule matches
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

Keys are vim-like and modal. Everything starts in normal mode, where letters
are commands, including on a text field. Press `i` on a field to type in it
(`-- INSERT --` shows in the title); Esc or leaving the field ends insert mode.

| Key       | Action |
|-----------|--------|
| j / k     | next / previous row in the list, or field in the form |
| g / G     | first / last row |
| h / l     | to the list / to the form (Enter on a row also goes to the form) |
| i         | insert mode on the current field |
| Esc       | back to normal mode |
| w         | write (save); Enter in the account field in insert mode too |
| n         | next: skip this transaction |
| e         | open the rules file in `$VISUAL`/`$EDITOR` |
| q         | quit |

Use `--since YYYY-MM-DD` to limit the review. Use `--all` to also revisit
transactions that are already categorised.

**rules** matches every rule in the rules file against CSV data, the way
hledger does, and prints one line per rule in the format compilers use:

```
imports/bank.csv.rules:42: note: 18 rows, last 2026-09-12
imports/bank.csv.rules:57: warning: 3 rows, all overridden by line 88
imports/bank.csv.rules:61: warning: no rows
```

"Overridden" means later matching rules reassign every field this rule sets.
It reads the source's CSV by default, which holds only your latest export, so
pass older exports too (`hledger-review rules exports/*.csv`) before you
delete a rule for having no rows. Overlapping rows are counted once.
`-r FILE` picks the source that uses that rules file, or, without a config,
reads `FILE` minus `.rules`. In Neovim, `:cexpr system("hledger-review rules")`
puts the lines in the quickfix list.

## Neovim

The repository is also a Neovim plugin that shows these stats at the end of
every rule while you edit a `*.csv.rules` file, including unsaved changes.
With lazy.nvim:

```lua
{ "TijnZeelenberg/hledger-review", event = "BufReadPre *.csv.rules", opts = {} }
```

`:HledgerReviewRules` refreshes and reports errors; `:HledgerReviewRules
toggle` hides or shows the stats. Options, with their defaults:

```lua
opts = {
  cmd = { "hledger-review", "rules" },
  pattern = { "*.csv.rules" },
  debounce = 300, -- ms
}
```

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
