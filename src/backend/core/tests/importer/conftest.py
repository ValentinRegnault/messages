"""Shared fixtures for importer tests."""

from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _mock_ssrf_dns():
    """Short-circuit SSRF hostname validation for IMAP tests.

    The IMAP import path validates the server hostname via
    ``core.services.ssrf.validate_hostname``. Test fixtures use unresolvable
    hostnames like ``imap.example.com``, so we bypass validation to let tests
    reach the mocked IMAP code. Patching ``validate_hostname`` at its call
    site (rather than ``socket.getaddrinfo``) keeps the mock scoped to SSRF
    checks and avoids redirecting unrelated DNS lookups (e.g. to the S3 test
    fixture backend).
    """
    with mock.patch(
        "core.services.importer.imap.validate_hostname",
        return_value=["93.184.216.34"],
    ):
        yield
