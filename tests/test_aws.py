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
from archive_dmg.errors import AwsAuthError, AwsBucketError, AwsUploadError


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
