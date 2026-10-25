"""Competitor lookups.

GenericStore – works on most store sites without custom code:
  * discovers the site's search URL (JSON-LD SearchAction, OpenSearch, <form>),
    and tries configured templates;
  * uses the WooCommerce Store API automatically when the site has one;
  * picks result links that carry the exact model, opens the product page and
    VERIFIES the model appears there before accepting the price.
KspStore – KSP's own JSON API (search + item for live campaign discounts).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlsplit

from bs4 import BeautifulSoup

from .extract import extract_page, links, parse_price
from .http import Http, Response
from .models import find_exact, find_similar, normalize

log = logging.getLogger(__name__)

SEARCH_PARAM_NAMES = {"q", "s", "search", "keyword", "keywords", "query", "text", "term", "searchterm",
                      "k", "w", "searchstring", "search_query", "srch", "kw"}
SEARCHY_PATH = re.compile(r"search|catalogsearch|/find|results?\b|\?s=|[?&](q|query|keyword|search)=", re.I)


@dataclass
class Offer:
    site: str
    model: str
    url: str | None = None
    title: str | None = None
    price: float | None = None
    old_price: float | None = None
    promos: list[str] = field(default_factory=list)
    in_stock: bool | None = None
    match: str = "none"            # exact | similar | none
    matched_model: str | None = None
    status: str = "not_found"      # ok | not_found | no_price | site_error | skipped

    @property
    def discount_pct(self):
        if self.price and self.old_price and self.old_price > self.price * 1.01:
            return round((1 - self.price / self.old_price) * 100, 1)
        return None

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "site", "model", "url", "title", "price", "old_price", "promos", "in_stock",
            "match", "matched_model", "status")}


class UrlCache:
    """Remembers where each model lives on each site, so daily runs skip searching."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text("utf-8"))
            except Exception:
                self.data = {}
        self.data.setdefault("urls", {})
        self.data.setdefault("sites", {})

    def get(self, site: str, model: str):
        return self.data["urls"].get(site, {}).get(normalize(model))

    def put(self, site: str, model: str, url: str | None, extra: dict | None = None):
        rec = {"url": url, "ts": dt.date.today().isoformat()}
        if extra:
            rec.update(extra)
        self.data["urls"].setdefault(site, {})[normalize(model)] = rec

    def site_state(self, site: str) -> dict:
        return self.data["sites"].setdefault(site, {})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=0, sort_keys=True), "utf-8")


