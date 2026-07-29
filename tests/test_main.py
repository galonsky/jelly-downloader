import ipaddress

import pytest

from app.main import is_public_ip, validate_public_url


def test_public_ip_check_rejects_private_and_loopback():
    assert not is_public_ip("127.0.0.1")
    assert not is_public_ip("10.0.0.1")
    assert not is_public_ip("::1")


def test_public_ip_check_accepts_public_ip():
    assert is_public_ip("8.8.8.8")


def test_url_scheme_is_checked():
    with pytest.raises(ValueError, match="HTTP"):
        validate_public_url("file:///tmp/video")
