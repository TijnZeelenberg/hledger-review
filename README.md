# hledger-review

Keep bank CSV exports as plain data, generate [hledger](https://hledger.org)
journals from them, and categorise whatever your CSV rules did not recognise,
in a terminal UI. Nothing is appended and no journal is edited by hand: every
categorisation is a line in a rules file, and the journals are regenerated.

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
hledger-review import ~/Downloads/export.csv   # merge, regenerate, check
hledger-review                                  # review what is still unmarked
hledger-review generate [YEAR...]               # regenerate the journals
hledger-review rules [CSV...]                   # how often each rule matches
```

Per bank account (a *source*) the layout is:

```
ing.rules              shared CSV rules, for every year
2026/2026.csv          every row the bank exported for 2026, merged
2026/one-offs.rules    `include ../ing.rules`, then one line per odd transaction
2026/2026.journal      generated, included by your main journal
```

A year journal is always the output of

```sh
hledger -f 2026/2026.csv --rules 2026/one-offs.rules print
```

behind a two-line `; GENERATED` header, so it can be thrown away and rebuilt.
Commit the CSVs and rules; the journals too if you like reviewing their diffs.

**import** reads the export with the shared rules' `separator`, `skip`,
`fields`, `date-format` and `encoding`, splits it by year, and adds to each
year's CSV only the rows whose `row_key` is not there yet. New rows go where
the export has them relative to rows the file already has, so the file keeps
the bank's order (newest first; an oldest-first export is reversed). If the
export shares no rows with the file, rows are merged by date, newest first.
Header, line ends and quoting of existing rows are kept byte for byte, so an
export with nothing new changes nothing. A new year gets its folder and a
one-offs file. It shows how many rows are new per year and how many of them
the rules leave on the unmarked account, asks for confirmation (`-y` skips
it), writes, regenerates the touched years and runs `hledger check` on the
main journal. If the check fails, for example on a balance assertion from
the bank's running balance, every touched file is rolled back. The export
itself is not copied or moved. A new year's journal still has to be
included in your main journal; import warns when it is not.

**generate** rebuilds every year journal (or the given years) of every
source (or `-s SOURCE`), then checks, rolling back on failure. Run it after
editing rules by hand.

**review** lists the transactions of the generated journals that post to the
unmarked account (`expenses:unknown` by default); `--all` lists all of them.
For each one you set:

- a description,
- an account, with completion from your existing accounts; a new account is
  confirmed first and can be declared in an accounts file,
- optionally, the same result for every other listed transaction with the
  same name,
- or, instead, a rule for the shared rules file, matching one CSV field
  (for example `%payee`), with the pattern prefilled. It must match this
  transaction; it applies to every year.

Saving writes a line to the year's one-offs table, matching exactly that CSV
row on its `row_key` fields:

```
if|account1|description|comment
%date ^20260830$ && %saldo ^1234,56$ && %amountraw ^555,81$ && %debitcredit ^Debit$|expenses:misc|Birthday present|
```

A changed description clears the bank's comment; an unchanged one keeps it.
Saving the same transaction again replaces its line. Then the year (every
year, for a rule) is regenerated and the list reloads; transactions that a
new rule categorised are ticked off too. `|` and line breaks cannot be saved,
since they would break the table. Quitting halfway loses nothing.

To find a transaction's CSV row, review matches its date, its amount on the
source account and its balance assertion against the year's CSV, evaluating
the shared rules (not the one-offs, which should only set accounts,
descriptions and comments). Rows that tie on all three pair up in the order
hledger reads them. Without balance assertions (no `balanceN` in the rules)
only date and amount are compared, which is only as reliable as that order.

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
| e         | edit the shared rules in `$VISUAL`/`$EDITOR`, then regenerate |
| E         | edit this year's one-offs, then regenerate |
| q         | quit |

Use `--since YYYY-MM-DD` to limit the review. Use `--all` to also revisit
transactions that are already categorised.

**rules** matches every rule in the rules file against CSV data, the way
hledger does, and prints one line per rule in the format compilers use:

```
bank.rules:42: note: 18/240 · 17d ago
bank.rules:57: warning: 3/240 · all overridden by 88 · 2mo ago
bank.rules:61: warning: unused
```

`18/240` is the rows the rule matches out of all rows, followed by how long
ago the latest one was. "Overridden" means later matching rules reassign
every field this rule sets.
By default it reads the source's shared rules and all of its year CSVs.
`include` is not followed, so the one-offs tables are not counted: a rule
that only one-offs override still shows as matching, and the "set no
account1" total includes rows that a one-off categorises. Overlapping rows
are counted once. `-r FILE` picks the source that uses that rules file, or,
without a config, reads `FILE` minus `.rules`. In Neovim, `:cexpr system("hledger-review rules")`
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

A shared rules file with another name needs its own `pattern` (and `event`),
for example `pattern = { "ing.rules" }`. The stats need the source's `fields`,
so they work on the shared rules, not on a one-offs file.

## Configure

`hledger-review.toml` is looked up in the current directory and its parents,
then next to the journal. You can also point to it with `--config` or
`$HLEDGER_REVIEW_CONFIG`. Paths are relative to the config file.

```toml
journal = "main.journal"            # overridden by -f and $LEDGER_FILE
unmarked = "expenses:unknown"       # the account your rules file defaults to
accounts_file = "accounts.journal"  # optional: new accounts are declared here

[sources.bank]                      # one table per bank account
account = "assets:checking"         # the account the CSV describes
rules = "bank.rules"                # shared rules
data = "{year}/{year}.csv"          # {year} is the year of the data
one_offs = "{year}/one-offs.rules"
output = "{year}/{year}.journal"
row_key = ["date", "balance", "amount", "direction"]   # names from `fields`
commodity_style = "€1,000.00"       # optional: `print -c` for the output
label = "Bank checking"             # optional: for the output's header
rule_fields = { payee = "description", notes = "comment" }
rule_account = "account1"           # the accountN your rules categorise with
```

`journal` is the entry point that `hledger check` runs on; it must include
the generated `output` files. In `journal`, `{year}` is the current year.

`row_key` names the CSV fields that together identify a row. Pick fields
the bank never changes and that tell apart rows on the same day: with a
running balance, `date` + balance + amount + direction usually does. Two
rows with the same key are still both imported when the export has both,
but a one-off line matches both of them.

`commodity_style` makes amounts look like the rest of your journal. The
generated file starts with a `decimal-mark` directive to match it (or the
rules' `decimal-mark` without a style), so it parses the same wherever it is
included.

`rule_fields` names the CSV fields the review can write rules for. For each
one it says where that field ended up in the transaction (its description or
its comment), so the rule pattern can be prefilled. A source without
`rule_fields` never gets rules written for it.

A one-offs file for a new year starts with only `include`; the review adds
the table (`if|<rule_account>|description|comment`) with its first line. You
can reorder its columns or leave out `comment`, but other columns cannot be
filled in by the review.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
