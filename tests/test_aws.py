"""Tests for archive_dmg.aws using botocore's Stubber -- no live AWS calls.

boto3.Session is not used directly here: a small FakeSession stands in for
it so each test can hand a pre-stubbed client to the function under test.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from botocore.stub import Stubber

from archive_dmg import aws
from archive_dmg.errors import AwsAuthError, AwsBucketError, AwsDownloadError, AwsUploadError


class FakeSession:
    def __init__(self, **clients: object) -> None:
        self._clients = clients

    def client(self, service_name: str, **kwargs: object) -> object:
        return self._clients[service_name]


class RaisingClient:
    """Stands in for a boto3 client whose call raises before any HTTP response."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_caller_identity(self) -> None:
        raise self._exc


def _client(service: str, region: str = "us-west-2"):
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )


# --- get_caller_identity -------------------------------------------------------


def test_get_caller_identity_success():
    sts = _client("sts")
    stubber = Stubber(sts)
    stubber.add_response(
        "get_caller_identity",
        {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/x", "UserId": "AID"},
    )
    with stubber:
        identity = aws.get_caller_identity(FakeSession(sts=sts))
    assert identity.account == "123456789012"
    assert identity.arn == "arn:aws:iam::123456789012:user/x"


def test_get_caller_identity_no_credentials():
    session = FakeSession(sts=RaisingClient(NoCredentialsError()))
    with pytest.raises(AwsAuthError) as exc_info:
        aws.get_caller_identity(session)
    assert "aws configure" in exc_info.value.hint.lower()


def test_get_caller_identity_partial_credentials():
    session = FakeSession(
        sts=RaisingClient(PartialCredentialsError(provider="env", cred_var="AWS_SECRET_ACCESS_KEY"))
    )
    with pytest.raises(AwsAuthError) as exc_info:
        aws.get_caller_identity(session)
    assert "incomplete" in exc_info.value.message.lower()


def test_get_caller_identity_signature_does_not_match():
    sts = _client("sts")
    stubber = Stubber(sts)
    stubber.add_client_error(
        "get_caller_identity",
        service_error_code="SignatureDoesNotMatch",
        service_message="signature mismatch",
        http_status_code=403,
    )
    with stubber, pytest.raises(AwsAuthError) as exc_info:
        aws.get_caller_identity(FakeSession(sts=sts))
    assert "signature was rejected" in exc_info.value.message
    assert "key pair" in (exc_info.value.hint or "")


def test_get_caller_identity_invalid_client_token():
    sts = _client("sts")
    stubber = Stubber(sts)
    stubber.add_client_error(
        "get_caller_identity", service_error_code="InvalidClientTokenId", http_status_code=403
    )
    with stubber, pytest.raises(AwsAuthError) as exc_info:
        aws.get_caller_identity(FakeSession(sts=sts))
    assert "not recognized" in (exc_info.value.hint or "")


# --- check_bucket_exists --------------------------------------------------------


def test_check_bucket_exists_success():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("head_bucket", {})
    with stubber:
        aws.check_bucket_exists(FakeSession(s3=s3), "my-bucket", "us-west-2")


def test_check_bucket_exists_not_found():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    with stubber, pytest.raises(AwsBucketError) as exc_info:
        aws.check_bucket_exists(FakeSession(s3=s3), "my-bucket", "us-west-2")
    assert "Bucket not found" in exc_info.value.message


def test_check_bucket_exists_access_denied():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_client_error("head_bucket", service_error_code="403", http_status_code=403)
    with stubber, pytest.raises(AwsBucketError) as exc_info:
        aws.check_bucket_exists(FakeSession(s3=s3), "my-bucket", "us-west-2")
    assert "Access denied" in exc_info.value.message


# --- object_exists ---------------------------------------------------------------


def test_object_exists_true():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("head_object", {"ContentLength": 10})
    with stubber:
        assert aws.object_exists(FakeSession(s3=s3), "b", "k", "us-west-2") is True


def test_object_exists_false():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    with stubber:
        assert aws.object_exists(FakeSession(s3=s3), "b", "k", "us-west-2") is False


# --- get_bucket_diagnostics --------------------------------------------------------


def test_get_bucket_diagnostics_all_enabled():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("get_bucket_versioning", {"Status": "Enabled"})
    stubber.add_response(
        "get_bucket_encryption",
        {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            }
        },
    )
    stubber.add_response(
        "get_public_access_block",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        },
    )
    stubber.add_response(
        "get_bucket_lifecycle_configuration",
        {
            "Rules": [
                {
                    "Status": "Enabled",
                    "Transitions": [{"StorageClass": "DEEP_ARCHIVE", "Days": 30}],
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                }
            ]
        },
    )
    with stubber:
        diagnostics = aws.get_bucket_diagnostics(FakeSession(s3=s3), "b", "us-west-2")

    assert diagnostics.versioning_enabled
    assert diagnostics.encryption_enabled
    assert diagnostics.public_access_blocked
    assert diagnostics.deep_archive_lifecycle_rule_found
    assert diagnostics.incomplete_multipart_cleanup_found


