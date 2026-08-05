"""DMG validation, checksum verification, mounting, and content inspection.

All subprocess calls use argument lists (never shell interpolation) so paths
with spaces, apostrophes, or Unicode are handled correctly. Mount-point
detection parses ``hdiutil attach -plist`` output structurally instead of
scraping human-readable text.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from archive_dmg.errors import DmgMountError, DmgVerificationError, ValidationError
from archive_dmg.models import DmgContents, DmgVerificationResult

# Normal macOS metadata that should not be treated as archive content.
IGNORED_NAMES = frozenset(
    {
        ".Spotlight-V100",
        ".fseventsd",
        ".DS_Store",
        ".Trashes",
        ".TemporaryItems",
        ".apdisk",
        ".VolumeIcon.icns",
    }
)

_DISK_ID_RE = re.compile(r"^(/dev/disk\d+)")
_DETACH_ATTEMPTS = 3


def validate_dmg_path(path: Path) -> Path:
    """Confirm ``path`` exists, is a regular file, and has a .dmg extension."""
    if not path.exists():
        raise ValidationError(
            f"File not found: {path}",
            hint="Check the path and try again.",
        )
    if not path.is_file():
        raise ValidationError(
            f"Not a regular file: {path}",
            hint="Provide a path to a .dmg file, not a directory or special file.",
        )
    if path.suffix.lower() != ".dmg":
        raise ValidationError(
            f"Expected a file with a .dmg extension, got: {path.name}",
            hint="archive-dmg only accepts .dmg disk images.",
        )
    return path


def verify_checksum(path: Path) -> None:
    """Run ``hdiutil verify`` and raise ``DmgVerificationError`` on failure."""
    result = subprocess.run(
        ["hdiutil", "verify", str(path)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise DmgVerificationError(
            "DMG verification failed.",
            hint=stderr
            or (
                "macOS reported that the image checksum is invalid. Do not upload this "
                "archive until the source image has been recreated or recovered."
            ),
        )


def _whole_disk_identifier(entities: list[dict[str, object]]) -> str:
    """Extract the base ``/dev/diskN`` identifier so detach affects only this image."""
    for entity in entities:
        dev_entry = entity.get("dev-entry")
        if isinstance(dev_entry, str):
            match = _DISK_ID_RE.match(dev_entry)
            if match:
                return match.group(1)
    raise DmgMountError(
        "Could not determine the disk identifier for the mounted image.",
        hint="Try opening it manually in Disk Utility to confirm that macOS can read it.",
    )


def _detach(disk_id: str, *, attempts: int = _DETACH_ATTEMPTS) -> None:
    """Detach ``disk_id``, retrying with ``-force`` if the volume is briefly busy."""
    last_stderr = ""
    for attempt in range(attempts):
        args = ["hdiutil", "detach", disk_id, "-quiet"]
        if attempt > 0:
            args.append("-force")
        result = subprocess.run(args, capture_output=True, check=False)
        if result.returncode == 0:
            return
        last_stderr = result.stderr.decode("utf-8", "replace").strip()
        if attempt + 1 < attempts:
            time.sleep(0.5 * (attempt + 1))
    raise DmgMountError(
        f"Unable to detach {disk_id} after {attempts} attempts.",
        hint=last_stderr or f"Run 'hdiutil detach {disk_id} -force' manually.",
    )


@contextmanager
def mount_readonly(path: Path) -> Iterator[list[Path]]:
    """Attach ``path`` read-only and without Finder, yielding its mount point(s).

    Detaches the image in a ``finally`` block so cleanup happens on normal
    completion, on an exception raised inside the ``with`` block, and on
    Ctrl-C. Only the disk identifier created by this call is ever detached.
    """
    result = subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", str(path)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise DmgMountError(
            "Unable to mount the DMG read-only.",
            hint=stderr
            or "Try opening it manually in Disk Utility to confirm that macOS can read it.",
        )

    try:
        plist = plistlib.loads(result.stdout)
    except plistlib.InvalidFileException as exc:
        raise DmgMountError(
            "hdiutil returned output that could not be parsed.",
            hint=str(exc),
        ) from exc

    entities = plist.get("system-entities", [])
    disk_id = _whole_disk_identifier(entities)
    mount_points = [Path(e["mount-point"]) for e in entities if e.get("mount-point")]

    if not mount_points:
        _detach(disk_id)
        raise DmgMountError(
            "The DMG mounted but exposed no readable volume.",
            hint="Try opening it manually in Disk Utility to confirm that macOS can read it.",
        )

    try:
        yield mount_points
    finally:
        try:
            _detach(disk_id)
        except DmgMountError:
            if sys.exc_info()[0] is None:
                raise
            # An exception from the `with` body is already propagating; let it
            # take priority over a secondary detach failure during cleanup.


def inspect_mounted_volume(mount_points: Sequence[Path]) -> DmgContents:
    """Walk the mounted volume(s), ignoring normal macOS metadata."""
    file_count = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    top_level_entries: set[str] = set()

    for mount_point in mount_points:
        for entry in mount_point.iterdir():
            if entry.name not in IGNORED_NAMES:
                top_level_entries.add(entry.name)

        for root, dirs, files in os.walk(mount_point):
            dirs[:] = [d for d in dirs if d not in IGNORED_NAMES]
            for filename in files:
                if filename in IGNORED_NAMES:
                    continue
                file_count += 1
                try:
                    mtime = (Path(root) / filename).stat().st_mtime
                except OSError:
                    continue
                modified = datetime.fromtimestamp(mtime, tz=UTC)
                if earliest is None or modified < earliest:
                    earliest = modified
                if latest is None or modified > latest:
                    latest = modified

    return DmgContents(
        file_count=file_count,
        top_level_entries=tuple(sorted(top_level_entries)),
        earliest_file_modified_utc=earliest,
        latest_file_modified_utc=latest,
    )


def verify_dmg(path: Path) -> DmgVerificationResult:
    """Run the full verification pipeline shared by ``verify`` and ``upload``."""
    verify_checksum(path)
    with mount_readonly(path) as mount_points:
        contents = inspect_mounted_volume(mount_points)
    return DmgVerificationResult(
        path=path,
        checksum_verified=True,
        mounted_read_only=True,
        contents=contents,
    )
