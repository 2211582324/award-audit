"""Provider-neutral official-award search with bounded third-party data transfer."""

from __future__ import annotations

import os
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from award_audit.agent.toolkit.safety import validate_public_url
from award_audit.core.pipeline.checks.l5_precheck import BROWSER_HEADERS

ANYSEARCH_MCP_URL = "https://api.anysearch.com/mcp"
BING_SEARCH_URL = "https://cn.bing.com/search"
MAX_QUERY_CHARS = 100
MAX_EXTRACT_CHARS = 250_000


class SearchProviderError(RuntimeError):
    code = "SEARCH_PROVIDER_ERROR"


class SearchAuthError(SearchProviderError):
    code = "SEARCH_AUTH_FAILED"


class SearchQuotaError(SearchProviderError):
    code = "SEARCH_QUOTA_EXHAUSTED"


class SearchRateLimitError(SearchProviderError):
    code = "SEARCH_RATE_LIMITED"


class SearchUnavailableError(SearchProviderError):
    code = "SEARCH_PROVIDER_UNAVAILABLE"


class SearchResponseError(SearchProviderError):
    code = "SEARCH_RESPONSE_INVALID"


class SearchHit(BaseModel):
    title: str = Field(default="", max_length=300)
    url: str = Field(min_length=1, max_length=2048)
    snippet: str = Field(default="", max_length=1000)
    rank: int = Field(ge=1, le=100)


class SearchResponse(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    hits: list[SearchHit] = Field(max_length=20)
    request_id: str = Field(default="", max_length=200)
    latency_ms: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=10)


class ExtractResponse(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=1, max_length=2048)
    text: str = Field(max_length=MAX_EXTRACT_CHARS)
    is_truncated: bool = False
    request_id: str = Field(default="", max_length=200)


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse: ...

    def extract(self, url: str, *, query_hint: str = "") -> ExtractResponse: ...


HttpClientFactory = Callable[..., Any]

_MCP_RESULT_HEADING = re.compile(r"^###\s+\d+\.\s*(?P<title>.+?)\s*$")
_MCP_RESULT_URL = re.compile(r"^-\s+\*\*URL\*\*:\s*(?P<url>\S+)\s*$")
_TRUSTED_EQUIVALENT_HOST_GROUPS = (
    frozenset({"edu.cn", "www.edu.cn", "cernet.edu.cn", "www.cernet.edu.cn"}),
)


def _bounded_query(query: str) -> str:
    normalized = " ".join(query.split()).strip()
    if not normalized:
        raise ValueError("search query cannot be empty")
    if len(normalized) > MAX_QUERY_CHARS:
        raise ValueError(f"search query exceeds {MAX_QUERY_CHARS} characters")
    return normalized


def _raise_status(status: int, *, request_id: str = "") -> None:
    suffix = f" (request_id={request_id[:80]})" if request_id else ""
    if status in (401, 403):
        raise SearchAuthError("search provider rejected authentication" + suffix)
    if status == 402:
        raise SearchQuotaError("search provider quota exhausted" + suffix)
    if status == 429:
        raise SearchRateLimitError("search provider rate limit exceeded" + suffix)
    if status >= 500:
        raise SearchUnavailableError(f"search provider returned HTTP {status}" + suffix)
    if status >= 400:
        raise SearchProviderError(f"search provider returned HTTP {status}" + suffix)