def test_get_bucket_diagnostics_missing_configuration_is_not_an_error():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("get_bucket_versioning", {})
    stubber.add_client_error(
        "get_bucket_encryption", service_error_code="ServerSideEncryptionConfigurationNotFoundError"
    )
    stubber.add_client_error(
        "get_public_access_block", service_error_code="NoSuchPublicAccessBlockConfiguration"
    )
    stubber.add_client_error(
        "get_bucket_lifecycle_configuration", service_error_code="NoSuchLifecycleConfiguration"
    )
    with stubber:
        diagnostics = aws.get_bucket_diagnostics(FakeSession(s3=s3), "b", "us-west-2")

    assert diagnostics == aws.BucketDiagnostics(
        versioning_enabled=False,
        encryption_enabled=False,
        public_access_blocked=False,
        deep_archive_lifecycle_rule_found=False,
        incomplete_multipart_cleanup_found=False,
    )


def test_get_bucket_diagnostics_lifecycle_without_deep_archive():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("get_bucket_versioning", {"Status": "Enabled"})
    stubber.add_response(
        "get_bucket_encryption",
        {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            }
        },
    )
    stubber.add_response(
        "get_public_access_block",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        },
    )
    stubber.add_response(
        "get_bucket_lifecycle_configuration",
        {"Rules": [{"Status": "Enabled", "Transitions": [{"StorageClass": "GLACIER"}]}]},
    )
    with stubber:
        diagnostics = aws.get_bucket_diagnostics(FakeSession(s3=s3), "b", "us-west-2")

    assert diagnostics.deep_archive_lifecycle_rule_found is False
    assert diagnostics.incomplete_multipart_cleanup_found is False


# --- verify_remote_object -------------------------------------------------------


def test_verify_remote_object_direct_match():
    digest_hex = "00" * 32
    checksum_b64 = aws.sha256_hex_to_base64(digest_hex)
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("head_object", {"ContentLength": 100, "ChecksumSHA256": checksum_b64})
    stubber.add_response("head_object", {})  # PartNumber=1 probe: no PartsCount => not multipart
    with stubber:
        result = aws.verify_remote_object(
            FakeSession(s3=s3),
            bucket="b",
            key="k",
            region="us-west-2",
            local_size=100,
            local_sha256_hex=digest_hex,
        )
    assert result.remote_size_verified is True
    assert result.remote_checksum_verified is True
    assert result.remote_checksum_status == "verified_direct_match"


def test_verify_remote_object_multipart_is_not_directly_comparable():
    s3 = _client("s3")
    stubber = Stubber(s3)
    # A plain head_object never includes PartsCount, multipart or not -- only
    # the PartNumber=1 probe does. The composite checksum is still present.
    stubber.add_response("head_object", {"ContentLength": 100, "ChecksumSHA256": "composite=="})
    stubber.add_response("head_object", {"PartsCount": 3})
    with stubber:
        result = aws.verify_remote_object(
            FakeSession(s3=s3),
            bucket="b",
            key="k",
            region="us-west-2",
            local_size=100,
            local_sha256_hex="00" * 32,
        )
    assert result.remote_size_verified is True
    assert result.remote_checksum_verified is False
    assert result.remote_checksum_status == "stored_by_s3_not_directly_comparable"


