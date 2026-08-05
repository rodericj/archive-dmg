"""boto3 wrappers for authentication, bucket diagnostics, and uploads.

Uses boto3's normal credential provider chain (default profile, ``AWS_PROFILE``,
environment variables, IAM access keys, SSO, assumed roles) -- this module
never reads ``~/.aws/credentials`` itself. Every AWS error that a user could
plausibly hit is mapped to a specific ``ArchiveDmgError`` subclass with an
actionable hint; unexpected errors are wrapped with as much detail as
botocore provides rather than swallowed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
)

from archive_dmg.checksum import sha256_hex_to_base64
from archive_dmg.errors import AwsAuthError, AwsBucketError, AwsUploadError
from archive_dmg.models import BucketDiagnostics, CallerIdentity, RemoteVerificationResult

_MULTIPART_THRESHOLD = 64 * 1024 * 1024
_MULTIPART_CHUNKSIZE = 64 * 1024 * 1024


def create_session(*, profile: str | None, region: str) -> boto3.Session:
    """Create a boto3 session, letting boto3's own provider chain resolve credentials."""
    try:
        return boto3.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as exc:
        raise AwsAuthError(
            f"AWS profile not found: {profile}",
            hint="Check ~/.aws/config for the profile name, or unset --profile / AWS_PROFILE.",
        ) from exc


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _error_message(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Message", str(exc)))


def _auth_failure(exc: ClientError) -> tuple[str, str]:
    code = _error_code(exc)
    if code == "SignatureDoesNotMatch":
        return (
            "AWS credentials were found, but the request signature was rejected.",
            "The access key ID and secret access key may not belong to the same key pair. "
            "Create a new access key or correct the configured credentials.",
        )
    if code == "InvalidClientTokenId":
        return (
            "Unable to authenticate with AWS.",
            "The configured access key ID is not recognized. Run 'aws configure' or verify "
            "the active AWS profile and credentials.",
        )
    if code in ("ExpiredToken", "ExpiredTokenException", "RequestExpired"):
        return (
            "AWS credentials have expired.",
            "Refresh the active profile or run:\n\n    aws configure",
        )
    if code == "AccessDenied":
        return (
            "AWS denied permission to check the active identity.",
            "Verify the active AWS identity has permission to call sts:GetCallerIdentity.",
        )
    return (f"Unable to authenticate with AWS ({code or 'unknown error'}).", _error_message(exc))


def _upload_failure(exc: ClientError) -> tuple[str, str]:
    code = _error_code(exc)
    if code in ("ExpiredToken", "ExpiredTokenException", "RequestExpired"):
        return (
            "Upload failed because AWS credentials expired.",
            "Refresh the active profile or run:\n\n    aws configure",
        )
    if code == "AccessDenied":
        return (
            "AWS denied permission to upload to this bucket.",
            "Verify the active AWS identity has s3:PutObject permission on the destination.",
        )
    if code == "NoSuchBucket":
        return (
            "Upload failed: the destination bucket no longer exists.",
            "Check the bucket name in your config file or --bucket argument.",
        )
    return (f"Upload failed ({code or 'unknown error'}).", _error_message(exc))


def get_caller_identity(session: boto3.Session) -> CallerIdentity:
    """Confirm AWS credentials work via ``sts:GetCallerIdentity``."""
    sts = session.client("sts")
    try:
        response = sts.get_caller_identity()
    except NoCredentialsError as exc:
        raise AwsAuthError(
            "Unable to authenticate with AWS.",
            hint="Run:\n\n    aws configure\n\nor verify the active AWS profile and credentials.",
        ) from exc
    except PartialCredentialsError as exc:
        raise AwsAuthError(
            "AWS credentials are incomplete.",
            hint="Run 'aws configure' to set both the access key ID and secret access key.",
        ) from exc
    except ClientError as exc:
        message, hint = _auth_failure(exc)
        raise AwsAuthError(message, hint=hint) from exc
    except EndpointConnectionError as exc:
        raise AwsAuthError(
            "Could not reach AWS.",
            hint="Check your network connection and try again.",
        ) from exc
    return CallerIdentity(
        account=response["Account"], arn=response["Arn"], user_id=response["UserId"]
    )


def check_bucket_exists(session: boto3.Session, bucket: str, region: str) -> None:
    """Confirm the bucket exists and is reachable in the configured region."""
    s3 = session.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = _error_code(exc)
        if status == 404 or code in ("404", "NoSuchBucket"):
            raise AwsBucketError(
                f"Bucket not found:\n\n    {bucket}",
                hint="Check the bucket name in your config file or --bucket argument.",
            ) from exc
        if status == 301 or code == "PermanentRedirect":
            actual_region = (
                exc.response.get("ResponseMetadata", {})
                .get("HTTPHeaders", {})
                .get("x-amz-bucket-region")
            )
            raise AwsBucketError(
                f"Bucket '{bucket}' is not in the configured region ({region}).",
                hint=(
                    f'Set region = "{actual_region}" in your config file or --region.'
                    if actual_region
                    else "Check the bucket's actual region and update --region."
                ),
            ) from exc
        if status == 403 or code in ("403", "AccessDenied"):
            raise AwsBucketError(
                f"Access denied to bucket:\n\n    {bucket}",
                hint="Verify the active AWS identity has permission to access this bucket.",
            ) from exc
        raise AwsBucketError(
            f"Could not verify bucket '{bucket}'.", hint=_error_message(exc)
        ) from exc
    except EndpointConnectionError as exc:
        raise AwsBucketError(
            "Could not reach AWS.",
            hint="Check your network connection and try again.",
        ) from exc


def get_bucket_diagnostics(session: boto3.Session, bucket: str, region: str) -> BucketDiagnostics:
    """Inspect versioning, encryption, public access block, and lifecycle rules.

    Missing configuration (e.g. no lifecycle rule at all) is reported as a
    ``False`` flag rather than an exception -- these are advisory checks used
    by ``doctor``, not hard failures.
    """
    s3 = session.client("s3", region_name=region)

    versioning_enabled = False
    try:
        response = s3.get_bucket_versioning(Bucket=bucket)
        versioning_enabled = response.get("Status") == "Enabled"
    except ClientError as exc:
        raise AwsBucketError(
            f"Could not read versioning settings for '{bucket}'.", hint=_error_message(exc)
        ) from exc

    encryption_enabled = False
    try:
        enc_response = s3.get_bucket_encryption(Bucket=bucket)
        rules = enc_response.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        encryption_enabled = any(
            rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") for rule in rules
        )
    except ClientError as exc:
        if _error_code(exc) != "ServerSideEncryptionConfigurationNotFoundError":
            raise AwsBucketError(
                f"Could not read encryption settings for '{bucket}'.", hint=_error_message(exc)
            ) from exc

    public_access_blocked = False
    try:
        pab_response = s3.get_public_access_block(Bucket=bucket)
        config = pab_response.get("PublicAccessBlockConfiguration", {})
        public_access_blocked = all(
            config.get(flag, False)
            for flag in (
                "BlockPublicAcls",
                "IgnorePublicAcls",
                "BlockPublicPolicy",
                "RestrictPublicBuckets",
            )
        )
    except ClientError as exc:
        if _error_code(exc) != "NoSuchPublicAccessBlockConfiguration":
            raise AwsBucketError(
                f"Could not read public access block settings for '{bucket}'.",
                hint=_error_message(exc),
            ) from exc

    deep_archive_found = False
    multipart_cleanup_found = False
    try:
        lifecycle_response = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
        for rule in lifecycle_response.get("Rules", []):
            if rule.get("Status") != "Enabled":
                continue
            transitions = list(rule.get("Transitions", [])) + list(
                rule.get("NoncurrentVersionTransitions", [])
            )
            if any(t.get("StorageClass") == "DEEP_ARCHIVE" for t in transitions):
                deep_archive_found = True
            if "AbortIncompleteMultipartUpload" in rule:
                multipart_cleanup_found = True
    except ClientError as exc:
        if _error_code(exc) != "NoSuchLifecycleConfiguration":
            raise AwsBucketError(
                f"Could not read lifecycle configuration for '{bucket}'.",
                hint=_error_message(exc),
            ) from exc

    return BucketDiagnostics(
        versioning_enabled=versioning_enabled,
        encryption_enabled=encryption_enabled,
        public_access_blocked=public_access_blocked,
        deep_archive_lifecycle_rule_found=deep_archive_found,
        incomplete_multipart_cleanup_found=multipart_cleanup_found,
    )


def object_exists(session: boto3.Session, bucket: str, key: str, region: str) -> bool:
    """Return whether ``key`` already exists in ``bucket``."""
    s3 = session.client("s3", region_name=region)
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 404:
            return False
        raise AwsBucketError(
            f"Could not check for an existing object at '{key}'.", hint=_error_message(exc)
        ) from exc


def upload_file_with_progress(
    session: boto3.Session,
    *,
    bucket: str,
    key: str,
    region: str,
    path: Path,
    on_bytes_transferred: Callable[[int], None],
) -> None:
    """Upload ``path`` to S3, using automatic multipart for large files.

    Requests an S3-computed SHA-256 checksum via ``ChecksumAlgorithm`` so the
    stored checksum can later be compared where S3's representation allows it.
    """
    s3 = session.client("s3", region_name=region)
    transfer_config = TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD,
        multipart_chunksize=_MULTIPART_CHUNKSIZE,
        max_concurrency=4,
        use_threads=True,
    )
    try:
        s3.upload_file(
            Filename=str(path),
            Bucket=bucket,
            Key=key,
            Config=transfer_config,
            Callback=on_bytes_transferred,
            ExtraArgs={"ChecksumAlgorithm": "SHA256"},
        )
    except ClientError as exc:
        message, hint = _upload_failure(exc)
        raise AwsUploadError(message, hint=hint) from exc
    except EndpointConnectionError as exc:
        raise AwsUploadError(
            "Could not reach AWS during upload.",
            hint="Check your network connection and try again.",
        ) from exc
    except (BotoCoreError, OSError) as exc:
        raise AwsUploadError(f"Upload failed: {path.name}", hint=str(exc)) from exc