class AnySearchProvider:
    """Direct AnySearch integration; never loads dotenv or persists returned credentials."""

    name = "anysearch"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        client_factory: HttpClientFactory | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = os.environ.get("ANYSEARCH_API_KEY", "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(
                timeout=self._timeout_seconds, headers=self._headers(), trust_env=False
            )
        try:
            import httpx
        except ImportError as exc:
            raise SearchUnavailableError(
                "AnySearch requires the optional httpx dependency"
            ) from exc
        return httpx.Client(
            timeout=self._timeout_seconds, headers=self._headers(), trust_env=False
        )

    def _call_mcp(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        request_id: str,
    ) -> tuple[str, str]:
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            with self._client() as client:
                response = client.post(ANYSEARCH_MCP_URL, json=body)
            response_request_id = response.headers.get("x-request-id", "")
            _raise_status(response.status_code, request_id=response_request_id)
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("error"):
                raise SearchResponseError(
                    f"AnySearch {tool_name} returned a JSON-RPC error"
                )
            result = payload.get("result", {})
            content = result.get("content", []) if isinstance(result, dict) else []
            text = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
            if not text.strip():
                raise SearchResponseError(
                    f"AnySearch {tool_name} returned no text content"
                )
            return text, str(payload.get("id", response_request_id))[:200]
        except SearchProviderError:
            raise
        except (ValueError, TypeError) as exc:
            raise SearchResponseError(
                f"AnySearch {tool_name} returned invalid JSON/schema"
            ) from exc
        except Exception as exc:
            raise SearchUnavailableError(
                f"AnySearch {tool_name} request failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _parse_search_text(text: str, *, max_results: int) -> list[SearchHit]:
        parsed: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        snippet_lines: list[str] = []

        def finish_current() -> None:
            nonlocal current, snippet_lines
            if current is not None and current.get("url"):
                current["snippet"] = " ".join(snippet_lines).strip()
                parsed.append(current)
            current = None
            snippet_lines = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            heading = _MCP_RESULT_HEADING.match(line)
            if heading:
                finish_current()
                current = {"title": heading.group("title").strip()}
                continue
            if current is None:
                continue
            url_match = _MCP_RESULT_URL.match(line)
            if url_match:
                current["url"] = url_match.group("url").rstrip(">).,，。")
                continue
            if line.startswith("- "):
                snippet_lines.append(line[2:].strip())
        finish_current()

        return [
            SearchHit(
                title=item.get("title", "")[:300],
                url=item["url"],
                snippet=item.get("snippet", "")[:1000],
                rank=rank,
            )
            for rank, item in enumerate(parsed[:max_results], start=1)
        ]

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        bounded = _bounded_query(query)
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")
        started = time.monotonic()
        text, request_id = self._call_mcp(
            "search",
            {"query": bounded, "max_results": max_results},
            request_id="award-audit-search",
        )
        hits = self._parse_search_text(text, max_results=max_results)
        return SearchResponse(
            provider=self.name,
            query=bounded,
            hits=hits,
            request_id=request_id,
            latency_ms=round((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _same_organization_host(left: str, right: str) -> bool:
        left = left.rstrip(".").lower()
        right = right.rstrip(".").lower()
        if left == right:
            return True
        if any(left in group and right in group for group in _TRUSTED_EQUIVALENT_HOST_GROUPS):
            return True
        left_labels = left.split(".")
        right_labels = right.split(".")
        shared = 0
        for left_label, right_label in zip(
            reversed(left_labels), reversed(right_labels), strict=False
        ):
            if left_label != right_label:
                break
            shared += 1
        return shared >= 3

    def _recovered_url_for_page(
        self,
        safe_url: str,
        *,
        query_hint: str = "",
    ) -> str:
        parsed = urlsplit(safe_url)
        page_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if not parsed.hostname or not page_id:
            return safe_url
        queries: list[tuple[str, int]] = []
        if query_hint.strip():
            queries.append((_bounded_query(query_hint), 8))
        # The original host may be retired while the same public document is
        # indexed under an organization's sibling host. The exact page id is
        # the stable recovery key; host-family and page-id checks below remain
        # mandatory before any indexed content is accepted.
        queries.append((_bounded_query(page_id), 5))
        target_host = parsed.hostname.lower()
        for query, max_results in queries:
            response = self.search(query, max_results=max_results)
            for item in response.hits:
                if item.url.rstrip("/") == safe_url.rstrip("/"):
                    continue
                candidate = urlsplit(item.url)
                candidate_page_id = candidate.path.rstrip("/").rsplit("/", 1)[-1]
                if (
                    candidate.hostname
                    and self._same_organization_host(
                        candidate.hostname.lower(), target_host
                    )
                    and candidate_page_id == page_id
                ):
                    return validate_public_url(item.url, resolve_dns=False)
        return safe_url

    def extract(self, url: str, *, query_hint: str = "") -> ExtractResponse:
        safe_url = validate_public_url(url, resolve_dns=False)
        text, request_id = self._call_mcp(
            "extract",
            {"url": safe_url},
            request_id="award-audit-extract",
        )
        resolved_url = safe_url
        if len(text.strip()) < 1000:
            try:
                recovered_url = self._recovered_url_for_page(
                    safe_url,
                    query_hint=query_hint,
                )
            except SearchProviderError:
                recovered_url = safe_url
            if recovered_url != safe_url:
                try:
                    recovered_text, recovered_request_id = self._call_mcp(
                        "extract",
                        {"url": recovered_url},
                        request_id="award-audit-recovered-extract",
                    )
                except SearchProviderError:
                    recovered_text, recovered_request_id = "", ""
                if len(recovered_text.strip()) > len(text.strip()):
                    text = recovered_text
                    resolved_url = recovered_url
                    request_id = recovered_request_id or request_id
        return ExtractResponse(
            provider=self.name,
            url=resolved_url,
            text=text[:MAX_EXTRACT_CHARS],
            is_truncated=len(text) > MAX_EXTRACT_CHARS,
            request_id=request_id,
        )


class _BingResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str, str]] = []
        self._in_algo = False
        self._in_title = False
        self._in_snippet = False
        self._href = ""
        self._title: list[str] = []
        self._snippet: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "li" and "b_algo" in classes:
            self._in_algo = True
            self._href = ""
            self._title = []
            self._snippet = []
        elif self._in_algo and tag == "a" and not self._href:
            self._href = values.get("href") or ""
            self._in_title = True
        elif self._in_algo and tag == "p":
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_title = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "li" and self._in_algo:
            if self._href:
                self.results.append((
                    " ".join(self._title).strip(),
                    self._href,
                    " ".join(self._snippet).strip(),
                ))
            self._in_algo = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)


class BingHtmlSearchProvider:
    name = "bing"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        client_factory: HttpClientFactory | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(
                timeout=self._timeout_seconds, headers=BROWSER_HEADERS, trust_env=False
            )
        try:
            import httpx
        except ImportError as exc:
            raise SearchUnavailableError(
                "Bing fallback requires the optional httpx dependency"
            ) from exc
        return httpx.Client(
            timeout=self._timeout_seconds,
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            trust_env=False,
        )

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        bounded = _bounded_query(query)
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")
        started = time.monotonic()
        try:
            with self._client() as client:
                response = client.get(
                    BING_SEARCH_URL,
                    params={"q": bounded, "setlang": "zh-CN", "ensearch": "0"},
                )
            _raise_status(response.status_code)
            parser = _BingResultParser()
            parser.feed(response.text)
        except SearchProviderError:
            raise
        except Exception as exc:
            raise SearchUnavailableError(f"Bing search failed: {type(exc).__name__}") from exc
        hits = [
            SearchHit(title=title[:300], url=url, snippet=snippet[:1000], rank=index)
            for index, (title, url, snippet) in enumerate(
                parser.results[:max_results], start=1
            )
        ]
        return SearchResponse(
            provider=self.name,
            query=bounded,
            hits=hits,
            latency_ms=round((time.monotonic() - started) * 1000),
        )

    def extract(self, url: str, *, query_hint: str = "") -> ExtractResponse:
        del query_hint
        from award_audit.agent.toolkit.web import fetch_page

        page = fetch_page(validate_public_url(url, resolve_dns=False), self._timeout_seconds)
        if page.status != 200:
            raise SearchProviderError(f"Bing fallback extract target returned HTTP {page.status}")
        return ExtractResponse(
            provider=self.name,
            url=page.url,
            text=page.text[:MAX_EXTRACT_CHARS],
            is_truncated=len(page.text) > MAX_EXTRACT_CHARS,
        )


class FallbackSearchProvider:
    name = "fallback"

    def __init__(self, primary: SearchProvider, secondary: SearchProvider) -> None:
        self.primary = primary
        self.secondary = secondary

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        try:
            return self.primary.search(query, max_results=max_results)
        except SearchProviderError as primary_error:
            try:
                response = self.secondary.search(query, max_results=max_results)
            except SearchProviderError as secondary_error:
                raise SearchUnavailableError(
                    f"both search providers failed: {primary_error.code}/{secondary_error.code}"
                ) from secondary_error
            response.warnings.append(f"fallback_from:{self.primary.name}:{primary_error.code}")
            return response

    def extract(self, url: str, *, query_hint: str = "") -> ExtractResponse:
        try:
            return self.primary.extract(url, query_hint=query_hint)
        except SearchProviderError as primary_error:
            try:
                return self.secondary.extract(url, query_hint=query_hint)
            except SearchProviderError as secondary_error:
                raise SearchUnavailableError(
                    f"both extract providers failed: {primary_error.code}/{secondary_error.code}"
                ) from secondary_error


class FakeSearchProvider:
    """Network-free Provider fake with queued responses/errors and call capture."""

    name = "fake"

    def __init__(
        self,
        responses: Iterable[SearchResponse | ExtractResponse | SearchProviderError],
    ) -> None:
        self._responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> SearchResponse | ExtractResponse:
        if not self._responses:
            raise SearchUnavailableError("fake search provider exhausted")
        response = self._responses.popleft()
        if isinstance(response, SearchProviderError):
            raise response
        return response

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        self.calls.append({"operation": "search", "query": query, "max_results": max_results})
        response = self._next()
        if not isinstance(response, SearchResponse):
            raise SearchResponseError("fake returned an extract response for search")
        return response

    def extract(self, url: str, *, query_hint: str = "") -> ExtractResponse:
        self.calls.append({"operation": "extract", "url": url, "query_hint": query_hint})
        response = self._next()
        if not isinstance(response, ExtractResponse):
            raise SearchResponseError("fake returned a search response for extract")
        return response
