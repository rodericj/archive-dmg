"""Plain data models shared across archive-dmg.

These are intentionally simple, immutable dataclasses rather than an ORM or a
Pydantic model layer: the data here is small, flows in one direction (disk ->
S3), and does not need validation beyond what ``dmg.py``/``config.py`` already
perform before constructing these objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    """Fully resolved configuration for a single command invocation."""

    bucket: str
    region: str
    default_prefix: str = ""
    profile: str | None = None


@dataclass(frozen=True, slots=True)
class DmgContents:
    """Summary of what is inside a mounted DMG, without cataloging files."""

    file_count: int
    top_level_entries: tuple[str, ...]
    earliest_file_modified_utc: datetime | None
    latest_file_modified_utc: datetime | None

    @property
    def has_dcim(self) -> bool:
        return any(entry.upper() == "DCIM" for entry in self.top_level_entries)


@dataclass(frozen=True, slots=True)
class DmgVerificationResult:
    """Result of running the full DMG verification pipeline."""

    path: Path
    checksum_verified: bool
    mounted_read_only: bool
    contents: DmgContents


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """The identity boto3 authenticated as, per ``sts:GetCallerIdentity``."""

    account: str
    arn: str
    user_id: str


@dataclass(frozen=True, slots=True)
class BucketDiagnostics:
    """Bucket-level configuration checks used by ``doctor``."""

    versioning_enabled: bool
    encryption_enabled: bool
    public_access_blocked: bool
    deep_archive_lifecycle_rule_found: bool
    incomplete_multipart_cleanup_found: bool
    actual_region: str | None = None


@dataclass(frozen=True, slots=True)
class UploadDestination:
    """Where an archive lives (or will live) in S3."""

    bucket: str
    key: str
    region: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


@dataclass(frozen=True, slots=True)
class RemoteVerificationResult:
    """Outcome of comparing the uploaded object against the local file."""

    remote_size_verified: bool
    remote_checksum_status: str
    remote_checksum_verified: bool = False


#: Storage classes whose objects must be restored before they can be read.
#: ``GLACIER_IR`` is deliberately absent -- Instant Retrieval reads directly.
ARCHIVED_STORAGE_CLASSES = frozenset({"GLACIER", "DEEP_ARCHIVE"})


@dataclass(frozen=True, slots=True)
class RemoteArchive:
    """One archived object in S3, as reported by ListObjectsV2 or HeadObject.

    ``restore_state`` is one of:

    ``not_applicable``
        The object is in a directly readable storage class.
    ``not_restored``
        Archived, with no restore requested -- a download will fail.
    ``in_progress``
        A restore was requested and AWS has not finished it yet.
    ``restored``
        A temporary readable copy exists, expiring at ``restore_expiry_utc``.
    """

    bucket: str
    key: str
    region: str
    size_bytes: int
    last_modified_utc: datetime
    storage_class: str
    restore_state: str = "not_applicable"
    restore_expiry_utc: datetime | None = None

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"

    @property
    def is_archived(self) -> bool:
        """Whether this object's storage class requires a restore before reading."""
        return self.storage_class in ARCHIVED_STORAGE_CLASSES

    @property
    def is_downloadable(self) -> bool:
        """Whether a download would succeed right now."""
        return not self.is_archived or self.restore_state == "restored"


@dataclass(frozen=True, slots=True)
class RestoreRequestResult:
    """Outcome of asking S3 for a temporary restored copy.

    ``outcome`` is ``requested`` for a newly accepted request,
    ``already_in_progress`` when one was already running, or
    ``already_restored`` when a readable copy already exists.
    """

    archive: RemoteArchive
    outcome: str
    days: int
    tier: str


@dataclass(frozen=True, slots=True)
class DownloadVerification:
    """Result of checking a downloaded file against its ``.sha256`` companion.

    ``status`` is ``verified`` on a match, ``sidecar_missing`` when the object
    had no ``.sha256`` companion in S3 to compare against, or ``skipped`` when
    the caller opted out.
    """

    status: str
    local_sha256: str | None = None
    expected_sha256: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One row of ``doctor`` output."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str | None = None
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """The full set of checks ``doctor`` performed, in display order."""

    environment_checks: tuple[CheckResult, ...]
    aws_checks: tuple[CheckResult, ...]

    @property
    def all_checks(self) -> tuple[CheckResult, ...]:
        return self.environment_checks + self.aws_checks

    @property
    def has_failures(self) -> bool:
        return any(check.status == "fail" for check in self.all_checks)


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    """The minimal, archival-only manifest written alongside each upload."""

    archive_filename: str
    archive_size_bytes: int
    archive_sha256: str
    archive_created_utc: datetime
    file_count: int
    earliest_file_modified_utc: datetime | None
    latest_file_modified_utc: datetime | None
    top_level_entries: tuple[str, ...]
    destination: UploadDestination
    dmg_checksum_verified: bool
    mounted_read_only: bool
    remote_size_verified: bool
    remote_checksum_verified: bool
    remote_checksum_status: str
    schema_version: int = field(default=1)
