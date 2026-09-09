"""Tests for archive_dmg.archive_service: key construction, overwrite protection, downloads."""

from __future__ import annotations

import hashlib

import pytest

from archive_dmg import archive_service
from archive_dmg.errors import (
    ArchiveNotRestoredError,
    AwsDownloadError,
    DestinationExistsError,
    ValidationError,
)
from archive_dmg.models import ArchiveConfig, RemoteArchive


def test_normalize_prefix_strips_slashes():
    assert archive_service.normalize_prefix("/foo/bar/") == "foo/bar"
    assert archive_service.normalize_prefix("") == ""
    assert archive_service.normalize_prefix("///") == ""


@pytest.mark.parametrize(
    ("prefix", "filename", "expected"),
    [
        ("", "Card.dmg", "Card.dmg"),
        ("2026/card-archives", "Card.dmg", "2026/card-archives/Card.dmg"),
        ("/2026/card-archives/", "Card.dmg", "2026/card-archives/Card.dmg"),
        ("cards", "Camera Card 01.dmg", "cards/Camera Card 01.dmg"),
    ],
)
def test_build_destination_key(prefix, filename, expected):
    assert archive_service.build_destination_key(prefix, filename) == expected


def test_upload_archive_blocks_existing_object_without_overwrite(monkeypatch, tmp_path):
    dmg_path = tmp_path / "Card.dmg"
    dmg_path.write_bytes(b"data")

    monkeypatch.setattr(archive_service, "validate_dmg_path", lambda p: p)
    monkeypatch.setattr(archive_service, "create_session", lambda **kwargs: object())
    monkeypatch.setattr(archive_service, "get_caller_identity", lambda session: None)
    monkeypatch.setattr(archive_service, "check_bucket_exists", lambda session, b, r: None)
    monkeypatch.setattr(archive_service, "object_exists", lambda *a, **kw: True)

    config = ArchiveConfig(bucket="b", region="us-west-2", default_prefix="")

    with pytest.raises(DestinationExistsError) as exc_info:
        archive_service.upload_archive(dmg_path=dmg_path, config=config, overwrite=False)

    assert "already exists" in exc_info.value.message
    assert "s3://b/Card.dmg" in exc_info.value.message


def test_upload_archive_allows_existing_object_with_overwrite(monkeypatch, tmp_path):
    dmg_path = tmp_path / "Card.dmg"
    dmg_path.write_bytes(b"data")

    calls: list[str] = []

    monkeypatch.setattr(archive_service, "validate_dmg_path", lambda p: p)
    monkeypatch.setattr(archive_service, "create_session", lambda **kwargs: object())
    monkeypatch.setattr(archive_service, "get_caller_identity", lambda session: None)
    monkeypatch.setattr(archive_service, "check_bucket_exists", lambda session, b, r: None)
    monkeypatch.setattr(archive_service, "object_exists", lambda *a, **kw: True)
    monkeypatch.setattr(
        archive_service,
        "verify_dmg",
        lambda p: (_ for _ in ()).throw(RuntimeError("reached verify_dmg")),
    )

    config = ArchiveConfig(bucket="b", region="us-west-2", default_prefix="")

    # With --overwrite, the existing-object check should be skipped entirely
    # and the pipeline should proceed past it (to the next real step, which
    # we stub to raise so this test stays fast and dependency-free).
    with pytest.raises(RuntimeError, match="reached verify_dmg"):
        archive_service.upload_archive(dmg_path=dmg_path, config=config, overwrite=True)

    assert calls == []


# --- download / restore --------------------------------------------------------

_CONFIG = ArchiveConfig(bucket="b", region="us-west-2", default_prefix="p")


def _stub_connection(monkeypatch):
    """Neutralize session creation, auth, and the bucket check."""
    monkeypatch.setattr(archive_service, "create_session", lambda **kwargs: object())
    monkeypatch.setattr(archive_service, "get_caller_identity", lambda session: None)
    monkeypatch.setattr(archive_service, "check_bucket_exists", lambda session, b, r: None)


def _archive(**overrides):
    from datetime import UTC, datetime

    defaults = {
        "bucket": "b",
        "key": "p/Card.dmg",
        "region": "us-west-2",
        "size_bytes": 4,
        "last_modified_utc": datetime(2026, 1, 1, tzinfo=UTC),
        "storage_class": "STANDARD",
        "restore_state": "not_applicable",
    }
    return RemoteArchive(**{**defaults, **overrides})


def _stub_head(monkeypatch, archive):
    monkeypatch.setattr(archive_service, "head_archive", lambda *a, **kw: archive)


