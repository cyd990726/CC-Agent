"""Provider-neutral web search and safe URL fetching tools."""

from __future__ import annotations

import html
import ipaddress
import json
import os
import socket
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)

from tools.base import Tool, ToolError, require_string


USER_AGENT = "Mini-Agent/0.1 (+https://github.com/mini-agent)"
DEFAULT_TIMEOUT = 15.0
MAX_SEARCH_RESULTS = 10
MAX_FETCH_CHARS = 100_000
MAX_FETCH_BYTES = 1_000_000


def _bounded_integer(
    value: Any,
    name: str,
    *,
    default: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ToolError(f"{name} must be a positive integer")
    if value > maximum:
        raise ToolError(f"{name} must not exceed {maximum}")
    return value


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider(ABC):
    """Backend used by :class:`WebSearchTool`."""

    name: str

    @abstractmethod
    def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Return normalized web search results."""


OpenUrl = Callable[..., Any]


def _read_json_response(
    request: Request,
    *,
    opener: OpenUrl,
    timeout: float,
) -> Mapping[str, Any]:
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_FETCH_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise ToolError(f"search provider returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"search provider request failed: {exc}") from exc
    if len(raw) > MAX_FETCH_BYTES:
        raise ToolError("search provider response is too large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("search provider returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ToolError("search provider returned an unexpected response")
    return payload


class TavilySearchProvider(SearchProvider):
    name = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        opener: OpenUrl = urlopen,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.opener = opener
        self.timeout = timeout

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        body = json.dumps(
            {
                "api_key": self.api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            }
        ).encode("utf-8")
        request = Request(
            "https://api.tavily.com/search",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        payload = _read_json_response(
            request, opener=self.opener, timeout=self.timeout
        )
        return _normalize_results(payload.get("results"), snippet_key="content")


class BraveSearchProvider(SearchProvider):
    name = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        opener: OpenUrl = urlopen,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.opener = opener
        self.timeout = timeout

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        params = urlencode({"q": query, "count": max_results})
        request = Request(
            f"https://api.search.brave.com/res/v1/web/search?{params}",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
                "User-Agent": USER_AGENT,
            },
        )
        payload = _read_json_response(
            request, opener=self.opener, timeout=self.timeout
        )
        web = payload.get("web")
        results = web.get("results") if isinstance(web, Mapping) else None
        return _normalize_results(results, snippet_key="description")


class SerperSearchProvider(SearchProvider):
    name = "serper"

    def __init__(
        self,
        api_key: str,
        *,
        opener: OpenUrl = urlopen,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.opener = opener
        self.timeout = timeout

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        request = Request(
            "https://google.serper.dev/search",
            data=json.dumps({"q": query, "num": max_results}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-API-KEY": self.api_key,
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        payload = _read_json_response(
            request, opener=self.opener, timeout=self.timeout
        )
        return _normalize_results(payload.get("organic"), snippet_key="snippet")


class UnavailableSearchProvider(SearchProvider):
    name = "unavailable"

    def __init__(self, message: str) -> None:
        self.message = message

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        raise ToolError(self.message)


def _normalize_results(value: Any, *, snippet_key: str) -> list[SearchResult]:
    if not isinstance(value, list):
        return []
    results: list[SearchResult] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        title = item.get("title")
        url = item.get("url") or item.get("link")
        snippet = item.get(snippet_key, "")
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        results.append(
            SearchResult(
                title=title.strip(),
                url=url.strip(),
                snippet=snippet.strip() if isinstance(snippet, str) else "",
            )
        )
    return results


def create_search_provider(
    name: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> SearchProvider:
    """Create a provider from configuration, or an informative unavailable one."""

    env = os.environ if environ is None else environ
    configured = (name or env.get("MINI_AGENT_SEARCH_PROVIDER", "auto")).lower()
    providers: dict[str, tuple[type[SearchProvider], str]] = {
        "tavily": (TavilySearchProvider, "TAVILY_API_KEY"),
        "brave": (BraveSearchProvider, "BRAVE_SEARCH_API_KEY"),
        "serper": (SerperSearchProvider, "SERPER_API_KEY"),
    }
    if configured == "auto":
        for provider_name in ("tavily", "brave", "serper"):
            provider_type, key_name = providers[provider_name]
            api_key = env.get(key_name)
            if api_key:
                return provider_type(api_key)
        return UnavailableSearchProvider(
            "web search is not configured; set TAVILY_API_KEY, "
            "BRAVE_SEARCH_API_KEY, or SERPER_API_KEY"
        )
    if configured not in providers:
        supported = ", ".join(["auto", *providers])
        return UnavailableSearchProvider(
            f"unknown search provider {configured!r}; supported: {supported}"
        )
    provider_type, key_name = providers[configured]
    api_key = env.get(key_name)
    if not api_key:
        return UnavailableSearchProvider(
            f"{configured} search requires the {key_name} environment variable"
        )
    return provider_type(api_key)


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the public web for current information and return titles, URLs, "
        "and snippets. Use fetch_url to read a promising result in full."
    )
    args_schema = {
        "query": "string; search query",
        "max_results": "optional positive integer from 1 to 10; defaults to 5",
    }

    def __init__(self, provider: SearchProvider) -> None:
        self.provider = provider

    def run(self, args: Mapping[str, Any]) -> str:
        query = require_string(args, "query")
        max_results = _bounded_integer(
            args.get("max_results"),
            "max_results",
            default=5,
            maximum=MAX_SEARCH_RESULTS,
        )
        results = self.provider.search(query, max_results)[:max_results]
        return json.dumps(
            {
                "provider": self.provider.name,
                "query": query,
                "results": [asdict(result) for result in results],
            },
            ensure_ascii=False,
        )


Resolver = Callable[..., list[Any]]


def _validate_public_url(url: str, *, resolver: Resolver = socket.getaddrinfo) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ToolError(f"invalid URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ToolError("url must use http or https")
    if not parsed.hostname:
        raise ToolError("url must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ToolError("url must not include credentials")
    try:
        addresses = resolver(parsed.hostname, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(f"cannot resolve hostname: {parsed.hostname}") from exc
    if not addresses:
        raise ToolError(f"cannot resolve hostname: {parsed.hostname}")
    for address in addresses:
        ip_text = address[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ToolError(
                f"hostname resolved to an invalid address: {ip_text}"
            ) from exc
        if not ip.is_global:
            raise ToolError("refusing to access a private or non-public address")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], None]) -> None:
        super().__init__()
        self.validator = validator

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        self.validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)

    def result(self) -> tuple[str, str]:
        title = " ".join(" ".join(self.title_parts).split())
        lines = [
            " ".join(line.split())
            for line in "".join(self.text_parts).splitlines()
        ]
        text = "\n".join(line for line in lines if line)
        return html.unescape(title), html.unescape(text)


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = (
        "Fetch a public HTTP(S) URL and extract readable text. Private network "
        "addresses and oversized responses are blocked."
    )
    args_schema = {
        "url": "string; public http or https URL",
        "max_chars": (
            "optional positive integer up to 100000; defaults to 20000"
        ),
    }

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        resolver: Resolver = socket.getaddrinfo,
        opener: OpenUrl | None = None,
    ) -> None:
        self.timeout = timeout
        self.resolver = resolver
        validator = lambda value: _validate_public_url(value, resolver=self.resolver)
        self.opener = opener or build_opener(_SafeRedirectHandler(validator)).open

    def run(self, args: Mapping[str, Any]) -> str:
        url = require_string(args, "url")
        max_chars = _bounded_integer(
            args.get("max_chars"),
            "max_chars",
            default=20_000,
            maximum=MAX_FETCH_CHARS,
        )
        _validate_public_url(url, resolver=self.resolver)
        request = Request(
            url,
            headers={
                "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.1",
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                final_url = response.geturl()
                _validate_public_url(final_url, resolver=self.resolver)
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
                raw = response.read(MAX_FETCH_BYTES + 1)
        except HTTPError as exc:
            raise ToolError(f"fetch returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ToolError(f"fetch failed: {exc}") from exc
        if len(raw) > MAX_FETCH_BYTES:
            raise ToolError(f"response exceeds {MAX_FETCH_BYTES} bytes")
        if not (
            content_type.startswith("text/")
            or content_type in {"application/json", "application/xml"}
        ):
            raise ToolError(f"unsupported content type: {content_type}")
        try:
            decoded = raw.decode(charset, errors="replace")
        except LookupError:
            decoded = raw.decode("utf-8", errors="replace")
        title = ""
        text = decoded
        if content_type == "text/html":
            parser = _HTMLTextExtractor()
            parser.feed(decoded)
            title, text = parser.result()
        truncated = len(text) > max_chars
        return json.dumps(
            {
                "url": final_url,
                "title": title,
                "content_type": content_type,
                "truncated": truncated,
                "content": text[:max_chars],
            },
            ensure_ascii=False,
        )
