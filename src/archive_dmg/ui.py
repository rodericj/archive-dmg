"""Rich rendering helpers shared by the CLI commands.

Kept separate from the service layer (``dmg.py``, ``aws.py``, ``doctor.py``)
so those modules stay Rich-free and return plain data that is easy to test.
Every function here takes an explicit ``Console`` instead of importing a
module-level singleton, so tests can capture output with ``Console(file=...)``.
"""

from __future__ import annotations

from datetime import datetime

from rich import box
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from archive_dmg.errors import ArchiveDmgError
from archive_dmg.models import CheckResult, RemoteArchive

_STATUS_GLYPHS = {"ok": ("✓", "green"), "warn": ("⚠", "yellow"), "fail": ("✗", "red")}

#: How each ``RemoteArchive.restore_state`` reads in the ``list`` table, and in
#: what style. Only archived objects can be in a state other than the first.
_RESTORE_LABELS = {
    "not_applicable": ("ready", "green"),
    "not_restored": ("needs restore", "yellow"),
    "in_progress": ("restoring...", "cyan"),
    "restored": ("restored", "green"),
}


def _indent(text: str, spaces: int = 4) -> str:
    pad = " " * spaces
    return "\n".join(f"{pad}{line}" if line else "" for line in text.splitlines())


def print_section(console: Console, title: str) -> None:
    console.print()
    console.print(title, style="bold")


def print_check(console: Console, check: CheckResult) -> None:
    """Render one doctor/verify/upload check line, e.g. '✓ hdiutil'."""
    glyph, style = _STATUS_GLYPHS[check.status]
    console.print(f"[{style}]{glyph}[/{style}] {check.name}")
    if check.detail:
        console.print(_indent(check.detail), style="dim" if check.status == "ok" else style)
    if check.hint:
        console.print(_indent(check.hint), style="dim")


def print_key_value(console: Console, label: str, value: str) -> None:
    """Render a labeled block, e.g. 'Bucket\\n    my-bucket'."""
    console.print()
    console.print(label, style="bold")
    console.print(_indent(value))


def print_error(console: Console, error: ArchiveDmgError) -> None:
    """Render an expected error without a stack trace: what failed, then the hint."""
    console.print()
    console.print(error.message, style="bold red")
    if error.hint:
        console.print()
        console.print(error.hint)


def print_success(console: Console, message: str) -> None:
    console.print()
    console.print(message, style="bold green")


def create_byte_progress(console: Console) -> Progress:
    """A determinate progress bar for a byte-counted operation (hashing or upload)."""
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        DownloadColumn(),
        TimeRemainingColumn(),
        console=console,
    )


_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(num_bytes: int) -> str:
    """Human-readable decimal (SI) size, matching how AWS reports object sizes."""
    size = float(num_bytes)
    for unit in _BYTE_UNITS[:-1]:
        if size < 1000:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} {_BYTE_UNITS[-1]}"


def format_count(n: int) -> str:
    return f"{n:,}"


def format_timestamp(moment: datetime | None) -> str:
    if moment is None:
        return "unknown"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_date_range(earliest: datetime | None, latest: datetime | None) -> str:
    if earliest is None or latest is None:
        return "unknown"
    return f"{earliest:%Y-%m-%d} → {latest:%Y-%m-%d}"


def format_entries(entries: tuple[str, ...]) -> str:
    return ", ".join(entries) if entries else "(none)"


def format_restore_state(archive: RemoteArchive) -> tuple[str, str]:
    """Return the (label, style) describing whether an archive can be read now."""
    label, style = _RESTORE_LABELS.get(archive.restore_state, (archive.restore_state, "white"))
    if archive.restore_state == "restored" and archive.restore_expiry_utc is not None:
        label = f"{label} until {archive.restore_expiry_utc:%Y-%m-%d}"
    return label, style


def print_archive_table(console: Console, archives: tuple[RemoteArchive, ...]) -> None:
    """Render the ``list`` output, newest first, with storage class and readiness."""
    table = Table(box=box.SIMPLE, header_style="bold", expand=False)
    table.add_column("Uploaded", no_wrap=True)
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("Storage class", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Key", overflow="fold")

    for archive in archives:
        label, style = format_restore_state(archive)
        table.add_row(
            f"{archive.last_modified_utc:%Y-%m-%d}",
            format_bytes(archive.size_bytes),
            archive.storage_class,
            f"[{style}]{label}[/{style}]",
            archive.key,
        )
    console.print(table)