def _stub_download(monkeypatch, payload: bytes):
    """Make download_file_with_progress actually write ``payload`` to the target."""

    def fake(session, *, bucket, key, region, path, on_bytes_transferred):
        path.write_bytes(payload)
        on_bytes_transferred(len(payload))

    monkeypatch.setattr(archive_service, "download_file_with_progress", fake)


def test_download_archive_refuses_deep_archive_that_was_never_restored(monkeypatch, tmp_path):
    _stub_connection(monkeypatch)
    _stub_head(
        monkeypatch, _archive(storage_class="DEEP_ARCHIVE", restore_state="not_restored")
    )

    with pytest.raises(ArchiveNotRestoredError) as exc_info:
        archive_service.download_archive(
            key="p/Card.dmg", config=_CONFIG, destination=tmp_path
        )

    assert "DEEP_ARCHIVE" in exc_info.value.message
    assert "--restore" in exc_info.value.hint


def test_download_archive_reports_restore_already_running(monkeypatch, tmp_path):
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive(storage_class="DEEP_ARCHIVE", restore_state="in_progress"))

    with pytest.raises(ArchiveNotRestoredError) as exc_info:
        archive_service.download_archive(
            key="p/Card.dmg", config=_CONFIG, destination=tmp_path
        )

    assert "already in progress" in exc_info.value.message
    assert "12 hours" in exc_info.value.hint


def test_download_archive_downloads_restored_deep_archive_object(monkeypatch, tmp_path):
    payload = b"data"
    digest = hashlib.sha256(payload).hexdigest()
    _stub_connection(monkeypatch)
    _stub_head(
        monkeypatch,
        _archive(storage_class="DEEP_ARCHIVE", restore_state="restored", size_bytes=len(payload)),
    )
    _stub_download(monkeypatch, payload)
    monkeypatch.setattr(
        archive_service, "download_bytes", lambda *a, **kw: f"{digest}  Card.dmg\n".encode()
    )

    result = archive_service.download_archive(
        key="p/Card.dmg", config=_CONFIG, destination=tmp_path
    )

    assert result.path == tmp_path / "Card.dmg"
    assert result.path.read_bytes() == payload
    assert result.verification.verified
    assert result.verification.local_sha256 == digest
    assert result.sha256_path is not None
    assert result.sha256_path.read_text().startswith(digest)


def test_download_archive_raises_when_checksum_does_not_match(monkeypatch, tmp_path):
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive(size_bytes=4))
    _stub_download(monkeypatch, b"data")
    monkeypatch.setattr(
        archive_service, "download_bytes", lambda *a, **kw: f"{'a' * 64}  Card.dmg\n".encode()
    )

    with pytest.raises(AwsDownloadError) as exc_info:
        archive_service.download_archive(
            key="p/Card.dmg", config=_CONFIG, destination=tmp_path
        )

    assert "does not match" in exc_info.value.message


def test_download_archive_reports_missing_sidecar_without_failing(monkeypatch, tmp_path):
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive(size_bytes=4))
    _stub_download(monkeypatch, b"data")
    monkeypatch.setattr(archive_service, "download_bytes", lambda *a, **kw: None)

    result = archive_service.download_archive(
        key="p/Card.dmg", config=_CONFIG, destination=tmp_path
    )

    assert result.verification.status == "sidecar_missing"
    assert result.verification.verified is False
    assert result.sha256_path is None


def test_download_archive_detects_truncated_transfer(monkeypatch, tmp_path):
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive(size_bytes=999))
    _stub_download(monkeypatch, b"short")

    with pytest.raises(AwsDownloadError) as exc_info:
        archive_service.download_archive(
            key="p/Card.dmg", config=_CONFIG, destination=tmp_path
        )

    assert "size" in exc_info.value.message.lower()


def test_download_archive_refuses_to_clobber_existing_file(monkeypatch, tmp_path):
    existing = tmp_path / "Card.dmg"
    existing.write_bytes(b"keep me")
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive())

    with pytest.raises(DestinationExistsError):
        archive_service.download_archive(
            key="p/Card.dmg", config=_CONFIG, destination=tmp_path
        )

    assert existing.read_bytes() == b"keep me"


def test_download_archive_overwrites_when_asked(monkeypatch, tmp_path):
    existing = tmp_path / "Card.dmg"
    existing.write_bytes(b"old")
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive(size_bytes=4))
    _stub_download(monkeypatch, b"data")
    monkeypatch.setattr(archive_service, "download_bytes", lambda *a, **kw: None)

    result = archive_service.download_archive(
        key="p/Card.dmg", config=_CONFIG, destination=tmp_path, overwrite=True
    )

    assert result.path.read_bytes() == b"data"


