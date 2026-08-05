"""Tests for archive_dmg.archive_service: key construction and overwrite protection."""

from __future__ import annotations

import pytest

from archive_dmg import archive_service
from archive_dmg.errors import DestinationExistsError
from archive_dmg.models import ArchiveConfig


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
