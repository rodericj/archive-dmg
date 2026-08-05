"""Tests for archive_dmg.dmg: validation, checksum, mount/detach, and inspection.

All hdiutil interaction is mocked via subprocess.run -- no real DMG or disk
image is created or attached.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import time
from pathlib import Path

import pytest

from archive_dmg import dmg
from archive_dmg.errors import DmgMountError, DmgVerificationError, ValidationError


def _completed(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _attach_plist(mount_point: str, dev_entry: str = "/dev/disk4s1") -> bytes:
    return plistlib.dumps(
        {"system-entities": [{"dev-entry": dev_entry, "mount-point": mount_point}]}
    )


# --- validate_dmg_path -------------------------------------------------------


def test_validate_dmg_path_missing(tmp_path):
    with pytest.raises(ValidationError):
        dmg.validate_dmg_path(tmp_path / "missing.dmg")


def test_validate_dmg_path_rejects_directory(tmp_path):
    with pytest.raises(ValidationError):
        dmg.validate_dmg_path(tmp_path)


def test_validate_dmg_path_rejects_wrong_extension(tmp_path):
    file_path = tmp_path / "archive.zip"
    file_path.write_bytes(b"data")
    with pytest.raises(ValidationError):
        dmg.validate_dmg_path(file_path)


def test_validate_dmg_path_accepts_dmg(tmp_path):
    file_path = tmp_path / "Card.dmg"
    file_path.write_bytes(b"data")
    assert dmg.validate_dmg_path(file_path) == file_path


def test_validate_dmg_path_is_case_insensitive(tmp_path):
    file_path = tmp_path / "Card.DMG"
    file_path.write_bytes(b"data")
    assert dmg.validate_dmg_path(file_path) == file_path


# --- verify_checksum ----------------------------------------------------------


def test_verify_checksum_success(tmp_path, monkeypatch):
    monkeypatch.setattr(dmg.subprocess, "run", lambda *a, **k: _completed(returncode=0))
    dmg.verify_checksum(tmp_path / "Card.dmg")


def test_verify_checksum_failure_raises_with_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dmg.subprocess, "run", lambda *a, **k: _completed(returncode=1, stderr=b"checksum bad")
    )
    with pytest.raises(DmgVerificationError) as exc_info:
        dmg.verify_checksum(tmp_path / "Card.dmg")
    assert "checksum bad" in (exc_info.value.hint or "")


# --- mount_readonly / _detach --------------------------------------------------


def test_mount_readonly_yields_mount_point_and_detaches(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "attach":
            return _completed(returncode=0, stdout=_attach_plist(str(tmp_path)))
        if cmd[1] == "detach":
            return _completed(returncode=0)
        raise AssertionError(cmd)

    monkeypatch.setattr(dmg.subprocess, "run", fake_run)

    with dmg.mount_readonly(Path("Card.dmg")) as mount_points:
        assert mount_points == [tmp_path]

    attach_calls = [c for c in calls if c[1] == "attach"]
    detach_calls = [c for c in calls if c[1] == "detach"]
    assert attach_calls == [["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", "Card.dmg"]]
    assert len(detach_calls) == 1
    assert detach_calls[0][2] == "/dev/disk4"


def test_mount_readonly_attach_failure_raises(monkeypatch):
    monkeypatch.setattr(
        dmg.subprocess,
        "run",
        lambda cmd, **kwargs: _completed(returncode=1, stderr=b"not recognized"),
    )
    with pytest.raises(DmgMountError), dmg.mount_readonly(Path("Card.dmg")):
        pass


def test_mount_readonly_no_mount_points_detaches_then_raises(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "attach":
            plist = plistlib.dumps({"system-entities": [{"dev-entry": "/dev/disk5"}]})
            return _completed(returncode=0, stdout=plist)
        if cmd[1] == "detach":
            return _completed(returncode=0)
        raise AssertionError(cmd)

    monkeypatch.setattr(dmg.subprocess, "run", fake_run)

    with pytest.raises(DmgMountError), dmg.mount_readonly(Path("Card.dmg")):
        pass

    assert any(c[1] == "detach" for c in calls)


def test_mount_readonly_detaches_on_exception_from_body(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "attach":
            return _completed(returncode=0, stdout=_attach_plist(str(tmp_path)))
        if cmd[1] == "detach":
            return _completed(returncode=0)
        raise AssertionError(cmd)

    monkeypatch.setattr(dmg.subprocess, "run", fake_run)

    class Boom(Exception):
        pass

    with pytest.raises(Boom), dmg.mount_readonly(Path("Card.dmg")):
        raise Boom("inspection failed")

    assert any(c[1] == "detach" for c in calls)


def test_mount_readonly_detach_failure_does_not_mask_original_exception(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[1] == "attach":
            return _completed(returncode=0, stdout=_attach_plist(str(tmp_path)))
        if cmd[1] == "detach":
            return _completed(returncode=1, stderr=b"still busy")
        raise AssertionError(cmd)

    monkeypatch.setattr(dmg.subprocess, "run", fake_run)
    monkeypatch.setattr(dmg.time, "sleep", lambda *_: None)

    class Boom(Exception):
        pass

    with pytest.raises(Boom), dmg.mount_readonly(Path("Card.dmg")):
        raise Boom("inspection failed")


def test_detach_retries_with_force_then_succeeds(monkeypatch):
    attempts: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        attempts.append(cmd)
        if len(attempts) < 2:
            return _completed(returncode=1, stderr=b"busy")
        return _completed(returncode=0)

    monkeypatch.setattr(dmg.subprocess, "run", fake_run)
    monkeypatch.setattr(dmg.time, "sleep", lambda *_: None)

    dmg._detach("/dev/disk4")

    assert len(attempts) == 2
    assert "-force" in attempts[1]


def test_detach_raises_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(
        dmg.subprocess, "run", lambda cmd, **kwargs: _completed(returncode=1, stderr=b"busy")
    )
    monkeypatch.setattr(dmg.time, "sleep", lambda *_: None)

    with pytest.raises(DmgMountError):
        dmg._detach("/dev/disk4", attempts=2)


# --- inspect_mounted_volume ----------------------------------------------------


def test_inspect_mounted_volume_ignores_macos_metadata(tmp_path):
    mount_point = tmp_path / "mnt"
    (mount_point / "DCIM").mkdir(parents=True)
    (mount_point / ".Spotlight-V100").mkdir()
    (mount_point / ".fseventsd").mkdir()
    (mount_point / ".DS_Store").write_bytes(b"x")
    (mount_point / "DCIM" / "IMG_0001.JPG").write_bytes(b"a")
    (mount_point / "DCIM" / ".DS_Store").write_bytes(b"y")
    (mount_point / "DCIM" / "IMG_0002.JPG").write_bytes(b"b")
    (mount_point / "PRIVATE").mkdir()
    (mount_point / "PRIVATE" / "data.bin").write_bytes(b"c")

    now = time.time()
    os.utime(mount_point / "DCIM" / "IMG_0001.JPG", (now - 100_000, now - 100_000))
    os.utime(mount_point / "DCIM" / "IMG_0002.JPG", (now, now))

    contents = dmg.inspect_mounted_volume([mount_point])

    assert contents.file_count == 3
    assert contents.top_level_entries == ("DCIM", "PRIVATE")
    assert contents.has_dcim is True
    assert contents.earliest_file_modified_utc < contents.latest_file_modified_utc


def test_inspect_mounted_volume_does_not_require_dcim(tmp_path):
    mount_point = tmp_path / "mnt"
    (mount_point / "FOOTAGE").mkdir(parents=True)
    (mount_point / "FOOTAGE" / "clip.mov").write_bytes(b"a")

    contents = dmg.inspect_mounted_volume([mount_point])

    assert contents.has_dcim is False
    assert contents.top_level_entries == ("FOOTAGE",)


# --- verify_dmg (composition) --------------------------------------------------


def test_verify_dmg_composes_checksum_mount_and_inspect(monkeypatch):
    sentinel_contents = dmg.DmgContents(
        file_count=1,
        top_level_entries=("DCIM",),
        earliest_file_modified_utc=None,
        latest_file_modified_utc=None,
    )
    calls: dict[str, object] = {}

    def fake_verify_checksum(path):
        calls["verify_checksum"] = path

    from contextlib import contextmanager

    @contextmanager
    def fake_mount_readonly(path):
        calls["mount_readonly"] = path
        yield [Path("/mnt")]

    def fake_inspect(mount_points):
        calls["inspect"] = mount_points
        return sentinel_contents

    monkeypatch.setattr(dmg, "verify_checksum", fake_verify_checksum)
    monkeypatch.setattr(dmg, "mount_readonly", fake_mount_readonly)
    monkeypatch.setattr(dmg, "inspect_mounted_volume", fake_inspect)

    result = dmg.verify_dmg(Path("Card.dmg"))

    assert result.checksum_verified is True
    assert result.mounted_read_only is True
    assert result.contents is sentinel_contents
    assert calls["verify_checksum"] == Path("Card.dmg")
    assert calls["inspect"] == [Path("/mnt")]
