"""Thin wrappers around the hledger executable and CSV rules files."""

import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

from hledger_review.journal import write_atomic

MIN_VERSION = (1, 42)


class HledgerError(Exception):
    """hledger is missing, too old, or a command failed."""


def executable() -> str | None:
    return shutil.which("hledger")


def require() -> str:
    """The hledger executable, checked against MIN_VERSION."""
    exe = executable()
    if not exe:
        raise HledgerError("hledger not found on PATH")
    out = subprocess.run(
        [exe, "--version"], capture_output=True, text=True, check=False
    ).stdout
    m = re.match(r"hledger (\d+)\.(\d+)", out)
    if not m or (int(m.group(1)), int(m.group(2))) < MIN_VERSION:
        need = ".".join(map(str, MIN_VERSION))
        raise HledgerError(f"hledger >= {need} needed, found: {out.strip() or '?'}")
    return exe


def run(journal: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run `hledger -f JOURNAL ARGS...`, capturing output."""
    exe = executable()
    if not exe:
        raise HledgerError("hledger not found on PATH")
    return subprocess.run(
        [exe, "-f", str(journal), *args], capture_output=True, text=True, check=False
    )


def accounts(journal: Path) -> list[str]:
    """Every account the journal uses or declares; empty without hledger."""
    if not executable():
        return []
    proc = run(journal, "accounts")
    return [a for a in proc.stdout.split("\n") if a] if proc.returncode == 0 else []


def check(journal: Path) -> tuple[bool, str]:
    proc = run(journal, "check")
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


# rules files
def rules_pattern(text: str) -> str:
    """Escape regex metacharacters (hledger uses POSIX extended regexes)."""
    return re.sub(r"([.*+?()\[\]{}|^$\\])", r"\\\1", text)


def append_rule(
    rules: Path,
    field: str,
    pattern: str,
    account: str,
    description: str,
    account_field: str = "account2",
) -> None:
    """Append an `if %FIELD PATTERN` block; later rules override earlier ones."""
    with rules.open("a") as f:
        f.write(f"\nif %{field} {pattern}\n")
        f.write(f"  {account_field:<12} {account}\n")
        f.write(f"  description  {description}\n")
        f.write("  comment\n")


def one_off_matcher(key: Iterable[tuple[str, str]]) -> str:
    """An exact match on every key field, e.g. `%date ^20260101$ && %saldo ^1,00$`."""
    return " && ".join(f"%{name} ^{rules_pattern(value)}$" for name, value in key)


def set_one_off(rules: Path, matcher: str, values: dict[str, str]) -> None:
    """Add or replace MATCHER's line in the file's `if|...` table.

    Without a table, one is appended with the columns of VALUES; an existing
    table's columns must all be in VALUES.
    """
    text = rules.read_text() if rules.exists() else ""
    lines = text.split("\n")
    header = next((i for i, x in enumerate(lines) if x.startswith("if|")), None)
    if header is None:
        table = f"if|{'|'.join(values)}\n{matcher}|{'|'.join(values.values())}\n"
        write_atomic(
            rules, (text.rstrip("\n") + "\n\n" if text.strip() else "") + table
        )
        return
    columns = [c.strip().lower() for c in lines[header][3:].split("|")]
    unknown = [c for c in columns if c not in values]
    if unknown:
        raise ValueError(f"{rules}: cannot fill column {', '.join(unknown)}")
    line = f"{matcher}|{'|'.join(values[c] for c in columns)}"
    end = header + 1
    while end < len(lines) and lines[end].strip():
        if lines[end].split("|")[0] == matcher:
            lines[end] = line
            break
        end += 1
    else:
        lines.insert(end, line)
    write_atomic(rules, "\n".join(lines))