def upload_bytes(
    session: boto3.Session,
    *,
    bucket: str,
    key: str,
    region: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> None:
    """Upload a small in-memory payload (the .sha256 or .manifest.json companion files)."""
    s3 = session.client("s3", region_name=region)
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ChecksumAlgorithm="SHA256",
        )
    except ClientError as exc:
        message, hint = _upload_failure(exc)
        raise AwsUploadError(message, hint=hint) from exc


def verify_remote_object(
    session: boto3.Session,
    *,
    bucket: str,
    key: str,
    region: str,
    local_size: int,
    local_sha256_hex: str,
) -> RemoteVerificationResult:
    """Compare the uploaded object against the local file.

    A direct SHA-256 comparison is only meaningful when S3 stored a
    whole-object checksum (i.e. the upload was not multipart). For multipart
    uploads, S3's ``ChecksumSHA256`` is a checksum-of-checksums and is *not*
    directly comparable to a plain SHA-256 of the file -- that case is
    reported as ``stored_by_s3_not_directly_comparable`` rather than silently
    claiming a match.
    """
    s3 = session.client("s3", region_name=region)
    try:
        response = s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    except ClientError as exc:
        raise AwsUploadError(
            f"Could not verify the uploaded object: {key}", hint=_error_message(exc)
        ) from exc

    remote_size = response.get("ContentLength", -1)
    if remote_size != local_size:
        raise AwsUploadError(
            "Uploaded object size does not match the local file.",
            hint=f"Local size is {local_size} bytes; remote size is {remote_size} bytes.",
        )

    parts_count = response.get("PartsCount")
    remote_checksum_b64 = response.get("ChecksumSHA256")

    if parts_count and parts_count > 1:
        return RemoteVerificationResult(
            remote_size_verified=True,
            remote_checksum_verified=False,
            remote_checksum_status="stored_by_s3_not_directly_comparable",
        )

    if not remote_checksum_b64:
        return RemoteVerificationResult(
            remote_size_verified=True,
            remote_checksum_verified=False,
            remote_checksum_status="not_available",
        )

    if remote_checksum_b64 == sha256_hex_to_base64(local_sha256_hex):
        return RemoteVerificationResult(
            remote_size_verified=True,
            remote_checksum_verified=True,
            remote_checksum_status="verified_direct_match",
        )

    raise AwsUploadError(
        "Uploaded object checksum does not match the local file.",
        hint="The upload may be corrupted. Investigate, then retry with --overwrite.",
    )
