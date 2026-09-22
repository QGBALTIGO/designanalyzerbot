
from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlsplit

from .security import validate_public_url


class SafeProxyError(RuntimeError):
    pass


class SafeOutboundProxy:
    """Small HTTP/CONNECT proxy that pins every destination to a validated public IP."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port: int | None = None

    async def start(self) -> "SafeOutboundProxy":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sock = self.server.sockets[0]
        self.port = int(sock.getsockname()[1])
        return self

    async def close(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        self.server = None

    async def __aenter__(self) -> "SafeOutboundProxy":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        remote_writer: asyncio.StreamWriter | None = None
        try:
            header_blob = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=15,
            )
            if len(header_blob) > 65536:
                raise SafeProxyError("proxy request headers too large")

            head_text = header_blob.decode("latin-1", errors="replace")
            lines = head_text.split("\r\n")
            if not lines or len(lines[0].split()) != 3:
                raise SafeProxyError("invalid proxy request")

            method, target, version = lines[0].split()
            headers = _parse_headers(lines[1:])

            if method.upper() == "CONNECT":
                host, port = _split_host_port(target, 443)
                validated = await validate_public_url(_validation_url(host, port, True))
                remote_reader, remote_writer = await _open_pinned(
                    validated.addresses,
                    port,
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await _tunnel(reader, writer, remote_reader, remote_writer)
                return

            absolute = target
            if not target.startswith(("http://", "https://")):
                host_header = headers.get("host")
                if not host_header:
                    raise SafeProxyError("missing Host header")
                absolute = "http://" + host_header + target

            parsed = urlsplit(absolute)
            if parsed.scheme != "http":
                raise SafeProxyError("non-CONNECT HTTPS request is not supported")
            host = parsed.hostname or ""
            port = parsed.port or 80
            validated = await validate_public_url(absolute)
            remote_reader, remote_writer = await _open_pinned(
                validated.addresses,
                port,
            )

            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query

            forward_lines = [f"{method} {path} {version}"]
            for key, value in _iter_headers(lines[1:]):
                lower = key.lower()
                if lower in {
                    "proxy-connection",
                    "connection",
                    "keep-alive",
                }:
                    continue
                forward_lines.append(f"{key}: {value}")
            forward_lines.append("Connection: close")
            remote_writer.write(("\r\n".join(forward_lines) + "\r\n\r\n").encode("latin-1"))

            content_length = 0
            try:
                content_length = int(headers.get("content-length", "0") or 0)
            except ValueError:
                content_length = 0
            if content_length > 0:
                if content_length > 16 * 1024 * 1024:
                    raise SafeProxyError("request body too large")
                body = await asyncio.wait_for(
                    reader.readexactly(content_length),
                    timeout=30,
                )
                remote_writer.write(body)

            await remote_writer.drain()
            while True:
                chunk = await remote_reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()

        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:
            with contextlib.suppress(Exception):
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
        finally:
            if remote_writer is not None:
                remote_writer.close()
                with contextlib.suppress(Exception):
                    await remote_writer.wait_closed()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _open_pinned(
    addresses: tuple[str, ...],
    port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    errors: list[Exception] = []
    for ip in addresses:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=12,
            )
        except Exception as exc:
            errors.append(exc)
    raise SafeProxyError(
        f"could not connect to validated public target: {errors[-1] if errors else 'no address'}"
    )


async def _tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
) -> None:
    async def pump(
        source: asyncio.StreamReader,
        destination: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                data = await source.read(65536)
                if not data:
                    break
                destination.write(data)
                await destination.drain()
        except Exception:
            pass

    first = asyncio.create_task(pump(client_reader, remote_writer))
    second = asyncio.create_task(pump(remote_reader, client_writer))
    done, pending = await asyncio.wait(
        {first, second},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.gather(*done, return_exceptions=True)


def _parse_headers(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in _iter_headers(lines):
        result[key.lower()] = value
    return result


def _iter_headers(lines: list[str]):
    for line in lines:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        yield key.strip(), value.strip()


def _split_host_port(value: str, default_port: int) -> tuple[str, int]:
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise SafeProxyError("invalid IPv6 CONNECT target")
        host = value[1:end]
        rest = value[end + 1:]
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        return host, int(port_text)
    return value, default_port


def _validation_url(host: str, port: int, https: bool) -> str:
    scheme = "https" if https else "http"
    wrapped = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default = 443 if https else 80
    suffix = "" if port == default else f":{port}"
    return f"{scheme}://{wrapped}{suffix}/"
