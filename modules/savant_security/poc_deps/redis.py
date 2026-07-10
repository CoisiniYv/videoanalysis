"""Minimal Redis client shim for POC containers.

This provides just enough of the redis-py surface used by the POC exporters:

- ``Redis.from_url(...)``
- ``Redis.xadd(...)``

It avoids an online ``pip install redis`` step inside the container while still
writing to a real Redis server.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterable
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
    socket_keepalive: bool = False
    single_connection_client: bool = False
    decode_responses: bool = False
    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _reader: BinaryIO | None = field(default=None, init=False, repr=False)

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
            parts.append(self._encode_value(key))
            parts.append(self._encode_value(value))
        return self._request(parts)

    def _request(self, parts: Iterable[bytes]) -> str | bytes | list[Any] | None:
        payload = self._encode_resp_array(list(parts))
        if self.single_connection_client:
            return self._request_persistent(payload)
        return self._request_once(payload)

    def _request_once(self, payload: bytes) -> str | bytes | list[Any] | None:
        with self._connect() as sock:
            with sock.makefile("rb") as reader:
                sock.sendall(payload)
                reply = self._read_reply(reader)
        return self._decode_response(reply)

    def _request_persistent(self, payload: bytes) -> str | bytes | list[Any] | None:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                if self._sock is None or self._reader is None:
                    self._sock = self._connect()
                    self._reader = self._sock.makefile("rb")
                self._sock.sendall(payload)
                reply = self._read_reply(self._reader)
                return self._decode_response(reply)
            except (OSError, socket.timeout, RedisError) as exc:
                last_exc = exc
                self.close()
                if attempt == 0:
                    continue
                break
        if last_exc is not None:
            raise last_exc
        raise RedisError("Redis request failed without an exception")

    def _connect(self) -> socket.socket:
        connect_timeout = (
            self.socket_connect_timeout
            if self.socket_connect_timeout is not None
            else self.socket_timeout
        )
        sock = socket.create_connection((self.host, self.port), timeout=connect_timeout)
        if self.socket_keepalive:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if self.socket_timeout is not None:
            sock.settimeout(self.socket_timeout)
        return sock

    def close(self) -> None:
        reader = self._reader
        sock = self._sock
        self._reader = None
        self._sock = None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _encode_resp_array(self, parts: list[bytes]) -> bytes:
        chunks = [f"*{len(parts)}\r\n".encode("ascii")]
        for part in parts:
            chunks.append(f"${len(part)}\r\n".encode("ascii"))
            chunks.append(part)
            chunks.append(b"\r\n")
        return b"".join(chunks)

    def _read_reply(self, reader: BinaryIO) -> Any:
        prefix = reader.read(1)
        if not prefix:
            raise RedisError("empty reply from Redis")
        line = reader.readline().rstrip(b"\r\n")
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
            data = reader.read(length)
            reader.read(2)
            return data
        if prefix == b"*":
            count = int(line)
            if count == -1:
                return None
            return [self._read_reply(reader) for _ in range(count)]
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

    def _encode_value(self, value: Any) -> bytes:
        """Preserve binary Stream fields such as bounded JPEG face crops."""
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, memoryview):
            return value.tobytes()
        return self._encode_str(self._stringify(value))

    def _stringify(self, value: Any) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)
