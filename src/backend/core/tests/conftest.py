"""Fixtures for tests in the messages core application"""

# pylint: disable=import-outside-toplevel,broad-exception-caught

from unittest import mock

import pytest

USER = "user"
TEAM = "team"
VIA = [USER, TEAM]


@pytest.fixture(scope="session", autouse=True)
def ensure_storage_buckets():
    """
    Ensure all required S3 buckets exist before running tests.

    This is a session-scoped fixture that runs once at the start of the test session.
    It creates any missing buckets in MinIO/S3 that are needed for tests.
    """
    from django.core.files.storage import storages

    buckets_to_ensure = ["message-imports", "message-blobs"]

    for storage_name in buckets_to_ensure:
        if storage_name not in storages.backends:
            continue

        try:
            storage = storages[storage_name]
            # Use boto3 to create bucket if it doesn't exist
            if hasattr(storage, "bucket"):
                client = storage.bucket.meta.client
                bucket_name = storage.bucket.name
                try:
                    client.head_bucket(Bucket=bucket_name)
                except client.exceptions.NoSuchBucket:
                    client.create_bucket(Bucket=bucket_name)
                except Exception:
                    # Bucket exists or other error, continue
                    pass
        except Exception:
            # Storage not configured or other error, skip
            pass


@pytest.fixture
def mock_user_teams():
    """Mock for the "teams" property on the User model."""
    with mock.patch(
        "core.models.User.teams", new_callable=mock.PropertyMock
    ) as mock_teams:
        yield mock_teams


# @pytest.fixture
# @pytest.mark.django_db
# def create_testdomain():
#     """Create the TESTDOMAIN."""
#     from core import models
#     models.MailDomain.objects.get_or_create(
#         name=settings.MESSAGES_TESTDOMAIN,
#         defaults={
#             "oidc_autojoin": True
#         }
#     )
