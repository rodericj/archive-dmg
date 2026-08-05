"""Environment and AWS diagnostic checks used by ``archive-dmg doctor``.

Each check returns a plain ``CheckResult`` rather than printing directly, so
this module has no Rich dependency and can be unit tested by asserting on
returned data.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from archive_dmg.aws import check_bucket_exists, create_session, get_bucket_diagnostics
from archive_dmg.aws import get_caller_identity as aws_get_caller_identity
from archive_dmg.errors import ArchiveDmgError
from archive_dmg.models import CheckResult, DoctorReport

_MIN_PYTHON = (3, 11)


def _check_python_version() -> CheckResult:
    version = sys.version_info
    label = f"Python {version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) >= _MIN_PYTHON:
        return CheckResult(name=label, status="ok")
    return CheckResult(
        name=label,
        status="fail",
        detail=f"archive-dmg requires Python {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+.",
        hint="Install a newer Python, e.g. 'brew install python@3.12'.",
    )


def _check_macos() -> CheckResult:
    if platform.system() == "Darwin":
        return CheckResult(name="macOS", status="ok")
    return CheckResult(
        name="macOS",
        status="fail",
        detail=f"Detected {platform.system()}, not macOS.",
        hint="archive-dmg relies on hdiutil and only runs on macOS.",
    )


def _check_hdiutil() -> CheckResult:
    if shutil.which("hdiutil"):
        return CheckResult(name="hdiutil", status="ok")
    return CheckResult(
        name="hdiutil",
        status="fail",
        detail="hdiutil not found on PATH.",
        hint="hdiutil ships with macOS; this system may be misconfigured.",
    )


def _check_sha256_support() -> CheckResult:
    # hashlib.sha256 is always available in the standard library, so this
    # check can never truly fail -- it just notes whether shasum is also
    # available for manual verification.
    if shutil.which("shasum"):
        return CheckResult(name="shasum or Python SHA-256 support", status="ok")
    return CheckResult(
        name="shasum or Python SHA-256 support",
        status="ok",
        detail="shasum not found; Python's built-in hashlib.sha256 will be used instead.",
    )


def _check_aws_cli() -> CheckResult:
    aws_path = shutil.which("aws")
    if not aws_path:
        return CheckResult(
            name="AWS CLI",
            status="warn",
            detail="AWS CLI not found.",
            hint=(
                "Install it with:\n\n    brew install awscli\n\n"
                "Not required for uploads (boto3 is used directly), but useful for "
                "diagnostics and manual recovery."
            ),
        )
    try:
        result = subprocess.run(["aws", "--version"], capture_output=True, check=False)
        version = (
            result.stdout.decode("utf-8", "replace").strip()
            or result.stderr.decode("utf-8", "replace").strip()
        )
    except OSError:
        version = None
    return CheckResult(name="AWS CLI", status="ok", detail=version or aws_path)


def run_environment_checks() -> tuple[CheckResult, ...]:
    """Checks that don't require AWS credentials or network access."""
    return (
        _check_python_version(),
        _check_macos(),
        _check_hdiutil(),
        _check_sha256_support(),
        _check_aws_cli(),
    )


def run_aws_checks(*, profile: str | None, bucket: str, region: str) -> tuple[CheckResult, ...]:
    """Checks that require live AWS access: identity, bucket, and bucket configuration."""
    checks: list[CheckResult] = []

    try:
        session = create_session(profile=profile, region=region)
        identity = aws_get_caller_identity(session)
    except ArchiveDmgError as exc:
        checks.append(
            CheckResult(
                name="Credentials are usable", status="fail", detail=exc.message, hint=exc.hint
            )
        )
        checks.append(
            CheckResult(
                name="Bucket exists",
                status="fail",
                detail="Skipped because AWS authentication failed.",
            )
        )
        return tuple(checks)

    checks.append(CheckResult(name="Credentials are usable", status="ok", detail=identity.arn))

    try:
        check_bucket_exists(session, bucket, region)
    except ArchiveDmgError as exc:
        checks.append(
            CheckResult(name="Bucket exists", status="fail", detail=exc.message, hint=exc.hint)
        )
        return tuple(checks)

    checks.append(CheckResult(name="Bucket exists", status="ok", detail=bucket))
    checks.append(CheckResult(name="Region", status="ok", detail=region))

    try:
        diagnostics = get_bucket_diagnostics(session, bucket, region)
    except ArchiveDmgError as exc:
        checks.append(
            CheckResult(
                name="Bucket configuration", status="fail", detail=exc.message, hint=exc.hint
            )
        )
        return tuple(checks)

    checks.append(
        CheckResult(
            name="Versioning enabled",
            status="ok" if diagnostics.versioning_enabled else "warn",
            detail=None if diagnostics.versioning_enabled else "Bucket versioning is not enabled.",
            hint=None
            if diagnostics.versioning_enabled
            else "Enable versioning so overwritten archives remain recoverable.",
        )
    )
    checks.append(
        CheckResult(
            name="Default encryption enabled",
            status="ok" if diagnostics.encryption_enabled else "warn",
            detail=None
            if diagnostics.encryption_enabled
            else "No default encryption is configured on the bucket.",
            hint=None
            if diagnostics.encryption_enabled
            else "Enable default (SSE-S3 or SSE-KMS) encryption on the bucket.",
        )
    )
    checks.append(
        CheckResult(
            name="Public access blocked",
            status="ok" if diagnostics.public_access_blocked else "warn",
            detail=None
            if diagnostics.public_access_blocked
            else "Public access block is not fully enabled.",
            hint=None
            if diagnostics.public_access_blocked
            else "Enable all four S3 Block Public Access settings on the bucket.",
        )
    )
    checks.append(
        CheckResult(
            name="Deep Archive lifecycle rule found",
            status="ok" if diagnostics.deep_archive_lifecycle_rule_found else "warn",
            detail=None
            if diagnostics.deep_archive_lifecycle_rule_found
            else (
                "The bucket lifecycle configuration does not include a transition to "
                "Glacier Deep Archive."
            ),
            hint=None
            if diagnostics.deep_archive_lifecycle_rule_found
            else "Uploads will remain in a more expensive storage class.",
        )
    )
    checks.append(
        CheckResult(
            name="Incomplete multipart uploads are cleaned up",
            status="ok" if diagnostics.incomplete_multipart_cleanup_found else "warn",
            detail=None
            if diagnostics.incomplete_multipart_cleanup_found
            else "No lifecycle rule aborts incomplete multipart uploads.",
            hint=None
            if diagnostics.incomplete_multipart_cleanup_found
            else (
                "Add an AbortIncompleteMultipartUpload lifecycle rule to avoid paying for "
                "abandoned uploads."
            ),
        )
    )

    return tuple(checks)


def run_doctor(*, profile: str | None, bucket: str, region: str) -> DoctorReport:
    """Run every environment and AWS check and return a combined report."""
    return DoctorReport(
        environment_checks=run_environment_checks(),
        aws_checks=run_aws_checks(profile=profile, bucket=bucket, region=region),
    )
