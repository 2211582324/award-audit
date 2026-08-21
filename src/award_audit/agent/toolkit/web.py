"""Safe web-page fetching, attachment discovery and evidence download."""

from __future__ import annotations

import hashlib
import re
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from pydantic import BaseModel, Field

from award_audit.agent.toolkit.safety import (
    UnsafeUrlError,
    inspect_evidence_file,
    validate_public_url,
)
from award_audit.core.pipeline.checks.l5_precheck import BROWSER_HEADERS

EXCEL_EXTS = (".xlsx", ".xls")
OTHER_DOC_EXTS = (".pdf", ".doc", ".docx", ".zip", ".rar")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# Long public rosters commonly exceed 30k characters. Keep a hard bound, but
# large enough for several hundred table rows before the completeness gate.
MAX_TEXT_CHARS = 120_000
MAX_DOWNLOAD_MB = 20
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024
MAX_REDIRECTS = 5

_TAG_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_A = re.compile(r"<a\s[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                    re.DOTALL | re.IGNORECASE)
_TAG_IMG = re.compile(r"<img\s[^>]*?src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_TAG_BASE = re.compile(r"<base\s[^>]*?href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_TAG_META = re.compile(r"<meta\s[^>]*>", re.IGNORECASE)
_ATTR_CONTENT = re.compile(r"\bcontent=[\"']([^\"']+)[\"']", re.IGNORECASE)
_TAG_ANY = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_ICON_HINT = re.compile(
    r"logo|lgo|icon|banner|btn|bg[_.-]|sprite|share|arrow|phone|"
    r"(?:^|[/_.-])(?:ico|bt)\d*(?:[/_.-]|$)|ewm|"
    r"(?:^|[/_.-])(?:qr|qrcode|code)(?:[/_.-]|$)|gzh|sph|bilibili|xhs|"
    r"/images/red\.png(?:$|\?)|"
    r"/ad/|post_ad|/e_images/|platform2025/images/(?:46|51|out3)\.",
    re.IGNORECASE,
)
_CD_FILENAME = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", re.IGNORECASE)
_TAG_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([a-zA-Z0-9._-]+)", re.IGNORECASE)
_YEAR_RE = re.compile(r"20[1-3][0-9]")
_REDIRECT_CODES = {301, 302, 303, 307, 308}


class Attachment(BaseModel):
    text: str
    url: str
    is_excel: bool


class RelatedPage(BaseModel):
    text: str
    url: str


class PageContent(BaseModel):
    url: str
    status: int
    text: str
    text_truncated: bool = False
    original_text_chars: int = 0
    title: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    related_pages: list[RelatedPage] = Field(default_factory=list)


def _disposition_ext(content_disposition: str) -> str:
    match = _CD_FILENAME.search(content_disposition or "")
    if not match:
        return ""
    return Path(unquote(match.group(1).strip())).suffix.lower()


def extract_years(text: str) -> set[str]:
    return {year for year in _YEAR_RE.findall(text or "") if 2015 <= int(year) <= 2035}


def _page_title(html: str) -> str:
    match = _TAG_TITLE.search(html or "")
    if not match:
        return ""
    return _WS.sub(" ", unescape(match.group(1)).replace("\n", " ")).strip()


def _decode_html_response(response: object) -> str:
    """Decode HTML using its meta/header charset before falling back conservatively."""

    raw = bytes(getattr(response, "content", b""))
    if not raw:
        return str(getattr(response, "text", ""))
    head = raw[:8192].decode("latin-1", errors="ignore")
    headers = getattr(response, "headers", {})
    content_type = str(headers.get("content-type", ""))
    candidates: list[str] = []
    for source in (head, content_type):
        match = _CHARSET.search(source)
        if match:
            encoding = match.group(1).lower()
            if encoding in {"gb2312", "gbk", "x-gbk"}:
                encoding = "gb18030"
            if encoding not in candidates:
                candidates.append(encoding)
    candidates.extend(encoding for encoding in ("utf-8", "gb18030") if encoding not in candidates)
    for encoding in candidates:
        try:
            return raw.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return str(getattr(response, "text", ""))


def _is_safe_discovered_url(url: str) -> bool:
    try:
        validate_public_url(url, resolve_dns=False)
    except UnsafeUrlError:
        return False
    return True


def _parse_html_content(
    html: str,
    base_url: str,
) -> tuple[str, list[Attachment], list[str], int]:
    """Extract bounded visible text, assets, and the pre-truncation text size."""

    cleaned = _TAG_SCRIPT.sub(" ", html)
    match = _TAG_BASE.search(html)
    effective_base = urljoin(base_url, unescape(match.group(1).strip())) if match else base_url
    attachments: list[Attachment] = []
    attachment_urls: set[str] = set()

    def attachment_traits(absolute: str, text: str = "") -> tuple[bool, bool, bool]:
        decoded = unquote(absolute).lower()
        low = decoded.split("?", 1)[0]
        text_low = text.lower().split("?", 1)[0]
        is_excel = low.endswith(EXCEL_EXTS) or text_low.endswith(EXCEL_EXTS)
        is_document = (
            low.endswith(OTHER_DOC_EXTS)
            or text_low.endswith(OTHER_DOC_EXTS)
            or any(ext in decoded for ext in (*EXCEL_EXTS, *OTHER_DOC_EXTS))
        )
        is_download_endpoint = any(
            hint in decoded
            for hint in (
                "downloadattachurl",
                "/download.jsp",
                "/download?",
                "/downfile.do",
                "/downfile.jsp",
            )
        )
        return is_excel, is_document, is_download_endpoint

    for href, inner in _TAG_A.findall(cleaned):
        text = _WS.sub(" ", _TAG_ANY.sub("", unescape(inner))).strip()
        absolute = urljoin(effective_base, unescape(href.strip()))
        low = absolute.lower().split("?", 1)[0]
        is_excel, is_document, is_download_endpoint = attachment_traits(absolute, text)
        looks_like_page = any(
            hint in low for hint in ("/detail/", "/list/", ".html", ".htm")
        )
        if (
            is_excel
            or is_document
            or is_download_endpoint
            or ("附件" in text and not looks_like_page)
        ) and (
            _is_safe_discovered_url(absolute)
        ):
            if absolute not in attachment_urls:
                attachments.append(Attachment(text=text[:80], url=absolute, is_excel=is_excel))
                attachment_urls.add(absolute)
    for tag in _TAG_META.findall(cleaned):
        content = _ATTR_CONTENT.search(tag)
        if content is None:
            continue
        absolute = urljoin(effective_base, unescape(content.group(1).strip()))
        is_excel, is_document, is_download_endpoint = attachment_traits(absolute)
        if (
            (is_excel or is_document or is_download_endpoint)
            and absolute not in attachment_urls
            and _is_safe_discovered_url(absolute)
        ):
            attachments.append(Attachment(text="meta attachment", url=absolute, is_excel=is_excel))
            attachment_urls.add(absolute)
    images: list[str] = []
    for source in _TAG_IMG.findall(cleaned):
        absolute = urljoin(effective_base, unescape(source.strip()))
        low = absolute.lower().split("?", 1)[0]
        if (low.endswith(IMAGE_EXTS) and not _ICON_HINT.search(absolute)
                and _is_safe_discovered_url(absolute) and absolute not in images):
            images.append(absolute)
    body = unescape(_TAG_ANY.sub(" ", cleaned))
    body = "\n".join(line.strip() for line in _WS.sub(" ", body).splitlines() if line.strip())
    return body[:MAX_TEXT_CHARS], attachments, images, len(body)


def parse_html(html: str, base_url: str) -> tuple[str, list[Attachment], list[str]]:
    """Extract bounded visible text and discover document/image links."""

    text, attachments, images, _original_text_chars = _parse_html_content(html, base_url)
    return text, attachments, images


def discover_related_pages(html: str, base_url: str) -> list[RelatedPage]:
    """Keep bounded same-site detail links separate from downloadable evidence."""

    cleaned = _TAG_SCRIPT.sub(" ", html)
    match = _TAG_BASE.search(html)
    effective_base = urljoin(base_url, unescape(match.group(1).strip())) if match else base_url
    base_host = urlsplit(effective_base).hostname
    related: list[RelatedPage] = []
    seen: set[str] = set()
    for href, inner in _TAG_A.findall(cleaned):
        absolute = urljoin(effective_base, unescape(href.strip()))
        parsed = urlsplit(absolute)
        if (
            parsed.hostname != base_host
            or "/detail/" not in parsed.path.lower()
            or absolute in seen
            or not _is_safe_discovered_url(absolute)
        ):
            continue
        text = _WS.sub(" ", _TAG_ANY.sub("", unescape(inner))).strip()
        related.append(RelatedPage(text=text[:200], url=absolute))
        seen.add(absolute)
        if len(related) >= 20:
            break
    return related


def _redirect_target(current_url: str, status: int, location: str | None) -> str | None:
    if status not in _REDIRECT_CODES or not location:
        return None
    return urljoin(current_url, location)


def fetch_page(url: str, timeout: float = 15.0) -> PageContent:
    """Fetch one public page, validating every redirect before following it."""

    import httpx

    current = url
    with httpx.Client(timeout=timeout, follow_redirects=False, headers=BROWSER_HEADERS,
                      trust_env=False) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            validate_public_url(current)
            response = client.get(current)
            target = _redirect_target(
                current, response.status_code, response.headers.get("location")
            )
            if target is not None:
                current = target
                continue
            if response.status_code != 200:
                return PageContent(url=current, status=response.status_code, text="")
            html = _decode_html_response(response)
            text, attachments, images, original_text_chars = _parse_html_content(
                html, str(response.url)
            )
            related_pages = discover_related_pages(html, str(response.url))
            return PageContent(url=str(response.url), status=response.status_code, text=text,
                               text_truncated=original_text_chars > len(text),
                               original_text_chars=original_text_chars,
                               title=_page_title(html), attachments=attachments,
                               images=images, related_pages=related_pages)
    raise RuntimeError(f"页面重定向超过 {MAX_REDIRECTS} 次: {url}")


def download_file(
    url: str,
    dest_dir: Path,
    timeout: float = 20.0,
    excel_only: bool = False,
    referer: str = "",
    *,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> Path:
    """Stream a public evidence file through size, redirect and magic checks."""

    import httpx

    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = dict(BROWSER_HEADERS)
    if referer:
        validate_public_url(referer)
        headers["Referer"] = referer
    stem = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    current = url
    part_path: Path | None = None
    destination: Path | None = None
    destination_existed = False
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers,
                          trust_env=False) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                validate_public_url(current)
                with client.stream("GET", current) as response:
                    target = _redirect_target(
                        current, response.status_code, response.headers.get("location")
                    )
                    if target is not None:
                        current = target
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(f"附件下载失败 HTTP {response.status_code}: {current}")
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise RuntimeError(f"附件超过 {max_bytes} 字节上限: {current}")
                    tail = current.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
                    extension = (_disposition_ext(response.headers.get("content-disposition", ""))
                                 or Path(tail).suffix or (".xlsx" if excel_only else ".bin"))
                    destination = dest_dir / (stem + extension)
                    destination_existed = destination.exists()
                    # Keep the evidence suffix on the temporary file so magic/extension
                    # validation sees .xlsx/.pdf/.jpg rather than the staging marker .part.
                    part_path = destination.with_name(
                        destination.stem + ".part" + destination.suffix
                    )
                    size = 0
                    with part_path.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise RuntimeError(f"附件超过 {max_bytes} 字节上限: {current}")
                            handle.write(chunk)
                    allowed = {"xlsx", "xls"} if excel_only else None
                    inspect_evidence_file(part_path, max_bytes=max_bytes, allowed_kinds=allowed)
                    part_path.replace(destination)
                    inspect_evidence_file(destination, max_bytes=max_bytes, allowed_kinds=allowed)
                    return destination
            raise RuntimeError(f"附件重定向超过 {MAX_REDIRECTS} 次: {url}")
    except Exception:
        if part_path is not None:
            part_path.unlink(missing_ok=True)
        if destination is not None and not destination_existed:
            destination.unlink(missing_ok=True)
        raise
