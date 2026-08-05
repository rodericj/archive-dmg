"""Tests for archive_dmg.manifest."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from archive_dmg.manifest import (
    build_manifest,
    manifest_to_dict,
    manifest_to_json,
    write_manifest_file,
)
from archive_dmg.models import (
    DmgContents,
    DmgVerificationResult,
    RemoteVerificationResult,
    UploadDestination,
)


def _verification(tmp_path):
    contents = DmgContents(
        file_count=3,
        top_level_entries=("DCIM", "PRIVATE"),
        earliest_file_modified_utc=datetime(2025, 2, 22, 18, 14, 3, tzinfo=UTC),
        latest_file_modified_utc=datetime(2026, 6, 22, 12, 5, 44, tzinfo=UTC),
    )
    return DmgVerificationResult(
        path=tmp_path / "Card.dmg",
        checksum_verified=True,
        mounted_read_only=True,
        contents=contents,
    )


def _direct_match_remote() -> RemoteVerificationResult:
    return RemoteVerificationResult(
        remote_size_verified=True,
        remote_checksum_verified=True,
        remote_checksum_status="verified_direct_match",
    )


def test_build_manifest_and_to_dict(tmp_path):
    destination = UploadDestination(bucket="b", key="cards/Card.dmg", region="us-west-2")

    manifest = build_manifest(
        dmg_path=tmp_path / "Card.dmg",
        archive_size_bytes=1234,
        archive_sha256="abc123",
        archive_created_utc=datetime(2026, 8, 5, 18, 3, 22, tzinfo=UTC),
        verification=_verification(tmp_path),
        destination=destination,
        remote_verification=_direct_match_remote(),
    )

    data = manifest_to_dict(manifest)

    assert data["schema_version"] == 1
    assert data["archive_filename"] == "Card.dmg"
    assert data["archive_size_bytes"] == 1234
    assert data["archive_created_utc"] == "2026-08-05T18:03:22Z"
    assert data["contents"] == {
        "file_count": 3,
        "earliest_file_modified_utc": "2025-02-22T18:14:03Z",
        "latest_file_modified_utc": "2026-06-22T12:05:44Z",
        "top_level_entries": ["DCIM", "PRIVATE"],
    }
    assert data["destination"] == {"bucket": "b", "key": "cards/Card.dmg", "region": "us-west-2"}
    assert data["verification"] == {
        "dmg_checksum_verified": True,
        "mounted_read_only": True,
        "remote_size_verified": True,
        "remote_checksum_verified": True,
        "remote_checksum_status": "verified_direct_match",
    }


def test_manifest_handles_missing_timestamps_and_empty_entries(tmp_path):
    contents = DmgContents(
        file_count=0,
        top_level_entries=(),
        earliest_file_modified_utc=None,
        latest_file_modified_utc=None,
    )
    verification = DmgVerificationResult(
        path=tmp_path / "Card.dmg",
        checksum_verified=True,
        mounted_read_only=True,
        contents=contents,
    )
    remote = RemoteVerificationResult(
        remote_size_verified=True,
        remote_checksum_verified=False,
        remote_checksum_status="stored_by_s3_not_directly_comparable",
    )

    manifest = build_manifest(
        dmg_path=tmp_path / "Card.dmg",
        archive_size_bytes=0,
        archive_sha256="x",
        archive_created_utc=datetime(2026, 1, 1, tzinfo=UTC),
        verification=verification,
        destination=UploadDestination(bucket="b", key="k", region="us-west-2"),
        remote_verification=remote,
    )
    data = manifest_to_dict(manifest)

    assert data["contents"]["earliest_file_modified_utc"] is None
    assert data["contents"]["latest_file_modified_utc"] is None
    assert data["contents"]["top_level_entries"] == []
    assert data["verification"]["remote_checksum_verified"] is False
    assert data["verification"]["remote_checksum_status"] == "stored_by_s3_not_directly_comparable"


def test_manifest_to_json_round_trips_through_json_loads(tmp_path):
    manifest = build_manifest(
        dmg_path=tmp_path / "Card.dmg",
        archive_size_bytes=10,
        archive_sha256="abc",
        archive_created_utc=datetime(2026, 1, 1, tzinfo=UTC),
        verification=_verification(tmp_path),
        destination=UploadDestination(bucket="b", key="k", region="us-west-2"),
        remote_verification=_direct_match_remote(),
    )

    parsed = json.loads(manifest_to_json(manifest))

    assert parsed["archive_sha256"] == "abc"


def test_write_manifest_file(tmp_path):
    dmg_path = tmp_path / "Card.dmg"
    manifest = build_manifest(
        dmg_path=dmg_path,
        archive_size_bytes=10,
        archive_sha256="abc",
        archive_created_utc=datetime(2026, 1, 1, tzinfo=UTC),
        verification=_verification(tmp_path),
        destination=UploadDestination(bucket="b", key="k", region="us-west-2"),
        remote_verification=_direct_match_remote(),
    )

    manifest_path = write_manifest_file(dmg_path, manifest)

    assert manifest_path == tmp_path / "Card.dmg.manifest.json"
    assert manifest_path.read_text() == manifest_to_json(manifest)
