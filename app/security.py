from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class UnsafeUrl(ValueError):
    pass


_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home",
)


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    hostname: str
    addresses: tuple[str, ...]


def normalize_url(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise UnsafeUrl("Envie um endereço de site válido.")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrl("Apenas links HTTP ou HTTPS são aceitos.")
    if not parsed.hostname:
        raise UnsafeUrl("O link não possui um domínio válido.")
    if parsed.username or parsed.password:
        raise UnsafeUrl("Links contendo usuário ou senha não são aceitos.")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise UnsafeUrl("Domínio inválido.") from exc
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeUrl("Endereços locais ou internos não podem ser analisados.")
    netloc = host
    if parsed.port:
        netloc += f":{parsed.port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def _is_public(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve(host: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrl("Não foi possível resolver o domínio informado.") from exc
    addresses = sorted({item[4][0] for item in infos})
    if not addresses:
        raise UnsafeUrl("O domínio não retornou nenhum endereço de rede.")
    if any(not _is_public(ip) for ip in addresses):
        raise UnsafeUrl("O domínio aponta para uma rede privada, local ou reservada.")
    return tuple(addresses)


async def validate_public_url(raw: str) -> ValidatedUrl:
    normalized = normalize_url(raw)
    parsed = urlsplit(normalized)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await asyncio.to_thread(_resolve, host, port)
    return ValidatedUrl(normalized, host, addresses)
