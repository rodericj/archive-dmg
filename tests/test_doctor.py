"""Tests for archive_dmg.doctor."""

from __future__ import annotations

from archive_dmg import doctor
from archive_dmg.errors import AwsAuthError


def test_check_hdiutil_missing(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor._check_hdiutil()
    assert result.status == "fail"


def test_check_hdiutil_present(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/hdiutil")
    result = doctor._check_hdiutil()
    assert result.status == "ok"


def test_check_aws_cli_missing_is_a_warning_not_a_failure(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor._check_aws_cli()
    assert result.status == "warn"
    assert "brew install awscli" in (result.hint or "")


def test_check_sha256_support_is_always_ok(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor._check_sha256_support().status == "ok"


def test_run_aws_checks_stops_after_auth_failure(monkeypatch):
    def fake_create_session(**kwargs):
        raise AwsAuthError("boom", hint="fix it")

    monkeypatch.setattr(doctor, "create_session", fake_create_session)

    checks = doctor.run_aws_checks(profile=None, bucket="b", region="us-west-2")

    assert checks[0].name == "Credentials are usable"
    assert checks[0].status == "fail"
    assert checks[0].detail == "boom"
    assert checks[1].name == "Bucket exists"
    assert checks[1].status == "fail"
    assert len(checks) == 2


def test_run_environment_checks_returns_five_checks():
    checks = doctor.run_environment_checks()
    names = [check.name for check in checks]
    assert any("Python" in name for name in names)
    assert "macOS" in names
    assert "hdiutil" in names
    assert "AWS CLI" in names
