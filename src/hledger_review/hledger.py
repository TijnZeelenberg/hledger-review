"""Thin wrappers around the hledger executable and CSV rules files."""

import re
import shutil
import subprocess
from pathlib import Path

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
