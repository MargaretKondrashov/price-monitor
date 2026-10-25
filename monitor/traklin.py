"""Collect Traklin's full catalog: product URLs from the site map + category pages,
then read each product page for title, model, price and promotions."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urljoin, urlsplit

from .extract import extract_page, links
from .http import Http
from .models import extract_model, model_from_label

log = logging.getLogger(__name__)

PAGINATION_TEXT = re.compile(r"^(\d{1,3}|הבא|הבאה|>|»|›|next)$", re.I)
NON_PRODUCT = re.compile(
    r"\.aspx(?!\?id=)|login|cart|shopping|contact|צור_קשר|סניפים|article|buying_guide|"
    r"sitemap|cancel_order|תקנון|אודות|privacy|\.(jpg|png|pdf|gif)$", re.I)


@dataclass
class TraklinProduct:
    url: str
    title: str
    model: str | None
    price: float | None
    old_price: float | None = None
    promos: list[str] = field(default_factory=list)
    in_stock: bool | None = None
    category: str = ""

    @property
    def discount_pct(self):
        if self.price and self.old_price and self.old_price > self.price * 1.01:
            return round((1 - self.price / self.old_price) * 100, 1)
        return None


def _canon(url: str) -> str:
    """Canonical form so encoded/decoded variants of the same URL de-duplicate."""
    p = urlsplit(url)
    path = unquote(p.path).rstrip("/")
    q = ("?" + p.query) if p.query else ""
    return f"{p.scheme}://{p.netloc.lower()}{path}{q}"


def _encode(url: str) -> str:
    p = urlsplit(url)
    path = quote(unquote(p.path), safe="/_-.~%")
    q = ("?" + p.query) if p.query else ""
    return f"{p.scheme}://{p.netloc}{path}{q}"


def _path_parts(url: str) -> list[str]:
    return [s for s in unquote(urlsplit(url).path).split("/") if s]


def _is_product(url: str, text: str, host: str) -> bool:
    p = urlsplit(url)
    if (p.hostname or "").lower().removeprefix("www.") != host:
        return False
    if re.search(r"product\.aspx\?id=\d+", url, re.I):
        return True
    if NON_PRODUCT.search(unquote(url)):
        return False
    parts = _path_parts(url)
    if len(parts) >= 3 and ("_דגם_" in parts[-1] or "דגם" in text):
        return True
    return len(parts) >= 3 and bool(re.search(r"[A-Za-z]+[0-9]|[0-9]+[A-Za-z]", parts[-1]))


def _is_category(url: str, host: str) -> bool:
    p = urlsplit(url)
    if (p.hostname or "").lower().removeprefix("www.") != host:
        return False
    if NON_PRODUCT.search(unquote(url)) or re.search(r"product\.aspx", url, re.I):
        return False
    parts = _path_parts(url)
    return 1 <= len(parts) <= 2 and "_דגם_" not in parts[-1]


def category_from_url(url: str) -> str:
    parts = _path_parts(url)
    if len(parts) >= 3:
        return " / ".join(p.replace("_", " ") for p in parts[:2])
    return ""


async def collect_product_urls(http: Http, cfg: dict, deadline: float | None = None) -> list[str]:
    base = cfg["base_url"].rstrip("/")
    host = urlsplit(base).hostname.lower().removeprefix("www.")
    products: dict[str, str] = {}
    categories: dict[str, str] = {}

    def absorb(html: str, page_url: str):
        for href, text in links(html, page_url):
            c = _canon(href)
            if _is_product(href, text, host):
                products.setdefault(c, href)
            elif _is_category(href, host):
                categories.setdefault(c, href)

    for sm in cfg.get("sitemap_pages", []):
        r = await http.get(_encode(urljoin(base + "/", sm.lstrip("/"))))
        if r and r.ok:
            absorb(r.text, r.url)
            log.info("Traklin site map: %d product links so far", len(products))
        else:
            log.warning("Traklin site map not reachable (%s)", r.status if r else "no response")

    for seed in cfg.get("category_seeds", []):
        categories.setdefault(_canon(base + seed), base + seed)

    # Crawl category pages (and their numbered pages) to catch anything the site map misses
    visited: set[str] = set()
    queue = list(categories.values())
    loop = asyncio.get_event_loop()
    max_pages = int(cfg.get("max_category_pages", 1500))

    async def visit(url: str):
        r = await http.get(_encode(url))
        if not (r and r.ok):
            return []
        before = len(products)
        absorb(r.text, r.url)
        new = []
        for href, text in links(r.text, r.url):
            t = text.split("|")[0].strip()
            if PAGINATION_TEXT.match(t) and urlsplit(href).path.rstrip("/") == urlsplit(r.url).path.rstrip("/"):
                new.append(href)
        for c, href in categories.items():
            if c not in visited:
                new.append(href)
        log.debug("category %s: +%d products", unquote(url), len(products) - before)
        return new

    while queue and len(visited) < max_pages:
        if deadline and loop.time() > deadline:
            break
        batch = []
        while queue and len(batch) < 6:
            u = queue.pop(0)
            c = _canon(u)
            if c in visited:
                continue
            visited.add(c)
            batch.append(u)
        if not batch:
            continue
        for found in await asyncio.gather(*(visit(u) for u in batch)):
            for u in found:
                if _canon(u) not in visited:
                    queue.append(u)

    log.info("Traklin: %d category pages scanned, %d product URLs found", len(visited), len(products))
    return list(products.values())


async def fetch_product(http: Http, url: str) -> TraklinProduct | None:
    r = await http.get(_encode(url))
    if not (r and r.ok):
        return None
    info = extract_page(r.text)
    title = (info.title or "").strip()
    model = extract_model(title, r.url) or model_from_label(info.text)
    return TraklinProduct(
        url=r.url, title=title, model=model, price=info.price, old_price=info.old_price,
        promos=info.promos, in_stock=info.in_stock, category=category_from_url(r.url) or category_from_url(url),
    )
