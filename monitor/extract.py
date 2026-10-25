"""Generic product-page extraction: price, old (crossed-out) price, promotions, stock.

Works across platforms by trying, in order of reliability:
  1. per-store CSS selectors from config (manual calibration, optional)
  2. JSON-LD schema.org Product/Offer (what most stores publish for Google)
  3. microdata itemprop="price" / meta product:price:amount
  4. Magento data-price-amount / WooCommerce <del>/<ins> markup
  5. elements whose class/id mentions "price", skipping related-product carousels
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
MIN_PRICE, MAX_PRICE = 5, 250_000

OLD_HINTS = re.compile(r"old|regular|before|was|strike|line-through|compare|list-?price|original|crossed|prev|full-?price|מחיר-?קודם", re.I)
SKIP_CONTAINERS = re.compile(
    r"related|upsell|up-sell|cross-?sell|similar|recommend|carousel|slider|swiper|also|accessor|"
    r"bundle|footer|header|nav|menu|mini-?cart|recently|viewed|compare-list|banner", re.I)
PROMO_WORDS = [
    "מבצע", "הנחה", "קופון", "מחיר מועדון", "לחברי מועדון", "חבר מועדון", "במתנה", "מתנה",
    "1+1", "2+1", "החזר כספי", "הטבה", "מחיר מיוחד", "חסכו", "חיסכון", "בלעדי",
    "מחיר אונליין", "מחיר לאונליין", "בלאק פריידי", "black friday", "sale", "% הנחה",
    "עד גמר המלאי", "בהזמנה מהאתר", "פלוס", "טרייד אין", "trade in",
]
PROMO_RE = re.compile("|".join(re.escape(w) for w in PROMO_WORDS), re.I)
OOS_RE = re.compile(r"אזל\s*מהמלאי|לא\s*במלאי|אזל\b|חסר\s*במלאי|out\s*of\s*stock|לא\s*זמין\s*להזמנה", re.I)


@dataclass
class PageInfo:
    title: str | None = None
    price: float | None = None
    old_price: float | None = None
    promos: list[str] = field(default_factory=list)
    in_stock: bool | None = None
    price_source: str | None = None
    text: str = ""            # visible text, used for model verification

    @property
    def discount_pct(self) -> float | None:
        if self.price and self.old_price and self.old_price > self.price * 1.01:
            return round((1 - self.price / self.old_price) * 100, 1)
        return None


def parse_price(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        v = float(s)
        return v if MIN_PRICE <= v <= MAX_PRICE else None
    s = str(s).replace("\xa0", " ")
    m = PRICE_RE.search(s)
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    v = float(whole + ("." + m.group(2) if m.group(2) else ""))
    return v if MIN_PRICE <= v <= MAX_PRICE else None


def _in_skipped_container(el: Tag, depth: int = 12) -> bool:
    p = el
    for _ in range(depth):
        p = p.parent
        if p is None or not isinstance(p, Tag):
            return False
        if p.name in ("footer", "header", "nav", "aside"):
            return True
        attrs = " ".join(p.get("class", []) or []) + " " + (p.get("id") or "")
        if attrs.strip() and SKIP_CONTAINERS.search(attrs):
            return True
    return False


def _attr_text(el: Tag) -> str:
    return " ".join(el.get("class", []) or []) + " " + (el.get("id") or "") + " " + (el.get("itemprop") or "")


# ---------- JSON-LD ----------

def _iter_jsonld(soup: BeautifulSoup):
    for s in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        raw = s.string or s.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except Exception:
            # Some stores put several objects or trailing commas; try a lenient cleanup
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()))
            except Exception:
                continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                yield d
                for k in ("@graph", "mainEntity", "itemListElement"):
                    if k in d:
                        stack.append(d[k])


def _from_jsonld(soup: BeautifulSoup, info: PageInfo) -> bool:
    for d in _iter_jsonld(soup):
        t = d.get("@type")
        types = t if isinstance(t, list) else [t]
        if not any(str(x).lower() in ("product", "productgroup", "individualproduct") for x in types):
            continue
        info.title = info.title or d.get("name")
        offers = d.get("offers")
        offers = offers if isinstance(offers, list) else [offers] if offers else []
        for o in offers:
            if not isinstance(o, dict):
                continue
            price = parse_price(o.get("price") or o.get("lowPrice"))
            spec = o.get("priceSpecification")
            specs = spec if isinstance(spec, list) else [spec] if spec else []
            for sp in specs:
                if not isinstance(sp, dict):
                    continue
                ptype = str(sp.get("priceType", "")).lower()
                v = parse_price(sp.get("price"))
                if v is None:
                    continue
                if "strikethrough" in ptype or "listprice" in ptype or "msrp" in ptype:
                    info.old_price = info.old_price or v
                elif price is None:
                    price = v
            if price is not None:
                info.price = price
                info.price_source = "json-ld"
                av = str(o.get("availability", "")).lower()
                if "outofstock" in av or "discontinued" in av or "soldout" in av:
                    info.in_stock = False
                elif "instock" in av or "limitedavailability" in av:
                    info.in_stock = True
                hp = parse_price(o.get("highPrice"))
                if hp and hp > price * 1.01 and not info.old_price and o.get("@type") != "AggregateOffer":
                    info.old_price = hp
                return True
    return False


# ---------- meta / microdata / platform markup ----------

def _from_meta(soup: BeautifulSoup, info: PageInfo) -> bool:
    sale = soup.find("meta", attrs={"property": re.compile(r"product:sale_price:amount", re.I)})
    reg = soup.find("meta", attrs={"property": re.compile(r"(product|og):price:amount", re.I)})
    if sale and parse_price(sale.get("content")):
        info.price = parse_price(sale.get("content"))
        if reg and parse_price(reg.get("content")) and parse_price(reg.get("content")) > info.price:
            info.old_price = parse_price(reg.get("content"))
        info.price_source = "meta"
        return True
    if reg and parse_price(reg.get("content")):
        info.price = parse_price(reg.get("content"))
        info.price_source = "meta"
        return True
    for el in soup.select('[itemprop="price"]'):
        if _in_skipped_container(el):
            continue
        v = parse_price(el.get("content") or el.get_text(" ", strip=True))
        if v:
            info.price = v
            info.price_source = "microdata"
            return True
    return False


def _from_magento(soup: BeautifulSoup, info: PageInfo) -> bool:
    final = [el for el in soup.select('[data-price-type="finalPrice"][data-price-amount]') if not _in_skipped_container(el)]
    if not final:
        return False
    info.price = parse_price(final[0].get("data-price-amount"))
    old = [el for el in soup.select('[data-price-type="oldPrice"][data-price-amount]') if not _in_skipped_container(el)]
    if old:
        v = parse_price(old[0].get("data-price-amount"))
        if v and info.price and v > info.price:
            info.old_price = v
    info.price_source = "magento"
    return info.price is not None


def _from_woocommerce(soup: BeautifulSoup, info: PageInfo) -> bool:
    for box in soup.select("p.price, span.price, div.summary .price"):
        if _in_skipped_container(box):
            continue
        ins = box.select_one("ins .amount, ins bdi, ins")
        dele = box.select_one("del .amount, del bdi, del")
        if ins:
            info.price = parse_price(ins.get_text(" ", strip=True))
            if dele:
                info.old_price = parse_price(dele.get_text(" ", strip=True))
        else:
            amt = box.select_one(".amount, bdi")
            info.price = parse_price((amt or box).get_text(" ", strip=True))
        if info.price:
            info.price_source = "woocommerce"
            return True
    return False


def _from_heuristic(soup: BeautifulSoup, info: PageInfo) -> bool:
    main = soup.find("main") or soup.find(id=re.compile("main|content", re.I)) or soup.body or soup
    current, old = [], []
    for el in main.find_all(True):
        at = _attr_text(el)
        is_strike = el.name in ("del", "s", "strike") or "line-through" in (el.get("style") or "")
        if not (re.search(r"price|מחיר", at, re.I) or is_strike):
            continue
        txt = el.get_text(" ", strip=True)
        if len(txt) > 60 or not re.search(r"\d", txt):
            continue
        # prefer leaf elements, so a box holding both current and old price isn't read as one
        if len(el.find_all(True)) > 6 or any(
                re.search(r"price|מחיר", _attr_text(d), re.I) or d.name in ("del", "s", "strike")
                for d in el.find_all(True)):
            continue
        if _in_skipped_container(el):
            continue
        v = parse_price(txt)
        if v is None:
            continue
        if is_strike or OLD_HINTS.search(at):
            old.append(v)
        elif re.search(r"club|מועדון|installment|payment|תשלום|monthly|points|נקודות", at + " " + txt, re.I):
            continue
        else:
            current.append(v)
    if current:
        info.price = current[0]
        info.price_source = "heuristic"
        bigger = [o for o in old if o > info.price * 1.01]
        if bigger and not info.old_price:
            info.old_price = bigger[0]
        return True
    return False


def _css_price(soup: BeautifulSoup, css: str | None) -> float | None:
    if not css:
        return None
    el = soup.select_one(css)
    if not el:
        return None
    return parse_price(el.get("content") or el.get("data-price-amount") or el.get_text(" ", strip=True))


def _promos(soup: BeautifulSoup, promo_css: str | None) -> list[str]:
    out: list[str] = []
    if promo_css:
        for el in soup.select(promo_css):
            t = el.get_text(" ", strip=True)
            if t:
                out.append(t[:100])
    main = soup.find("main") or soup.body or soup
    for el in main.find_all(string=PROMO_RE):
        parent = el.parent
        if parent is None or parent.name in ("script", "style", "noscript", "title", "option"):
            continue
        if _in_skipped_container(parent):
            continue
        t = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))
        if 3 <= len(t) <= 110 and t not in out:
            out.append(t)
        if len(out) >= 12:
            break
    return out


RELATED_BLOCKS = re.compile(
    r"related|upsell|up-sell|cross-?sell|similar|recommend|carousel|slider|swiper|recently|viewed|"
    r"also-?(bought|like)|mega-?menu|breadcrumb", re.I)


def visible_text(soup: BeautifulSoup) -> str:
    """The product's own text: page chrome and other-product blocks (carousels,
    'similar products', menus) removed, so a model mentioned there can't confirm a match."""
    for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside"]):
        t.decompose()
    for el in soup.find_all(True):
        if getattr(el, "decomposed", False) or not isinstance(el.attrs, dict):
            continue    # already removed together with an ancestor
        key = " ".join(el.attrs.get("class", []) or []) + " " + str(el.attrs.get("id") or "")
        if key.strip() and RELATED_BLOCKS.search(key):
            el.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def extract_page(html: str, price_css: str | None = None, old_price_css: str | None = None,
                 promo_css: str | None = None) -> PageInfo:
    soup = BeautifulSoup(html or "", "lxml")
    info = PageInfo()
    h1 = soup.find("h1")
    og = soup.find("meta", attrs={"property": "og:title"})
    info.title = (h1.get_text(" ", strip=True) if h1 else None) or (og.get("content") if og else None) or (
        soup.title.get_text(strip=True) if soup.title else None)

    manual = _css_price(soup, price_css)
    if manual:
        info.price, info.price_source = manual, "config-css"
        info.old_price = _css_price(soup, old_price_css)
    else:
        for fn in (_from_jsonld, _from_magento, _from_meta, _from_woocommerce, _from_heuristic):
            if fn(soup, info):
                break
        # Structured data often has the price but not the crossed-out one
        if info.price and not info.old_price:
            probe = PageInfo(price=None)
            if _from_magento(soup, probe) and probe.old_price and probe.old_price > info.price:
                info.old_price = probe.old_price
            else:
                probe = PageInfo()
                if _from_heuristic(soup, probe) and probe.old_price and probe.old_price > info.price * 1.01 \
                        and abs((probe.price or 0) - info.price) < 1:
                    info.old_price = probe.old_price
        if old_price_css:
            v = _css_price(soup, old_price_css)
            if v and info.price and v > info.price:
                info.old_price = v

    info.promos = _promos(soup, promo_css)
    info.text = visible_text(soup)
    if info.in_stock is None:
        # Only look near the top of the text to avoid "out of stock" in other products
        head = info.text[:6000]
        info.in_stock = False if OOS_RE.search(head) else None
    return info


def links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute href, anchor+context text) for every link on the page."""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        ctx = a.get_text(" ", strip=True)
        if a.get("title"):
            ctx += " " + a["title"]
        img = a.find("img")
        if img is not None and img.get("alt"):
            ctx += " " + img["alt"]
        # include a bit of the product card around the link
        card = a.parent
        for _ in range(3):
            if card is None or card.parent is None:
                break
            if len(card.get_text(" ", strip=True)) > 400:
                break
            card = card.parent
        if card is not None:
            ctx += " | " + card.get_text(" ", strip=True)[:400]
        out.append((urljoin(base_url, href), ctx))
    return out