def test_download_archive_rejects_missing_destination_directory(monkeypatch, tmp_path):
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive())

    with pytest.raises(ValidationError) as exc_info:
        archive_service.download_archive(
            key="p/Card.dmg", config=_CONFIG, destination=tmp_path / "nope" / "Card.dmg"
        )

    assert "does not exist" in exc_info.value.message


def test_download_archive_skips_verification_when_requested(monkeypatch, tmp_path):
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive(size_bytes=4))
    _stub_download(monkeypatch, b"data")

    def explode(*args, **kwargs):
        raise AssertionError("download_bytes should not be called with verify_checksum=False")

    monkeypatch.setattr(archive_service, "download_bytes", explode)

    result = archive_service.download_archive(
        key="p/Card.dmg", config=_CONFIG, destination=tmp_path, verify_checksum=False
    )

    assert result.verification.status == "skipped"


def test_request_restore_rejects_unknown_tier_before_connecting(monkeypatch):
    def explode(**kwargs):
        raise AssertionError("should not create a session for an invalid tier")

    monkeypatch.setattr(archive_service, "create_session", explode)

    with pytest.raises(ValidationError) as exc_info:
        archive_service.request_restore(key="p/Card.dmg", config=_CONFIG, tier="Speedy")

    assert "Standard" in exc_info.value.hint


def test_request_restore_rejects_zero_days_before_connecting(monkeypatch):
    def explode(**kwargs):
        raise AssertionError("should not create a session for invalid days")

    monkeypatch.setattr(archive_service, "create_session", explode)

    with pytest.raises(ValidationError):
        archive_service.request_restore(key="p/Card.dmg", config=_CONFIG, days=0)


def test_request_restore_skips_request_for_directly_readable_object(monkeypatch):
    _stub_connection(monkeypatch)
    _stub_head(monkeypatch, _archive(storage_class="STANDARD"))

    def explode(*args, **kwargs):
        raise AssertionError("restore_archive should not be called for a STANDARD object")

    monkeypatch.setattr(archive_service, "restore_archive", explode)

    outcome = archive_service.request_restore(key="p/Card.dmg", config=_CONFIG)
    assert outcome.outcome == "already_restored"


def test_request_restore_skips_request_when_copy_already_restored(monkeypatch):
    _stub_connection(monkeypatch)
    _stub_head(
        monkeypatch, _archive(storage_class="DEEP_ARCHIVE", restore_state="restored")
    )
    monkeypatch.setattr(
        archive_service,
        "restore_archive",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no request expected")),
    )

    assert archive_service.request_restore(key="p/Card.dmg", config=_CONFIG).outcome == (
        "already_restored"
    )


def test_request_restore_issues_request_for_cold_object(monkeypatch):
    _stub_connection(monkeypatch)
    _stub_head(
        monkeypatch, _archive(storage_class="DEEP_ARCHIVE", restore_state="not_restored")
    )
    seen: dict[str, object] = {}

    def fake_restore(session, *, bucket, key, region, days, tier):
        seen.update(bucket=bucket, key=key, days=days, tier=tier)
        return "requested"

    monkeypatch.setattr(archive_service, "restore_archive", fake_restore)

    outcome = archive_service.request_restore(
        key="p/Card.dmg", config=_CONFIG, days=3, tier="Bulk"
    )

    assert outcome.outcome == "requested"
    assert seen == {"bucket": "b", "key": "p/Card.dmg", "days": 3, "tier": "Bulk"}


def test_list_remote_archives_defaults_to_config_prefix(monkeypatch):
    _stub_connection(monkeypatch)
    seen: dict[str, object] = {}

    def fake_list(session, *, bucket, region, prefix, suffix):
        seen.update(prefix=prefix, suffix=suffix)
        return ()

    monkeypatch.setattr(archive_service, "list_archives", fake_list)

    archive_service.list_remote_archives(config=_CONFIG)
    assert seen == {"prefix": "p", "suffix": ".dmg"}


def test_list_remote_archives_empty_prefix_lists_whole_bucket(monkeypatch):
    _stub_connection(monkeypatch)
    seen: dict[str, object] = {}

    def fake_list(session, *, bucket, region, prefix, suffix):
        seen.update(prefix=prefix, suffix=suffix)
        return ()

    monkeypatch.setattr(archive_service, "list_archives", fake_list)

    archive_service.list_remote_archives(config=_CONFIG, prefix="", all_keys=True)
    assert seen == {"prefix": "", "suffix": None}
