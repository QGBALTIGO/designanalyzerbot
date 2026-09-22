import asyncio
import ipaddress
from unittest.mock import patch

import pytest

from app.security import UnsafeUrl, normalize_url, validate_public_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example.com", "https://example.com/"),
        ("HTTPS://Example.COM/path?q=1#frag", "https://example.com/path?q=1"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://münich.example/", "https://xn--mnich-kva.example/"),
    ],
)
def test_normalize_valid(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ftp://example.com",
        "file:///etc/passwd",
        "http://localhost",
        "http://foo.local",
        "http://router.lan",
        "http://user:pass@example.com",
    ],
)
def test_normalize_blocks_bad_targets(raw):
    with pytest.raises(UnsafeUrl):
        normalize_url(raw)


def test_ip_classification_public_and_private():
    from app.security import _is_public
    assert _is_public("8.8.8.8")
    assert _is_public("1.1.1.1")
    for ip in ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "fc00::1", "fe80::1"]:
        assert not _is_public(ip), ip


@pytest.mark.asyncio
async def test_validate_public_url_accepts_public_dns():
    with patch("app.security.socket.getaddrinfo") as mocked:
        mocked.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ]
        validated = await validate_public_url("example.com")
        assert validated.url == "https://example.com/"
        assert validated.addresses == ("93.184.216.34",)


@pytest.mark.asyncio
async def test_validate_public_url_rejects_mixed_public_private_dns():
    with patch("app.security.socket.getaddrinfo") as mocked:
        mocked.return_value = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]
        with pytest.raises(UnsafeUrl):
            await validate_public_url("example.com")
