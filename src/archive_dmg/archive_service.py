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

from archive_dmg.aws import (
    check_bucket_exists,
    create_session,
    get_caller_identity,
    object_exists,
    upload_bytes,
    upload_file_with_progress,
    verify_remote_object,
)
from archive_dmg.checksum import sha256_file, write_sha256_file
from archive_dmg.dmg import validate_dmg_path, verify_dmg
from archive_dmg.errors import DestinationExistsError
from archive_dmg.manifest import build_manifest, write_manifest_file
from archive_dmg.models import (
    ArchiveConfig,
    ArchiveManifest,
    DmgVerificationResult,
    RemoteVerificationResult,
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

    def verification_complete(self, result: DmgVerificationResult) -> None:
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

    verification = verify_dmg(dmg_path)
    reporter.verification_complete(verification)

    archive_sha256 = sha256_file(dmg_path, on_bytes_read=reporter.checksum_progress)
    reporter.checksum_ready(archive_sha256)
    sha256_path = write_sha256_file(dmg_path, archive_sha256)
    archive_size_bytes = dmg_path.stat().st_size

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
