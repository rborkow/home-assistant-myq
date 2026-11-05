"""TLS CXNet framing used by battery-powered Video Keypad relay sessions.

Ported from the independently hardware-validated myQ Keypad Bridge.
No account storage, APK, Android runtime, or device-specific configuration.
"""

from __future__ import annotations

import re
import socket
import ssl
import struct
import uuid
from dataclasses import dataclass
from typing import Protocol, cast


class ReadableSocket(Protocol):
    def recv(self, size: int) -> bytes: ...


HEADER = struct.Struct("<BBHHBIHHH2sQ")
MAX_DATA = 1_024_000


class HubRedirect(Exception):
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        super().__init__("Authenticated hub redirect received")


def properties_encode(values: dict[str, str]) -> bytes:
    def escape(value: str) -> str:
        result = ""
        for c in str(value):
            if c in "\\:= #!":
                result += "\\" + c
            elif c in "\n\r\t\f":
                result += {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\f": "\\f"}[c]
            elif not 32 <= ord(c) < 127:
                result += "".join(
                    f"\\u{int.from_bytes(b, 'big'):04x}"
                    for b in [
                        c.encode("utf-16-be")[i : i + 2]
                        for i in range(0, len(c.encode("utf-16-be")), 2)
                    ]
                )
            else:
                result += c
        return result

    # Properties.store() always emits a leading comment, including for options.
    return (
        "#CXNet properties\n" + "".join(f"{escape(k)}={escape(v)}\n" for k, v in values.items())
    ).encode("ascii")


def properties_decode(data: bytes) -> dict[str, str]:
    def unescape(s: str) -> str:
        def convert(m: re.Match[str]) -> str:
            s = m.group(1)
            return (
                chr(int(s[1:], 16))
                if s.startswith("u")
                else {"n": "\n", "r": "\r", "t": "\t", "f": "\f"}.get(s, s)
            )

        return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", convert, s)

    lines, pending = [], ""
    for line in data.decode("latin1").splitlines():
        line = pending + (line.lstrip(" \t\f") if pending else line)
        n = len(line) - len(line.rstrip("\\"))
        if n % 2:
            pending = line[:-1]
            continue
        lines.append(line)
        pending = ""
    if pending:
        lines.append(pending)
    result = {}
    for line in lines:
        line = line.lstrip(" \t\f")
        if not line or line.startswith(("#", "!")):
            continue
        match = re.match(r"((?:\\.|[^\\:=\s])*)(?:\s*[=:]\s*|\s+)?(.*)", line)
        assert match is not None
        result[unescape(match[1])] = unescape(match[2])
    return result


def read_exact(stream: ReadableSocket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = stream.recv(size - len(result))
        if not chunk:
            raise EOFError("CXNet socket closed")
        result.extend(chunk)
    return bytes(result)


def relay_access_units(data: bytes, expected_alias: str) -> list[bytes]:
    """Mode-0 relay framing: BE outer lengths, LE access-unit lengths."""
    if len(data) < 14 or data[0] != 5:
        raise ValueError("Invalid relay media header")
    source_size = struct.unpack_from(">I", data, 2)[0]
    offset = 6 + source_size
    if offset + 8 > len(data):
        raise ValueError("Truncated relay source/header")
    source = data[6:offset].decode("utf-8")
    data_size = struct.unpack_from(">I", data, offset)[0]
    if data_size < 4 or offset + 4 + data_size != len(data):
        raise ValueError("Invalid relay data length")
    codec = data[offset + 4] & 31
    if source.casefold() != expected_alias.casefold():
        raise ValueError("Relay source does not match requested camera")
    if codec != 3:
        raise ValueError("Relay codec is not H.264")
    offset += 8
    units = []
    while offset < len(data):
        if offset + 4 > len(data):
            raise ValueError("Truncated relay access-unit length")
        size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if size <= 0 or offset + size > len(data):
            raise ValueError("Invalid relay access-unit length")
        units.append(data[offset : offset + size])
        offset += size
    return units


@dataclass
class Packet:
    action: int
    data: bytes = b""
    options: bytes = b""
    sender: bytes = bytes(16)
    recipient: int = 0
    service: int = 121
    crypto: int = 2
    direction: int = 0
    version: int = 3
    server: bytes = b"**"

    def encode(self) -> bytes:
        header_size = HEADER.size + len(self.sender) + len(self.options)
        if (
            not 16 <= len(self.sender) <= 32767
            or len(self.sender) % 16
            or header_size > 32767
            or len(self.data) > MAX_DATA
        ):
            raise ValueError("CXNet frame exceeds protocol bounds")
        return (
            HEADER.pack(
                65,
                self.version,
                header_size,
                len(self.sender),
                self.direction,
                len(self.data),
                self.service,
                self.action,
                self.crypto,
                self.server,
                self.recipient,
            )
            + self.sender
            + self.options
            + self.data
        )

    @classmethod
    def read(cls, stream: ReadableSocket) -> Packet:
        (
            magic,
            version,
            header_size,
            sender_size,
            direction,
            data_size,
            service,
            action,
            crypto,
            server,
            recipient,
        ) = HEADER.unpack(read_exact(stream, HEADER.size))
        options_size = header_size - HEADER.size - sender_size
        if magic != 65 or version not in (1, 2, 3):
            raise ValueError("Invalid CXNet signature/version")
        if (
            not 16 <= sender_size <= 32767
            or sender_size % 16
            or not options_size >= 0
            or header_size > 32767
            or data_size > MAX_DATA
        ):
            raise ValueError("Invalid CXNet lengths")
        if crypto == 1:
            raise ValueError("Legacy IDEA encryption is unsupported; TLS is required")
        sender = read_exact(stream, sender_size)
        options = read_exact(stream, options_size)
        data = read_exact(stream, data_size)
        return cls(
            action, data, options, sender, recipient, service, crypto, direction, version, server
        )


class Session:
    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = 10,
        tls_server_name: str | None = None,
        no_sni: bool = False,
    ) -> None:
        self._socket: ssl.SSLSocket | None = None
        raw = socket.create_connection((host, int(port)), timeout=timeout)
        try:
            context = ssl.create_default_context()
            if no_sni:
                # Some legacy CXNet hubs silently ignore application traffic with SNI.
                # Verify the chain and original hostname before sending credentials.
                context.check_hostname = False
                self._socket = context.wrap_socket(raw)
                expected = (tls_server_name or host).casefold().rstrip(".")
                names = [
                    value.casefold().rstrip(".")
                    for kind, value in cast(
                        tuple[tuple[str, str], ...],
                        (self._socket.getpeercert() or {}).get("subjectAltName", ()),
                    )
                    if kind == "DNS"
                ]
                # These service certificates contain exact DNS SANs. Requiring
                # an exact SAN avoids deprecated match_hostname and never falls
                # back to unverified IP/CN or wildcard matching.
                if expected not in names:
                    raise ssl.SSLCertVerificationError("Service certificate DNS name mismatch")
            else:
                self._socket = context.wrap_socket(raw, server_hostname=tls_server_name or host)
        except BaseException:
            if self._socket is not None:
                self._socket.close()
            raw.close()
            raise
        self.sender = bytes(16)
        self.recipient = 0

    @property
    def socket(self) -> ssl.SSLSocket:
        if self._socket is None:
            raise EOFError("Keypad socket closed")
        return self._socket

    def send(
        self, action: int, *, data: bytes = b"", options: bytes = b"", service: int = 121
    ) -> None:
        self.socket.sendall(
            Packet(action, data, options, self.sender, self.recipient, service=service).encode()
        )

    def receive(self) -> Packet:
        return Packet.read(self.socket)

    def login(
        self,
        fields: dict[str, str | int],
        partner_id: str,
        client_properties: dict[str, str] | None = None,
    ) -> dict[str, str]:
        if not fields.get("m_pwd"):
            raise ValueError("Missing keypad account token")
        # AuthenticationManager.loginWithOAuth2Token() deliberately uses an empty username.
        options = {
            "CX_UNAME": str(fields.get("mUsername") or ""),
            "CX_PASSWD": str(fields["m_pwd"]),
            "os_name": "Android",
            "os_version": "17",
            "partner-id": partner_id,
            "sdk_version": "6.0.10193.3",
            "client_type": "google Pixel 9 Pro",
            "app-id": "com.chamberlain.android.liftmaster.myq",
            "current_version": "5.322.0.1132239",
        }
        if client_properties:
            options.update(client_properties)
        body = {"CX_UNAME": str(fields.get("mUsername") or ""), "mode": "0"}
        if fields.get("m_devId"):
            body["dev-id"] = str(fields["m_devId"])
        # CxnetParser.F() stores login Properties twice into the same buffer.
        self.send(519, data=properties_encode(body) * 2, options=properties_encode(options))
        packet = self.receive()
        if packet.action != 519:
            raise RuntimeError("Keypad broker rejected login")
        properties = properties_decode(packet.data)
        if "1" in properties:
            raise HubRedirect(properties["1"])
        if "SWITCH_DATACENTER" in properties:
            raise RuntimeError("Cross-datacenter redirect is unsupported")
        self.sender, self.recipient = packet.sender, packet.recipient
        self.send(526)
        return properties

    def command(self, command: str, destination: str, correlation: str) -> None:
        options = {"CX_DSTID": destination, "CorrelationID": correlation}
        if "video-" in command:
            options["traceparent"] = f"00-{uuid.UUID(correlation).hex}-{uuid.uuid4().hex[:16]}-01"
        self.send(411, data=command.encode(), options=properties_encode(options))

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


def login_routed(
    fields: dict[str, str | int],
    partner_id: str,
    timeout: float,
    tls_server_name: str,
    no_sni: bool,
    client_properties: dict[str, str] | None = None,
) -> tuple[Session, dict[str, str]]:
    """Follow at most three TLS-authenticated hub redirects; verify every hop."""
    host, port = str(fields["m_serverhost"]), int(fields["mServerSSLPort"])
    visited = set()
    for _ in range(4):
        if (host, port) in visited:
            raise RuntimeError("Hub redirect loop")
        visited.add((host, port))
        session = Session(host, port, timeout, tls_server_name, no_sni)
        try:
            props = session.login(fields, partner_id, client_properties)
            return session, props
        except HubRedirect as redirect:
            session.close()
            # The authenticated original service certificate remains required
            # even when routing supplies an IP address or a different hostname.
            match = re.fullmatch(r"([A-Za-z0-9.-]+):(\d{1,5})", redirect.endpoint)
            if not match or int(match[2]) != 5104:
                raise ValueError("Unsupported hub redirect endpoint") from None
            host, port = match[1], int(match[2])
        except BaseException:
            session.close()
            raise
    raise RuntimeError("Too many hub redirects")
