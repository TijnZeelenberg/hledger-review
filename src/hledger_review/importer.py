"""`hledger-review import`: append a bank export to the journal, safely.

1. Copy the export to the source's stable CSV path. hledger keys both its
   "already imported" state (`.latest.<name>`) and the rules lookup on that
   name, so overlapping exports are fine: seen transactions are skipped.
2. Show a dry run and ask for confirmation.
3. Import, then `hledger check`. On failure (typically a balance assertion
   from the bank's running balance) the journal and state are rolled back.
"""

import shutil
import sys
import tempfile
from pathlib import Path

from hledger_review import hledger
from hledger_review.config import Config, ConfigError, Source


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _unmarked_count(config: Config) -> int:
    proc = hledger.run(config.journal, "print", f"acct:^{config.unmarked}$")
    return sum(1 for line in proc.stdout.split("\n") if line[:1].isdigit())


def run_import(config: Config, export: Path, source: Source, assume_yes: bool) -> int:
    """Import EXPORT for SOURCE; returns a process exit code."""
    if source.csv is None:
        raise ConfigError(f"sources.{source.name}.csv is not set")
    if not export.is_file():
        raise ConfigError(f"{export} does not exist")
    if not config.journal.is_file():
        raise ConfigError(f"journal {config.journal} does not exist")
    hledger.require()
    csv, state = source.csv, source.state
    assert state is not None

    # 1. stable file name
    if export.resolve() != csv.resolve():
        csv.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(export, csv)
    print(f"journal:  {config.journal}")
    print(f"export:   {export}")
    print(f"source:   {source.name} ({source.account})\n")

    # 2. dry run
    dry = hledger.run(config.journal, "import", str(csv), "--dry-run")
    out = (dry.stdout + dry.stderr).strip()
    print(out)
    if dry.returncode != 0:
        return 1
    if out.startswith("no new transactions"):
        return 0
    print()
    if not assume_yes and not _confirm(f"Append these to {config.journal.name}?"):
        print("aborted, nothing written")
        return 1

    # 3. import with rollback
    with tempfile.TemporaryDirectory() as backup:
        journal_bak = Path(backup) / "journal"
        state_bak = Path(backup) / "state"
        shutil.copyfile(config.journal, journal_bak)
        had_state = state.exists()
        if had_state:
            shutil.copyfile(state, state_bak)

        imported = hledger.run(config.journal, "import", str(csv))
        ok, msg = (
            hledger.check(config.journal)
            if imported.returncode == 0
            else (
                False,
                (imported.stdout + imported.stderr).strip(),
            )
        )
        if not ok:
            shutil.copyfile(journal_bak, config.journal)
            if had_state:
                shutil.copyfile(state_bak, state)
            else:
                state.unlink(missing_ok=True)
            print(msg, file=sys.stderr)
            print(
                f"\nerror: the import failed its check, so it was rolled back.\n\n"
                f"A failing balance assertion means the journal's {source.account}\n"
                "balance did not match the bank's running balance at that point. If\n"
                "it is the FIRST imported transaction, the history before it is off\n"
                "(a missing, duplicated or wrong-amount transaction); find it with\n"
                f"    hledger -f {config.journal} bal {source.account} -DH -b <date>\n"
                "If it is a later one, the export is inconsistent: re-export it.",
                file=sys.stderr,
            )
            return 1
    print(imported.stdout.strip())

    # 4. what next
    latest = state.read_text().split("\n", 1)[0] if state.exists() else "?"
    print(
        f"\nImported through {latest}. {_unmarked_count(config)} transaction(s) "
        f"still on {config.unmarked}:\n    hledger-review"
    )
    return 0