def test_verify_remote_object_no_checksum_available():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("head_object", {"ContentLength": 100})
    stubber.add_response("head_object", {})
    with stubber:
        result = aws.verify_remote_object(
            FakeSession(s3=s3),
            bucket="b",
            key="k",
            region="us-west-2",
            local_size=100,
            local_sha256_hex="00" * 32,
        )
    assert result.remote_checksum_status == "not_available"
    assert result.remote_checksum_verified is False


def test_verify_remote_object_size_mismatch_raises():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("head_object", {"ContentLength": 50})
    with stubber, pytest.raises(AwsUploadError):
        aws.verify_remote_object(
            FakeSession(s3=s3),
            bucket="b",
            key="k",
            region="us-west-2",
            local_size=100,
            local_sha256_hex="00" * 32,
        )


def test_verify_remote_object_checksum_mismatch_raises():
    s3 = _client("s3")
    stubber = Stubber(s3)
    wrong_checksum = aws.sha256_hex_to_base64("11" * 32)
    stubber.add_response("head_object", {"ContentLength": 100, "ChecksumSHA256": wrong_checksum})
    stubber.add_response("head_object", {})
    with stubber, pytest.raises(AwsUploadError):
        aws.verify_remote_object(
            FakeSession(s3=s3),
            bucket="b",
            key="k",
            region="us-west-2",
            local_size=100,
            local_sha256_hex="00" * 32,
        )


def test_is_multipart_object_true_when_parts_count_greater_than_one():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("head_object", {"PartsCount": 5})
    with stubber:
        assert aws._is_multipart_object(s3, "b", "k") is True


def test_is_multipart_object_false_when_parts_count_absent():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response("head_object", {})
    with stubber:
        assert aws._is_multipart_object(s3, "b", "k") is False


# --- create_session -------------------------------------------------------------


def test_create_session_unknown_profile_raises():
    with pytest.raises(AwsAuthError):
        aws.create_session(profile="archive-dmg-test-profile-does-not-exist", region="us-west-2")


# --- _parse_restore_state ------------------------------------------------------


def test_parse_restore_state_not_applicable_for_standard():
    assert aws._parse_restore_state("STANDARD", None) == ("not_applicable", None)


def test_parse_restore_state_archived_with_no_header_is_not_restored():
    assert aws._parse_restore_state("DEEP_ARCHIVE", None) == ("not_restored", None)


def test_parse_restore_state_detects_in_progress():
    state, expiry = aws._parse_restore_state("DEEP_ARCHIVE", 'ongoing-request="true"')
    assert state == "in_progress"
    assert expiry is None


def test_parse_restore_state_detects_restored_with_expiry():
    header = 'ongoing-request="false", expiry-date="Fri, 21 Dec 2012 00:00:00 GMT"'
    state, expiry = aws._parse_restore_state("DEEP_ARCHIVE", header)
    assert state == "restored"
    assert expiry is not None
    assert (expiry.year, expiry.month, expiry.day) == (2012, 12, 21)


def test_parse_restore_state_restored_tolerates_unparseable_expiry():
    header = 'ongoing-request="false", expiry-date="not a date"'
    assert aws._parse_restore_state("DEEP_ARCHIVE", header) == ("restored", None)


# --- list_archives -------------------------------------------------------------


def _dt(day: int):
    from datetime import UTC, datetime

    return datetime(2026, 1, day, tzinfo=UTC)


def test_list_archives_filters_to_dmg_and_sorts_newest_first():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response(
        "list_objects_v2",
        {
            "Contents": [
                {"Key": "p/old.dmg", "Size": 10, "LastModified": _dt(1)},
                {"Key": "p/new.dmg", "Size": 20, "LastModified": _dt(9)},
                {"Key": "p/new.dmg.sha256", "Size": 8, "LastModified": _dt(9)},
            ]
        },
        {"Bucket": "b", "Prefix": "p", "OptionalObjectAttributes": ["RestoreStatus"]},
    )
    with stubber:
        out = aws.list_archives(FakeSession(s3=s3), bucket="b", region="us-west-2", prefix="p")
    assert [a.key for a in out] == ["p/new.dmg", "p/old.dmg"]


