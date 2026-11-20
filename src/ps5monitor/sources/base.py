"""Shared HTTP fetching (timeouts, retries with jitter, conditional requests) and schema.org helpers."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ps5monitor.models import Condition, Stock

log = logging.getLogger(__name__)


class SourceError(Exception):
    """A source could not be read. Never interpreted as 'no stock'."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Page:
    url: str
    status: int
    text: str
    not_modified: bool = False


class Fetcher:
    def __init__(self, client: httpx.Client, cache: dict[str, str] | None = None, max_retries: int = 2,
                 sleep: Any = time.sleep) -> None:
        self.client = client
        self.cache = cache if cache is not None else {}  # url -> "etag|last-modified"
        self.max_retries = max_retries
        self.sleep = sleep
        self.requests = 0

    def get(self, url: str, *, conditional: bool = False) -> Page:
        headers: dict[str, str] = {}
        if conditional and url in self.cache:
            etag, _, lm = self.cache[url].partition("|")
            if etag:
                headers["If-None-Match"] = etag
            if lm:
                headers["If-Modified-Since"] = lm
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.sleep(min(8.0, 1.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.75))
            try:
                self.requests += 1
                r = self.client.get(url, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = SourceError(f"red/timeout: {type(exc).__name__}")
                continue
            if r.status_code == 304:
                return Page(url, 304, "", not_modified=True)
            if r.status_code >= 500:
                last_exc = SourceError(f"HTTP {r.status_code}", r.status_code)
                continue
            if r.status_code in (403, 429, 503) or r.status_code >= 400:
                # Access denied / rate limited / anti-bot challenge: do not retry or try to bypass.
                hint = " (posible protección anti-bot)" if "just a moment" in r.text[:3000].lower() else ""
                raise SourceError(f"HTTP {r.status_code}{hint}", r.status_code)
            if conditional:
                etag, lm = r.headers.get("etag", ""), r.headers.get("last-modified", "")
                if etag or lm:
                    self.cache[url] = f"{etag}|{lm}"
            return Page(url, r.status_code, r.text)
        assert last_exc is not None
        raise last_exc


def make_client(user_agent: str, timeout_s: float, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept-Language": "es-ES,es;q=0.9",
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        follow_redirects=True,
        transport=transport,
    )


_LD_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def jsonld_blocks(html: str) -> list[Any]:
    out: list[Any] = []
    for raw in _LD_RE.findall(html):
        try:
            out.append(json.loads(raw.strip()))
        except json.JSONDecodeError:
            continue
    return out


def iter_typed(data: Any, wanted: str) -> list[dict[str, Any]]:
    """Find schema.org nodes of a given @type, descending into lists, @graph and BuyAction.object."""
    found: list[dict[str, Any]] = []
    stack = [data]
    while stack:
        node = stack.pop(0)  # breadth-first, preserves document order
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            t = node.get("@type")
            if t == wanted or (isinstance(t, list) and wanted in t):
                found.append(node)
            for key in ("@graph", "object", "itemListElement", "item", "mainEntity", "hasPart"):
                if key in node:
                    stack.append(node[key])
    return found


def as_list(x: Any) -> list[Any]:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def schema_stock(value: str | None) -> Stock:
    v = (value or "").rsplit("/", 1)[-1].lower()
    if v in ("instock", "limitedavailability", "onlineonly", "instoreonly"):
        return Stock.IN_STOCK if v != "instoreonly" else Stock.UNAVAILABLE
    if v in ("preorder", "presale", "backorder"):
        return Stock.PREORDER
    if v in ("outofstock", "soldout"):
        return Stock.OUT_OF_STOCK
    if v in ("discontinued",):
        return Stock.UNAVAILABLE
    return Stock.UNKNOWN


def schema_condition(value: str | None) -> Condition:
    v = (value or "").rsplit("/", 1)[-1].lower()
    return {"newcondition": Condition.NEW, "usedcondition": Condition.USED,
            "refurbishedcondition": Condition.REFURBISHED, "damagedcondition": Condition.USED}.get(
        v, Condition.UNKNOWN)


def strip_tags(html: str) -> str:
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&euro;", "€")
            .replace("&#8364;", "€"))
    return re.sub(r"\s+", " ", text).strip()
