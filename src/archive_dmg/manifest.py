"""Build and serialize the minimal archival manifest.

The manifest intentionally records only what is needed to locate and verify
the archive later: no EXIF data, camera guesses, or per-file inventories. See
``models.ArchiveManifest`` for the exact field set.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from archive_dmg.models import (
    ArchiveManifest,
    DmgVerificationResult,
    RemoteVerificationResult,
    UploadDestination,
)


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(
    *,
    dmg_path: Path,
    archive_size_bytes: int,
    archive_sha256: str,
    archive_created_utc: datetime,
    verification: DmgVerificationResult,
    destination: UploadDestination,
    remote_verification: RemoteVerificationResult,
) -> ArchiveManifest:
    """Assemble the manifest from the DMG verification and upload results."""
    contents = verification.contents
    return ArchiveManifest(
        archive_filename=dmg_path.name,
        archive_size_bytes=archive_size_bytes,
        archive_sha256=archive_sha256,
        archive_created_utc=archive_created_utc,
        file_count=contents.file_count,
        earliest_file_modified_utc=contents.earliest_file_modified_utc,
        latest_file_modified_utc=contents.latest_file_modified_utc,
        top_level_entries=contents.top_level_entries,
        destination=destination,
        dmg_checksum_verified=verification.checksum_verified,
        mounted_read_only=verification.mounted_read_only,
        remote_size_verified=remote_verification.remote_size_verified,
        remote_checksum_verified=remote_verification.remote_checksum_verified,
        remote_checksum_status=remote_verification.remote_checksum_status,
    )


def manifest_to_dict(manifest: ArchiveManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "archive_filename": manifest.archive_filename,
        "archive_size_bytes": manifest.archive_size_bytes,
        "archive_sha256": manifest.archive_sha256,
        "archive_created_utc": _iso(manifest.archive_created_utc),
        "contents": {
            "file_count": manifest.file_count,
            "earliest_file_modified_utc": _iso(manifest.earliest_file_modified_utc),
            "latest_file_modified_utc": _iso(manifest.latest_file_modified_utc),
            "top_level_entries": list(manifest.top_level_entries),
        },
        "destination": {
            "bucket": manifest.destination.bucket,
            "key": manifest.destination.key,
            "region": manifest.destination.region,
        },
        "verification": {
            "dmg_checksum_verified": manifest.dmg_checksum_verified,
            "mounted_read_only": manifest.mounted_read_only,
            "remote_size_verified": manifest.remote_size_verified,
            "remote_checksum_verified": manifest.remote_checksum_verified,
            "remote_checksum_status": manifest.remote_checksum_status,
        },
    }


def manifest_to_json(manifest: ArchiveManifest) -> str:
    return json.dumps(manifest_to_dict(manifest), indent=2) + "\n"


def write_manifest_file(dmg_path: Path, manifest: ArchiveManifest) -> Path:
    """Write ``<dmg_path>.manifest.json`` next to the DMG and return its path."""
    manifest_path = dmg_path.with_name(dmg_path.name + ".manifest.json")
    manifest_path.write_text(manifest_to_json(manifest))
    return manifest_path