def test_list_archives_all_keys_includes_companions():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response(
        "list_objects_v2",
        {
            "Contents": [
                {"Key": "p/a.dmg", "Size": 10, "LastModified": _dt(1)},
                {"Key": "p/a.dmg.sha256", "Size": 8, "LastModified": _dt(1)},
            ]
        },
        {"Bucket": "b", "Prefix": "p", "OptionalObjectAttributes": ["RestoreStatus"]},
    )
    with stubber:
        out = aws.list_archives(
            FakeSession(s3=s3), bucket="b", region="us-west-2", prefix="p", suffix=None
        )
    assert len(out) == 2


def test_list_archives_maps_restore_status_from_list_response():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response(
        "list_objects_v2",
        {
            "Contents": [
                {
                    "Key": "p/pending.dmg",
                    "Size": 1,
                    "LastModified": _dt(3),
                    "StorageClass": "DEEP_ARCHIVE",
                    "RestoreStatus": {"IsRestoreInProgress": True},
                },
                {
                    "Key": "p/cold.dmg",
                    "Size": 1,
                    "LastModified": _dt(2),
                    "StorageClass": "DEEP_ARCHIVE",
                },
                {
                    "Key": "p/warm.dmg",
                    "Size": 1,
                    "LastModified": _dt(1),
                    "StorageClass": "DEEP_ARCHIVE",
                    "RestoreStatus": {"IsRestoreInProgress": False, "RestoreExpiryDate": _dt(20)},
                },
            ]
        },
        {"Bucket": "b", "Prefix": "p", "OptionalObjectAttributes": ["RestoreStatus"]},
    )
    with stubber:
        out = aws.list_archives(FakeSession(s3=s3), bucket="b", region="us-west-2", prefix="p")
    states = {a.key: a.restore_state for a in out}
    assert states == {
        "p/pending.dmg": "in_progress",
        "p/cold.dmg": "not_restored",
        "p/warm.dmg": "restored",
    }
    assert all(a.is_archived for a in out)
    assert [a.is_downloadable for a in out if a.key == "p/warm.dmg"] == [True]
    assert [a.is_downloadable for a in out if a.key == "p/cold.dmg"] == [False]


def test_list_archives_access_denied_is_bucket_error():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_client_error("list_objects_v2", service_error_code="AccessDenied")
    with stubber, pytest.raises(AwsBucketError) as exc_info:
        aws.list_archives(FakeSession(s3=s3), bucket="b", region="us-west-2")
    assert "ListBucket" in exc_info.value.hint


# --- head_archive --------------------------------------------------------------


def test_head_archive_defaults_absent_storage_class_to_standard():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response(
        "head_object",
        {"ContentLength": 42, "LastModified": _dt(4)},
        {"Bucket": "b", "Key": "k.dmg"},
    )
    with stubber:
        archive = aws.head_archive(FakeSession(s3=s3), bucket="b", key="k.dmg", region="us-west-2")
    assert archive.storage_class == "STANDARD"
    assert archive.is_downloadable is True
    assert archive.size_bytes == 42


def test_head_archive_missing_object_raises_download_error():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    with stubber, pytest.raises(AwsDownloadError) as exc_info:
        aws.head_archive(FakeSession(s3=s3), bucket="b", key="nope.dmg", region="us-west-2")
    assert "list" in exc_info.value.hint


# --- restore_archive -----------------------------------------------------------


def test_restore_archive_requests_and_reports_requested():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_response(
        "restore_object",
        {},
        {
            "Bucket": "b",
            "Key": "k.dmg",
            "RestoreRequest": {"Days": 7, "GlacierJobParameters": {"Tier": "Standard"}},
        },
    )
    with stubber:
        outcome = aws.restore_archive(
            FakeSession(s3=s3), bucket="b", key="k.dmg", region="us-west-2", days=7, tier="Standard"
        )
    assert outcome == "requested"


def test_restore_archive_already_in_progress_is_not_an_error():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_client_error("restore_object", service_error_code="RestoreAlreadyInProgress")
    with stubber:
        outcome = aws.restore_archive(
            FakeSession(s3=s3), bucket="b", key="k.dmg", region="us-west-2", days=7, tier="Bulk"
        )
    assert outcome == "already_in_progress"


# --- download_bytes ------------------------------------------------------------


def test_download_bytes_returns_none_when_sidecar_absent():
    s3 = _client("s3")
    stubber = Stubber(s3)
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    with stubber:
        assert (
            aws.download_bytes(FakeSession(s3=s3), bucket="b", key="k.sha256", region="us-west-2")
            is None
        )