class BaseStore:
    def __init__(self, cfg: dict, http: Http, cache: UrlCache, run_cfg: dict, match_cfg: dict, debug_dir: Path):
        self.key = cfg["key"]
        self.name = cfg["name"]
        self.base = cfg["base_url"].rstrip("/")
        self.host = urlsplit(self.base).hostname.lower().removeprefix("www.")
        self.cfg = cfg
        self.http = http
        self.cache = cache
        self.recheck_days = int(run_cfg.get("recheck_not_found_days", 7))
        self.probe_n = int(run_cfg.get("site_probe_products", 25))
        self.allow_similar = bool(match_cfg.get("allow_similar_models", True))
        self.sim_threshold = float(match_cfg.get("similarity_threshold", 0.86))
        self.debug_dir = debug_dir / self.key
        self.searches = 0
        self.found = 0
        self.search_page_hits = 0
        self.misses = 0          # completed lookups that found nothing
        self.signals = 0         # evidence the site answers searches (model seen in results / valid API reply)
        self.broken = False
        self.notes: list[str] = []
        self._debug_samples: list[tuple[str, str]] = []
        self.calibrate = False

    # --- helpers ---
    def _recent_not_found(self, rec, model: str = "") -> bool:
        """True = skip searching today. Each missing model gets re-searched about once per
        `recheck_days`, on its own day of the cycle, so re-checks are spread evenly over the
        week instead of all landing on the same day."""
        if not rec or rec.get("url"):
            return False
        try:
            age = (dt.date.today() - dt.date.fromisoformat(rec["ts"])).days
        except Exception:
            return False
        if age < 1:
            return True
        if age >= self.recheck_days * 2:
            return False
        slot = zlib.crc32(f"{self.key}:{normalize(model)}".encode()) % max(1, self.recheck_days)
        return dt.date.today().toordinal() % max(1, self.recheck_days) != slot

    def _keep_debug(self, label: str, html: str):
        if self.calibrate or len(self._debug_samples) < 4:
            self._debug_samples.append((label, html))

    def dump_debug(self, force: bool = False):
        if not (force or self.calibrate or self.found == 0):
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        for i, (label, html) in enumerate(self._debug_samples):
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80]
            (self.debug_dir / f"{i:02d}_{safe}.html").write_text(html or "", "utf-8")
        (self.debug_dir / "notes.txt").write_text("\n".join(self.notes), "utf-8")

    def _same_site(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.") == self.host

    def health(self) -> dict:
        return {"site": self.key, "name": self.name, "searches": self.searches, "found": self.found,
                "broken": self.broken, "notes": self.notes[-5:]}

    async def setup(self):
        pass

    async def lookup(self, model: str) -> Offer:
        raise NotImplementedError

    def _check_broken(self):
        # Lookups run in parallel, so judge by evidence rather than by order of completion:
        # only give up on a site that never once showed a sign of answering searches.
        self.misses += 1
        if not self.broken and self.misses >= self.probe_n and self.found == 0 and self.signals == 0:
            self.broken = True
            self.notes.append(f"No product matched after {self.searches} searches – the site is "
                              f"probably blocking automated access or changed its layout.")
            # forget the remembered search URL so the next run re-discovers it
            self.cache.site_state(self.key).pop("search_template", None)
            log.warning("%s: marking as not working this run", self.name)


class GenericStore(BaseStore):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.templates: list[str] = []
        self.fail_streak = 0
        self.mode = "html"

    async def setup(self):
        state = self.cache.site_state(self.key)
        discovered: list[str] = []
        home = await self.http.get(self.base + "/")
        if home is None or not home.ok:
            self.notes.append(f"Home page not reachable (status {home.status if home else 'none'}).")
            if home is not None:
                self._keep_debug(f"home_status_{home.status}", home.text)
        else:
            self._keep_debug("home", home.text)
            discovered = await self._discover_search(home)
        # WooCommerce Store API (cheap single probe)
        woo = await self.http.get(self.base + "/wp-json/wc/store/v1/products?per_page=1", accept_json=True)
        if woo and woo.ok and woo.text.lstrip().startswith("["):
            self.mode = "woo"
            self.notes.append("Using WooCommerce Store API.")
        ordered = []
        if state.get("search_template"):
            ordered.append(state["search_template"])
        ordered += discovered + list(self.cfg.get("search_templates", []))
        seen = set()
        self.templates = [t for t in ordered if not (t in seen or seen.add(t))]
        self.notes.append("Search templates: " + ", ".join(self.templates[:6]))

    async def _discover_search(self, home: Response) -> list[str]:
        out: list[str] = []
        soup = BeautifulSoup(home.text, "lxml")
        # 1. JSON-LD SearchAction
        for s in soup.find_all("script", type=lambda t: t and "ld+json" in t):
            for m in re.finditer(r'"(?:target|urlTemplate)"\s*:\s*"([^"]*\{search_term_string\}[^"]*)"', s.get_text()):
                out.append(m.group(1).replace("\\/", "/").replace("{search_term_string}", "{q}"))
        # 2. OpenSearch description
        link = soup.find("link", rel=lambda r: r and "search" in r, type=re.compile("opensearch", re.I))
        if link and link.get("href"):
            r = await self.http.get(urljoin(home.url, link["href"]))
            if r and r.ok:
                m = re.search(r'template="([^"]*\{searchTerms\}[^"]*)"', r.text)
                if m:
                    out.append(m.group(1).replace("&amp;", "&").replace("{searchTerms}", "{q}"))
        # 3. Search forms
        for form in soup.find_all("form"):
            field_name = None
            for inp in form.find_all("input"):
                n = (inp.get("name") or "").strip()
                if inp.get("type", "").lower() == "search" or n.lower() in SEARCH_PARAM_NAMES:
                    if n:
                        field_name = n
                        break
            if not field_name:
                continue
            action = urljoin(home.url, form.get("action") or home.url)
            extras = [f"{h.get('name')}={quote_plus(h.get('value', ''))}" for h in form.find_all("input", type="hidden")
                      if h.get("name") and len(h.get("value", "")) < 40 and "token" not in h.get("name", "").lower()
                      and "key" not in h.get("name", "").lower()]
            qs = "&".join([f"{field_name}={{q}}"] + extras[:3])
            out.append(action + ("&" if "?" in action else "?") + qs)
        # make relative to base where possible
        res = []
        for t in out:
            if t.startswith(self.base):
                t = t[len(self.base):] or "/"
            elif t.startswith("http") and not self._same_site(t.replace("{q}", "x")):
                continue
            res.append(t)
        return res

    def _search_url(self, template: str, q: str) -> str:
        t = template.replace("{q}", quote_plus(q))
        return t if t.startswith("http") else self.base + ("" if t.startswith("/") else "/") + t

    # ---------------------------------------------------------------
    async def lookup(self, model: str) -> Offer:
        rec = self.cache.get(self.key, model)
        if self._recent_not_found(rec, model):
            return Offer(self.key, model, status="not_found")
        if rec and rec.get("url"):
            offer = await self._from_product_url(model, rec["url"])
            if offer and offer.status in ("ok", "no_price"):
                self.found += 1
                return offer
        if self.broken:
            return Offer(self.key, model, status="site_error")

        self.searches += 1
        offer = await (self._search_woo(model) if self.mode == "woo" else self._search_html(model))
        if offer and offer.match != "none":
            self.found += 1
            self.cache.put(self.key, model, offer.url)
            return offer
        if self.signals > 0:
            self.cache.put(self.key, model, None)
        self._check_broken()
        return Offer(self.key, model, status="not_found" if self.signals > 0 or not self.broken else "site_error")

    async def _from_product_url(self, model: str, url: str, page: Response | None = None) -> Offer | None:
        r = page or await self.http.get(url, referer=self.base + "/")
        if not (r and r.ok):
            return None
        info = extract_page(r.text, self.cfg.get("price_css"), self.cfg.get("old_price_css"), self.cfg.get("promo_css"))
        if self.calibrate:
            self._keep_debug(f"product_{normalize(model)}", r.text)
        # Verify against the product's own title/text – the decisive check
        head = f"{info.title or ''} {info.text[:20000]}"
        match, matched = "none", None
        if find_exact(model, head):
            match, matched = "exact", model
        elif self.allow_similar:
            title_sim = find_similar(model, info.title or "", self.sim_threshold)
            if title_sim:
                match, matched = "similar", title_sim
        if match == "none":
            return None
        return Offer(self.key, model, url=r.url, title=info.title, price=info.price, old_price=info.old_price,
                     promos=info.promos, in_stock=info.in_stock, match=match, matched_model=matched,
                     status="ok" if info.price else "no_price")

    async def _search_html(self, model: str) -> Offer | None:
        queries = [model]
        if normalize(model) != model.upper():
            queries.append(normalize(model))
        state = self.cache.site_state(self.key)
        for template in list(self.templates):
            for q in queries:
                url = self._search_url(template, q)
                if self.broken:
                    return None
                r = await self.http.get(url, referer=self.base + "/")
                if not (r and r.ok):
                    self.fail_streak += 1
                    if self.fail_streak >= 10 and self.found == 0 and self.signals == 0 and not self.broken:
                        self.broken = True
                        self.notes.append(f"Search requests keep failing (last status "
                                          f"{r.status if r else 'no response'}) – the site is blocking access.")
                        self.cache.site_state(self.key).pop("search_template", None)
                        log.warning("%s: blocked, skipping it this run", self.name)
                    continue
                self.fail_streak = 0
                self._keep_debug(f"search_{normalize(model)}", r.text)
                landed_on_product = not SEARCHY_PATH.search(r.url) and r.url.rstrip("/") != url.rstrip("/")
                if landed_on_product:   # single result -> redirected straight to the product
                    offer = await self._from_product_url(model, r.url, page=r)
                    if offer:
                        self.signals += 1
                        state["search_template"] = template
                        return offer
                has_model = find_exact(model, r.text)
                if has_model:
                    self.search_page_hits += 1
                    self.signals += 1
                    if state.get("search_template") != template:
                        state["search_template"] = template
                        self.templates.remove(template)
                        self.templates.insert(0, template)
                cands = self._candidates(model, r)
                for cand in cands[:3]:
                    offer = await self._from_product_url(model, cand)
                    if offer:
                        return offer
                if has_model:
                    break   # this template works; other query forms won't add much
            if state.get("search_template") == template:
                break       # a known-good template found nothing: the product isn't there
        return None

    def _candidates(self, model: str, r: Response) -> list[str]:
        exact, similar, seen = [], [], set()
        for href, ctx in links(r.text, r.url):
            if not self._same_site(href) or SEARCHY_PATH.search(href):
                continue
            key = href.split("#")[0].rstrip("/")
            if key in seen or key == r.url.rstrip("/"):
                continue
            seen.add(key)
            name = ctx.split("|")[0]
            if find_exact(model, href) or find_exact(model, name):
                exact.append(key)
            elif find_exact(model, ctx):
                similar.insert(0, key)
            elif self.allow_similar and find_similar(model, name, self.sim_threshold):
                similar.append(key)
        return exact + similar

    async def _search_woo(self, model: str) -> Offer | None:
        for q in dict.fromkeys([model, normalize(model)]):
            url = f"{self.base}/wp-json/wc/store/v1/products?search={quote_plus(q)}&per_page=20"
            r = await self.http.get(url, accept_json=True)
            if not (r and r.ok):
                continue
            try:
                items = r.json()
            except Exception:
                continue
            if isinstance(items, list):
                self.signals += 1
            best = None
            for it in items if isinstance(items, list) else []:
                text = f"{it.get('name', '')} {it.get('sku', '')}"
                if find_exact(model, text):
                    best = (it, "exact", model)
                    break
                if self.allow_similar and not best:
                    sim = find_similar(model, text, self.sim_threshold)
                    if sim:
                        best = (it, "similar", sim)
            if best:
                it, match, matched = best
                pr = it.get("prices", {}) or {}
                minor = int(pr.get("currency_minor_unit", 2) or 0)
                conv = lambda v: (int(v) / (10 ** minor)) if str(v or "").isdigit() else parse_price(v)
                price = conv(pr.get("price"))
                regular = conv(pr.get("regular_price"))
                promos = ["מבצע באתר"] if it.get("on_sale") else []
                return Offer(self.key, model, url=it.get("permalink"), title=BeautifulSoup(it.get("name", ""), "lxml").get_text(),
                             price=price, old_price=regular if regular and price and regular > price else None,
                             promos=promos, in_stock=it.get("is_in_stock"), match=match, matched_model=matched,
                             status="ok" if price else "no_price")
        return None


class KspStore(BaseStore):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.API = self.cfg.get("api_url", "https://ksp.co.il/m_action/api").rstrip("/")
        self.web = self.cfg.get("item_url", "https://ksp.co.il/web/item/")

    async def setup(self):
        r = await self.http.get(f"{self.API}/category/?search=samsung", accept_json=True,
                                referer=self.web)
        if not (r and r.ok and r.text.lstrip().startswith("{")):
            self.notes.append(f"KSP API probe failed (status {r.status if r else 'none'}) – Cloudflare may be blocking.")
            if r is not None:
                self._keep_debug("api_probe", r.text[:20000])

    async def _api(self, path: str):
        r = await self.http.get(self.API + path, accept_json=True, referer=self.web)
        if not (r and r.ok):
            return None
        try:
            return r.json().get("result")
        except Exception:
            self._keep_debug("api_bad_json", r.text[:20000])
            return None

    async def lookup(self, model: str) -> Offer:
        rec = self.cache.get(self.key, model)
        if self._recent_not_found(rec, model):
            return Offer(self.key, model, status="not_found")
        uin, match, matched = (rec or {}).get("uin"), (rec or {}).get("match", "exact"), (rec or {}).get("matched")
        labels: list[str] = []
        if not uin:
            if self.broken:
                return Offer(self.key, model, status="site_error")
            self.searches += 1
            res = await self._api(f"/category/?search={quote_plus(model)}")
            if res is not None:
                self.signals += 1
            items = (res or {}).get("items") or []
            if isinstance(items, dict):
                items = list(items.values())
            pick = None
            for it in items:
                text = f"{it.get('name', '')} {it.get('description', '')}"
                if find_exact(model, text):
                    pick = (it, "exact", model)
                    break
                if self.allow_similar and not pick:
                    sim = find_similar(model, it.get("name", ""), self.sim_threshold)
                    if sim:
                        pick = (it, "similar", sim)
            if not pick:
                if res is not None:
                    self.cache.put(self.key, model, None)
                    self._check_broken()
                    return Offer(self.key, model, status="not_found")
                self._check_broken()
                return Offer(self.key, model, status="site_error")
            it, match, matched = pick
            uin = str(it.get("uin"))
            labels = [l.get("msg") for l in (it.get("labels") or []) if isinstance(l, dict) and l.get("msg")]

        item = await self._api(f"/item/{uin}")
        if not item:
            return Offer(self.key, model, status="site_error")
        data = item.get("data") or {}
        price = parse_price(data.get("price") or data.get("min_price"))
        old = None
        promos = list(labels)
        for k, entry in (item.get("bms") or {}).items():
            disc = entry.get("discount") if isinstance(entry, dict) else None
            if isinstance(disc, dict) and (str(k) == str(uin) or len(item.get("bms")) == 1):
                v = parse_price(disc.get("value"))
                if v and price and v < price:
                    old, price = price, v
                if disc.get("name"):
                    promos.append(BeautifulSoup(str(disc["name"]), "lxml").get_text(" ", strip=True)[:100])
        in_stock = bool(data.get("addToCart")) if "addToCart" in data else None
        self.found += 1
        self.cache.put(self.key, model, f"{self.web}{uin}",
                       {"uin": uin, "match": match, "matched": matched})
        title = BeautifulSoup(str(data.get("name", "")), "lxml").get_text(" ", strip=True)
        return Offer(self.key, model, url=f"{self.web}{uin}", title=title, price=price,
                     old_price=old, promos=promos, in_stock=in_stock, match=match, matched_model=matched,
                     status="ok" if price else "no_price")


def make_store(cfg, *a, **kw) -> BaseStore:
    return KspStore(cfg, *a, **kw) if cfg.get("type") == "ksp" else GenericStore(cfg, *a, **kw)
