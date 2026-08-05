"""Custom exceptions for archive-dmg.

Every exception here represents an *expected* failure mode with a clear,
user-facing explanation. The CLI layer catches ``ArchiveDmgError`` and renders
``message`` plus ``hint`` without a stack trace. Anything else (a genuine bug)
is allowed to propagate as a traceback.
"""

from __future__ import annotations


class ArchiveDmgError(Exception):
    """Base class for expected, user-facing errors.

    Attributes:
        message: What failed, in plain language.
        hint: The likely reason and/or what the user should do next.
        exit_code: Process exit code the CLI should use for this error.
    """

    exit_code: int = 1

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(ArchiveDmgError):
    """The configuration file or resolved settings are invalid or missing."""


class ValidationError(ArchiveDmgError):
    """A user-supplied path or argument failed validation."""


class DmgVerificationError(ArchiveDmgError):
    """``hdiutil verify`` reported that the DMG is damaged or unreadable."""


class DmgMountError(ArchiveDmgError):
    """The DMG could not be mounted, inspected, or detached."""


class AwsAuthError(ArchiveDmgError):
    """AWS credentials are missing, invalid, or rejected."""


class AwsBucketError(ArchiveDmgError):
    """The configured S3 bucket is missing, unreachable, or misconfigured."""


class AwsUploadError(ArchiveDmgError):
    """An upload to S3 failed or could not be verified after completion."""


class DestinationExistsError(ArchiveDmgError):
    """The destination object already exists and ``--overwrite`` was not given."""
