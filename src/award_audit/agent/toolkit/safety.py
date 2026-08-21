"""Hard safety checks shared by network and local-file tools."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_URL_CHARS = 2048
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_RATIO = 200


class SafetyError(ValueError):
    """Base class for input rejected at a trust boundary."""

    code = "SAFETY_REJECTED"


class UnsafeUrlError(SafetyError):
    code = "UNSAFE_URL"


class UnsafePathError(SafetyError):
    code = "UNSAFE_PATH"


class UnsafeFileError(SafetyError):
    code = "UNSAFE_FILE"


@dataclass(frozen=True)
class FileInspection:
    kind: str
    content_type: str
    extension: str
    size_bytes: int
    sha256: str


def _is_forbidden_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    return not ip.is_global


def validate_public_url(
    url: str,
    *,
    resolve_dns: bool = True,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> str:
    """Allow only public HTTP(S) endpoints, including DNS rebinding checks."""

    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise UnsafeUrlError("URL is empty or too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise UnsafeUrlError("URL contains control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL authority or port is invalid") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("only http/https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL requires a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("userinfo in URLs is not allowed")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrlError("local hostnames are not allowed")
    try:
        ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        is_ip = False
    else:
        is_ip = True
        if _is_forbidden_ip(hostname):
            raise UnsafeUrlError("non-public IP addresses are not allowed")
    if resolve_dns and not is_ip:
        try:
            resolved = resolver(hostname, port or 443, type=socket.SOCK_STREAM)
            addresses = {item[4][0] for item in resolved}
        except OSError as exc:
            raise UnsafeUrlError(f"hostname cannot be resolved: {hostname}") from exc
        if not addresses:
            raise UnsafeUrlError(f"hostname has no address: {hostname}")
        if any(_is_forbidden_ip(address) for address in addresses):
            raise UnsafeUrlError("hostname resolves to a non-public IP address")
    return url


def validate_local_path(
    path: str | Path,
    allowed_roots: Iterable[str | Path],
    *,
    must_exist: bool = True,
    file_only: bool = False,
) -> Path:
    """Resolve a path and require it to remain under an explicit allowed root."""

    raw = Path(path)
    if raw.name.lower() == ".env":
        raise UnsafePathError("environment files are never valid tool inputs")
    try:
        resolved = raw.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise UnsafePathError(f"path cannot be resolved: {raw}") from exc
    roots = [Path(root).resolve(strict=False) for root in allowed_roots]
    if resolved.name.lower() == ".env":
        raise UnsafePathError("environment files are never valid tool inputs")
    if not roots or not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise UnsafePathError("path is outside allowed roots")
    if must_exist and file_only and not resolved.is_file():
        raise UnsafePathError("path is not a regular file")
    return resolved


def _inspect_zip(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise UnsafeFileError("archive has too many members")
            total = sum(info.file_size for info in infos)
            compressed = sum(info.compress_size for info in infos)
            if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise UnsafeFileError("archive expands beyond the safety limit")
            if compressed and total / compressed > MAX_ZIP_RATIO:
                raise UnsafeFileError("archive compression ratio is unsafe")
            if any(info.flag_bits & 0x1 for info in infos):
                raise UnsafeFileError("encrypted archives are not allowed")
            names = {info.filename.replace("\\", "/") for info in infos}
    except zipfile.BadZipFile as exc:
        raise UnsafeFileError("invalid ZIP container") from exc
    if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
        return "xlsx"
    raise UnsafeFileError("generic ZIP archives are not accepted as evidence")


def _detect_kind(path: Path, head: bytes) -> str:
    if head.startswith(b"MZ") or head.startswith(b"\x7fELF"):
        raise UnsafeFileError("executable files are not allowed")
    if head.startswith(b"PK\x03\x04"):
        return _inspect_zip(path)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    raise UnsafeFileError("file magic is not an allowed evidence type")


_TYPE_INFO = {
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "xls": ("application/vnd.ms-excel", ".xls"),
    "pdf": ("application/pdf", ".pdf"),
    "png": ("image/png", ".png"),
    "jpeg": ("image/jpeg", ".jpg"),
    "gif": ("image/gif", ".gif"),
    "webp": ("image/webp", ".webp"),
}
_KNOWN_SUFFIXES = {".xlsx": "xlsx", ".xls": "xls", ".pdf": "pdf", ".png": "png",
                   ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}


def inspect_evidence_file(
    path: str | Path,
    *,
    max_bytes: int,
    allowed_kinds: set[str] | None = None,
) -> FileInspection:
    """Check size, magic, archive structure, extension and SHA-256."""

    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise UnsafeFileError("file is unavailable") from exc
    if size <= 0:
        raise UnsafeFileError("empty files are not valid evidence")
    if size > max_bytes:
        raise UnsafeFileError(f"file exceeds {max_bytes} byte limit")
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        head = handle.read(16)
        digest.update(head)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    kind = _detect_kind(file_path, head)
    if allowed_kinds is not None and kind not in allowed_kinds:
        raise UnsafeFileError(f"file type {kind} is not allowed here")
    suffix_kind = _KNOWN_SUFFIXES.get(file_path.suffix.lower())
    if suffix_kind is None:
        raise UnsafeFileError("file extension is not an allowed evidence type")
    if suffix_kind != kind:
        raise UnsafeFileError(f"file extension does not match {kind} content")
    content_type, extension = _TYPE_INFO[kind]
    return FileInspection(kind, content_type, extension, size, digest.hexdigest())
