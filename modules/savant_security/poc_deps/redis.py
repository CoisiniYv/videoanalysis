"""Minimal Redis client shim for POC containers.

This provides just enough of the redis-py surface used by the POC exporters:

- ``Redis.from_url(...)``
- ``Redis.xadd(...)``

It avoids an online ``pip install redis`` step inside the container while still
writing to a real Redis server.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse


class RedisError(RuntimeError):
    pass


@dataclass
class Redis:
    host: str
    port: int = 6379
    db: int = 0
    socket_timeout: float | None = 5.0
    socket_connect_timeout: float | None = None
    decode_responses: bool = False

    @classmethod
    def from_url(cls, url: str, decode_responses: bool = False, **kwargs: Any) -> "Redis":
        parsed = urlparse(url)
        if parsed.scheme not in {"redis", "rediss"}:
            raise RedisError(f"unsupported redis URL scheme: {parsed.scheme}")
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
        path = parsed.path.lstrip("/")
        db = int(path) if path else 0
        return cls(host=host, port=port, db=db, decode_responses=decode_responses, **kwargs)

    def xadd(
        self,
        stream: str,
        fields: dict[str, Any],
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> str | bytes | None:
        parts: list[bytes] = [b"XADD", self._encode_str(stream)]
        if maxlen is not None:
            parts.append(b"MAXLEN")
            if approximate:
                parts.append(b"~")
            parts.append(self._encode_str(str(int(maxlen))))
        parts.append(b"*")
        for key, value in fields.items():
            parts.append(self._encode_str(str(key)))
            parts.append(self._encode_str(self._stringify(value)))
        return self._request(parts)

    def _request(self, parts: Iterable[bytes]) -> str | bytes | list[Any] | None:
        payload = self._encode_resp_array(list(parts))
        connect_timeout = (
            self.socket_connect_timeout
            if self.socket_connect_timeout is not None
            else self.socket_timeout
        )
        with socket.create_connection((self.host, self.port), timeout=connect_timeout) as sock:
            if self.socket_timeout is not None:
                sock.settimeout(self.socket_timeout)
            sock.sendall(payload)
            reply = self._read_reply(sock)
        return self._decode_response(reply)

    def _encode_resp_array(self, parts: list[bytes]) -> bytes:
        chunks = [f"*{len(parts)}\r\n".encode("ascii")]
        for part in parts:
            chunks.append(f"${len(part)}\r\n".encode("ascii"))
            chunks.append(part)
            chunks.append(b"\r\n")
        return b"".join(chunks)

    def _read_reply(self, sock: socket.socket) -> Any:
        file = sock.makefile("rb")
        prefix = file.read(1)
        if not prefix:
            raise RedisError("empty reply from Redis")
        line = file.readline().rstrip(b"\r\n")
        if prefix == b"+":
            return line
        if prefix == b"-":
            raise RedisError(line.decode("utf-8", errors="replace"))
        if prefix == b":":
            return int(line)
        if prefix == b"$":
            length = int(line)
            if length == -1:
                return None
            data = file.read(length)
            file.read(2)
            return data
        if prefix == b"*":
            count = int(line)
            if count == -1:
                return None
            return [self._read_reply(sock) for _ in range(count)]
        raise RedisError(f"unsupported Redis reply prefix: {prefix!r}")

    def _decode_response(self, reply: Any) -> str | bytes | list[Any] | None:
        if reply is None:
            return None
        if isinstance(reply, bytes):
            return reply.decode("utf-8", errors="replace") if self.decode_responses else reply
        if isinstance(reply, list):
            return [
                item.decode("utf-8", errors="replace") if self.decode_responses and isinstance(item, bytes) else item
                for item in reply
            ]
        return str(reply) if self.decode_responses else reply

    def _encode_str(self, value: str) -> bytes:
        return value.encode("utf-8")

    def _stringify(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)
