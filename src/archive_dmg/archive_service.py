"""The shared verify -> checksum -> manifest -> upload pipeline.

This is the one place that knows how to turn a validated DMG into a verified
S3 archive. A future ``archive-dmg create`` command should call
``upload_archive`` directly after producing a DMG from an SD card, rather
than duplicating any verification, checksum, manifest, or upload logic.

Progress/UX is reported through the ``UploadReporter`` hooks below instead of
importing Rich here, which keeps this module trivial to unit test with a
no-op reporter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import boto3

from archive_dmg.aws import (
    RESTORE_TIERS,
    check_bucket_exists,
    create_session,
    download_bytes,
    download_file_with_progress,
    get_caller_identity,
    head_archive,
    list_archives,
    object_exists,
    restore_archive,
    upload_bytes,
    upload_file_with_progress,
    verify_remote_object,
)
from archive_dmg.checksum import parse_sha256_line, sha256_file, write_sha256_file
from archive_dmg.dmg import validate_dmg_path, verify_dmg
from archive_dmg.errors import (
    ArchiveNotRestoredError,
    AwsDownloadError,
    DestinationExistsError,
    ValidationError,
)
from archive_dmg.manifest import build_manifest, write_manifest_file
from archive_dmg.models import (
    ArchiveConfig,
    ArchiveManifest,
    DmgVerificationResult,
    DownloadVerification,
    RemoteArchive,
    RemoteVerificationResult,
    RestoreRequestResult,
    UploadDestination,
)


def normalize_prefix(prefix: str) -> str:
    """Strip slashes so prefixes compose cleanly into an S3 key (no double slashes)."""
    return prefix.strip("/")


def build_destination_key(prefix: str, filename: str) -> str:
    """Join a (possibly empty) prefix and filename into an S3 object key.

    S3 has no real directories -- a "prefix" is just the leading portion of a
    key string. This never creates anything resembling a folder.
    """
    cleaned = normalize_prefix(prefix)
    return f"{cleaned}/{filename}" if cleaned else filename


class UploadReporter:
    """Lifecycle hooks for archive UX. The default no-op implementation is used in tests."""

    def environment_checked(self) -> None:
        pass

    def verification_started(self) -> None:
        pass

    def verification_complete(self, result: DmgVerificationResult) -> None:
        pass

    def checksum_started(self, total_bytes: int) -> None:
        pass

    def checksum_progress(self, bytes_read: int) -> None:
        pass

    def checksum_ready(self, sha256_hex: str) -> None:
        pass

    def upload_started(self, total_bytes: int) -> None:
        pass

    def upload_progress(self, bytes_transferred: int) -> None:
        pass

    def upload_dmg_complete(self) -> None:
        pass

    def upload_sha256_complete(self) -> None:
        pass

    def upload_manifest_complete(self) -> None:
        pass

    def remote_verification_complete(self, result: RemoteVerificationResult) -> None:
        pass


@dataclass(frozen=True, slots=True)
class UploadResult:
    destination: UploadDestination
    verification: DmgVerificationResult
    archive_sha256: str
    archive_size_bytes: int
    manifest: ArchiveManifest
    remote_verification: RemoteVerificationResult


def upload_archive(
    *,
    dmg_path: Path,
    config: ArchiveConfig,
    overwrite: bool,
    reporter: UploadReporter | None = None,
) -> UploadResult:
    """Validate, verify, checksum, upload, and confirm a single DMG archive.

    ``config.default_prefix`` is expected to already reflect CLI > config
    file precedence (see ``config.resolve_config``); this function does not
    re-apply that precedence itself.
    """
    reporter = reporter or UploadReporter()
    dmg_path = validate_dmg_path(dmg_path)

    session = create_session(profile=config.profile, region=config.region)
    get_caller_identity(session)
    check_bucket_exists(session, config.bucket, config.region)
    reporter.environment_checked()

    key = build_destination_key(config.default_prefix, dmg_path.name)
    destination = UploadDestination(bucket=config.bucket, key=key, region=config.region)

    if not overwrite and object_exists(session, config.bucket, key, config.region):
        raise DestinationExistsError(
            f"The destination object already exists:\n\n    {destination.uri}",
            hint="Use --overwrite only if replacing it is intentional.",
        )

    reporter.verification_started()
    verification = verify_dmg(dmg_path)
    reporter.verification_complete(verification)

    archive_size_bytes = dmg_path.stat().st_size
    reporter.checksum_started(archive_size_bytes)
    archive_sha256 = sha256_file(dmg_path, on_bytes_read=reporter.checksum_progress)
    reporter.checksum_ready(archive_sha256)
    sha256_path = write_sha256_file(dmg_path, archive_sha256)

    reporter.upload_started(archive_size_bytes)
    upload_file_with_progress(
        session,
        bucket=config.bucket,
        key=key,
        region=config.region,
        path=dmg_path,
        on_bytes_transferred=reporter.upload_progress,
    )
    reporter.upload_dmg_complete()

    upload_bytes(
        session,
        bucket=config.bucket,
        key=f"{key}.sha256",
        region=config.region,
        data=sha256_path.read_bytes(),
        content_type="text/plain",
    )
    reporter.upload_sha256_complete()

    remote_verification = verify_remote_object(
        session,
        bucket=config.bucket,
        key=key,
        region=config.region,
        local_size=archive_size_bytes,
        local_sha256_hex=archive_sha256,
    )
    reporter.remote_verification_complete(remote_verification)

    manifest = build_manifest(
        dmg_path=dmg_path,
        archive_size_bytes=archive_size_bytes,
        archive_sha256=archive_sha256,
        archive_created_utc=datetime.now(UTC),
        verification=verification,
        destination=destination,
        remote_verification=remote_verification,
    )
    manifest_path = write_manifest_file(dmg_path, manifest)

    upload_bytes(
        session,
        bucket=config.bucket,
        key=f"{key}.manifest.json",
        region=config.region,
        data=manifest_path.read_bytes(),
        content_type="application/json",
    )
    reporter.upload_manifest_complete()

    return UploadResult(
        destination=destination,
        verification=verification,
        archive_sha256=archive_sha256,
        archive_size_bytes=archive_size_bytes,
        manifest=manifest,
        remote_verification=remote_verification,
    )


class DownloadReporter:
    """Lifecycle hooks for the download/restore UX. Default no-op is used in tests."""

    def environment_checked(self) -> None:
        pass

    def archive_resolved(self, archive: RemoteArchive) -> None:
        pass

    def download_started(self, total_bytes: int) -> None:
        pass

    def download_progress(self, bytes_transferred: int) -> None:
        pass

    def download_complete(self, path: Path) -> None:
        pass

    def checksum_started(self, total_bytes: int) -> None:
        pass

    def checksum_progress(self, bytes_read: int) -> None:
        pass

    def verification_complete(self, result: DownloadVerification) -> None:
        pass


@dataclass(frozen=True, slots=True)
class DownloadResult:
    archive: RemoteArchive
    path: Path
    sha256_path: Path | None
    verification: DownloadVerification


def _connect(config: ArchiveConfig) -> boto3.Session:
    """Create a session and confirm credentials and bucket before doing real work."""
    session = create_session(profile=config.profile, region=config.region)
    get_caller_identity(session)
    check_bucket_exists(session, config.bucket, config.region)
    return session


def list_remote_archives(
    *, config: ArchiveConfig, prefix: str | None = None, all_keys: bool = False
) -> tuple[RemoteArchive, ...]:
    """List archives in the configured bucket, newest first.

    ``prefix`` defaults to ``config.default_prefix``; pass an empty string to
    list the whole bucket. ``all_keys`` includes the ``.sha256`` and
    ``.manifest.json`` companions instead of only the archives themselves.
    """
    session = _connect(config)
    effective_prefix = config.default_prefix if prefix is None else prefix
    return list_archives(
        session,
        bucket=config.bucket,
        region=config.region,
        prefix=normalize_prefix(effective_prefix),
        suffix=None if all_keys else ".dmg",
    )


def request_restore(
    *, key: str, config: ArchiveConfig, days: int = 7, tier: str = "Standard"
) -> RestoreRequestResult:
    """Ask S3 for a temporary readable copy of an archived object.

    Restoring an object that is already readable is reported as
    ``already_restored`` rather than issuing a pointless request.
    """
    if tier not in RESTORE_TIERS:
        raise ValidationError(
            f"Unknown retrieval tier: {tier}",
            hint=f"Choose one of: {', '.join(RESTORE_TIERS)}.",
        )
    if days < 1:
        raise ValidationError(
            f"--restore-days must be at least 1 (got {days}).",
            hint="This is how many days the temporary restored copy remains readable.",
        )

    session = _connect(config)
    archive = head_archive(session, bucket=config.bucket, key=key, region=config.region)

    if not archive.is_archived:
        return RestoreRequestResult(
            archive=archive, outcome="already_restored", days=days, tier=tier
        )
    if archive.restore_state == "restored":
        return RestoreRequestResult(
            archive=archive, outcome="already_restored", days=days, tier=tier
        )

    outcome = restore_archive(
        session,
        bucket=config.bucket,
        key=key,
        region=config.region,
        days=days,
        tier=tier,
    )
    return RestoreRequestResult(archive=archive, outcome=outcome, days=days, tier=tier)


def download_archive(
    *,
    key: str,
    config: ArchiveConfig,
    destination: Path,
    overwrite: bool = False,
    verify_checksum: bool = True,
    reporter: DownloadReporter | None = None,
) -> DownloadResult:
    """Download one archived object and verify it against its ``.sha256`` companion.

    Refuses to start when the object is in Glacier storage without a restored
    copy, since the transfer would fail partway rather than cleanly -- the
    caller is directed to ``request_restore`` instead.
    """
    reporter = reporter or DownloadReporter()

    session = _connect(config)
    reporter.environment_checked()

    archive = head_archive(session, bucket=config.bucket, key=key, region=config.region)
    reporter.archive_resolved(archive)

    if not archive.is_downloadable:
        if archive.restore_state == "in_progress":
            raise ArchiveNotRestoredError(
                f"A restore is already in progress for:\n\n    {archive.uri}",
                hint=(
                    "AWS has not finished it yet. Deep Archive restores typically take "
                    "around 12 hours at Standard tier. Re-run this command once "
                    "'archive-dmg list' shows the object as restored."
                ),
            )
        raise ArchiveNotRestoredError(
            f"This object is in {archive.storage_class} and cannot be read directly:"
            f"\n\n    {archive.uri}",
            hint=(
                "Request a temporary copy first:\n\n"
                f"    archive-dmg download '{key}' --restore\n\n"
                "Then download it once the restore completes (hours later)."
            ),
        )

    target = destination / Path(key).name if destination.is_dir() else destination
    if target.exists() and not overwrite:
        raise DestinationExistsError(
            f"A local file already exists at:\n\n    {target}",
            hint="Use --overwrite only if replacing it is intentional.",
        )
    parent = target.parent
    if not parent.exists():
        raise ValidationError(
            f"The destination directory does not exist:\n\n    {parent}",
            hint="Create it first, or pass an existing directory to --output.",
        )

    reporter.download_started(archive.size_bytes)
    download_file_with_progress(
        session,
        bucket=config.bucket,
        key=key,
        region=config.region,
        path=target,
        on_bytes_transferred=reporter.download_progress,
    )
    reporter.download_complete(target)

    actual_size = target.stat().st_size
    if actual_size != archive.size_bytes:
        raise AwsDownloadError(
            "The downloaded file is not the size S3 reported for the object.",
            hint=(
                f"S3 reported {archive.size_bytes} bytes; the local file is "
                f"{actual_size} bytes. Delete it and retry."
            ),
        )

    if not verify_checksum:
        verification = DownloadVerification(status="skipped")
        reporter.verification_complete(verification)
        return DownloadResult(
            archive=archive, path=target, sha256_path=None, verification=verification
        )

    sidecar = download_bytes(
        session, bucket=config.bucket, key=f"{key}.sha256", region=config.region
    )
    expected = parse_sha256_line(sidecar.decode("utf-8", errors="replace")) if sidecar else None
    if expected is None:
        verification = DownloadVerification(status="sidecar_missing")
        reporter.verification_complete(verification)
        return DownloadResult(
            archive=archive, path=target, sha256_path=None, verification=verification
        )

    reporter.checksum_started(actual_size)
    local_sha256 = sha256_file(target, on_bytes_read=reporter.checksum_progress)

    if local_sha256 != expected:
        raise AwsDownloadError(
            "The downloaded file does not match the checksum stored alongside it in S3.",
            hint=(
                f"Expected {expected}\nGot      {local_sha256}\n\n"
                "The download may be corrupted. Delete the local file and retry."
            ),
        )

    sha256_path = write_sha256_file(target, local_sha256)
    verification = DownloadVerification(
        status="verified", local_sha256=local_sha256, expected_sha256=expected
    )
    reporter.verification_complete(verification)
    return DownloadResult(
        archive=archive, path=target, sha256_path=sha256_path, verification=verification
    )
