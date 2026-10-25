"""One full run: Traklin catalog -> competitor lookups -> diff vs last run -> report."""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .http import Http
from .models import extract_model, normalize
from .stores import BaseStore, Offer, UrlCache, make_store
from .traklin import TraklinProduct, collect_product_urls, fetch_product

log = logging.getLogger(__name__)


@dataclass
class Change:
    site: str
    site_name: str
    model: str
    title: str
    old: float | None
    new: float | None
    kind: str          # up | down | new_promo | promo_ended | back_in_stock | out_of_stock
    url: str | None = None

    @property
    def pct(self):
        if self.old and self.new:
            return round((self.new - self.old) / self.old * 100, 1)
        return None


@dataclass
class RunResult:
    date: str
    products: list[TraklinProduct]
    offers: dict[str, dict[str, Offer]]           # site -> normalized model -> offer
    stores: list[tuple[str, str]]                 # (key, name)
    changes: list[Change] = field(default_factory=list)
    health: dict = field(default_factory=dict)
    first_run: bool = False
    partial: bool = False
    prev_date: str | None = None


def _filter_boilerplate(promo_lists: list[list[str]]) -> None:
    """Drop promo snippets that appear on a large share of a site's pages
    (site-wide banners like 'מבצעי השבוע' are not product promotions)."""
    n = len(promo_lists)
    if n < 8:
        return
    freq = Counter(p for lst in promo_lists for p in set(lst))
    common = {p for p, c in freq.items() if c / n > 0.3}
    for lst in promo_lists:
        lst[:] = [p for p in lst if p not in common][:3]


async def run(cfg: dict, data_dir: Path, debug_dir: Path, *, max_products: int | None = None,
              calibrate: bool = False) -> RunResult:
    run_cfg = cfg.get("run", {})
    tcfg = cfg["traklin"]
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 60 * float(run_cfg.get("max_runtime_minutes", 320))
    max_products = max_products if max_products is not None else int(run_cfg.get("max_products", 0) or 0)

    http = Http(max_retries=int(run_cfg.get("http_retries", 4)))
    http.configure_host(tcfg["base_url"], int(tcfg.get("concurrency", 3)), float(tcfg.get("delay_seconds", 0.6)))
    defaults = cfg.get("store_defaults", {})
    store_cfgs = [s for s in cfg.get("stores", []) if s.get("enabled", True)]
    for s in store_cfgs:
        http.configure_host(s["base_url"], int(s.get("concurrency", defaults.get("concurrency", 2))),
                            float(s.get("delay_seconds", defaults.get("delay_seconds", 1.0))))

    today = dt.date.today().isoformat()
    try:
        # ---------- 1. Traklin product list ----------
        urls = await collect_product_urls(http, tcfg, deadline=loop.time() + 45 * 60)
        if max_products:
            random.Random(today).shuffle(urls)
            urls = urls[: max_products * 2]   # some pages have no model/price

        cache = UrlCache(data_dir / "url_cache.json")
        stores: list[BaseStore] = [make_store(s, http, cache, run_cfg, cfg.get("matching", {}), debug_dir)
                                   for s in store_cfgs]
        for s in stores:
            s.calibrate = calibrate

        async def one(store: BaseStore, model: str) -> Offer:
            if loop.time() > deadline:
                return Offer(store.key, model, status="skipped")
            try:
                return await store.lookup(model)
            except Exception as e:  # never let one product kill the run
                log.debug("%s %s: %s", store.key, model, e)
                return Offer(store.key, model, status="site_error")

        async def lookups(models: list[str]) -> list[list[Offer]]:
            return await asyncio.gather(*(asyncio.gather(*(one(s, m) for m in models)) for s in stores))

        # Most Traklin URLs already carry the model ("..._דגם_gr-728b_..."), so competitor
        # lookups start right away, in parallel with reading Traklin's own pages.
        early: dict[str, str] = {}
        for u in urls:
            m = extract_model("", u)
            if m:
                early.setdefault(normalize(m), m)
        if max_products:
            early = dict(list(early.items())[:max_products])

        async def traklin_pages():
            return await asyncio.gather(*(fetch_product(http, u) for u in urls))

        await asyncio.gather(*(s.setup() for s in stores))
        fetched, early_offers = await asyncio.gather(traklin_pages(), lookups(list(early.values())))

        by_model: dict[str, TraklinProduct] = {}
        no_model = 0
        for p in fetched:
            if p is None:
                continue
            if not p.model:
                no_model += 1
                continue
            by_model.setdefault(normalize(p.model), p)
        products = list(by_model.values())
        if max_products:
            products = products[:max_products]
        _filter_boilerplate([p.promos for p in products])
        log.info("Traklin: %d products with a model (%d pages without a model)", len(products), no_model)

        # ---------- 2. Competitors: models only known from the product page ----------
        late_models = [p.model for p in products if normalize(p.model) not in early]
        late_offers = await lookups(late_models) if late_models else [[] for _ in stores]

        wanted = {normalize(p.model) for p in products}
        offers: dict[str, dict[str, Offer]] = {}
        for s, e_list, l_list in zip(stores, early_offers, late_offers):
            res = {normalize(o.model): o for o in list(e_list) + list(l_list) if normalize(o.model) in wanted}
            _filter_boilerplate([o.promos for o in res.values() if o.status == "ok"])
            s.dump_debug()
            log.info("%s: matched %d / %d", s.name, sum(o.match != "none" for o in res.values()), len(res))
            offers[s.key] = res
        cache.save()

        partial = loop.time() > deadline
        health = {
            "traklin": {"pages": len(urls), "products": len(products), "no_model": no_model,
                        "priced": sum(1 for p in products if p.price)},
            "stores": [s.health() for s in stores],
            "http": http.stats,
        }
        result = RunResult(date=today, products=products, offers=offers,
                           stores=[(s.key, s.name) for s in stores], health=health, partial=partial)
        compare_with_previous(result, data_dir, cfg)
        return result
    finally:
        await http.close()


# ---------------- history ----------------

def _snapshot(result: RunResult) -> dict:
    return {
        "date": result.date,
        "traklin": {normalize(p.model): {"price": p.price, "old_price": p.old_price, "promos": p.promos,
                                         "in_stock": p.in_stock, "title": p.title, "url": p.url}
                    for p in result.products},
        "offers": {site: {m: {"price": o.price, "old_price": o.old_price, "promos": o.promos,
                              "in_stock": o.in_stock, "url": o.url, "match": o.match}
                          for m, o in offs.items() if o.status in ("ok", "no_price")}
                   for site, offs in result.offers.items()},
    }


def compare_with_previous(result: RunResult, data_dir: Path, cfg: dict) -> None:
    prev_path = data_dir / "latest.json"
    min_pct = float(cfg.get("report", {}).get("min_change_percent", 0.5))
    if not prev_path.exists():
        result.first_run = True
        return
    prev = json.loads(prev_path.read_text("utf-8"))
    result.prev_date = prev.get("date")
    names = dict(result.stores)
    names["traklin"] = cfg["traklin"]["name"]

    def diff(site, model, title, url, now: dict, before: dict | None):
        if not before:
            return
        a, b = before.get("price"), now.get("price")
        if a and b and abs(b - a) / a * 100 >= min_pct:
            result.changes.append(Change(site, names[site], model, title, a, b, "up" if b > a else "down", url))
        had_promo = bool(before.get("promos")) or bool(before.get("old_price"))
        has_promo = bool(now.get("promos")) or bool(now.get("old_price"))
        new_discount = bool(now.get("old_price")) and not before.get("old_price")
        new_text = set(now.get("promos") or []) - set(before.get("promos") or [])
        if new_discount or (has_promo and not had_promo) or new_text:
            result.changes.append(Change(site, names[site], model, title, a, b, "new_promo", url))
        elif had_promo and not has_promo:
            result.changes.append(Change(site, names[site], model, title, a, b, "promo_ended", url))
        if before.get("in_stock") is False and now.get("in_stock") is True:
            result.changes.append(Change(site, names[site], model, title, a, b, "back_in_stock", url))
        if before.get("in_stock") is True and now.get("in_stock") is False:
            result.changes.append(Change(site, names[site], model, title, a, b, "out_of_stock", url))

    for p in result.products:
        m = normalize(p.model)
        diff("traklin", p.model, p.title, p.url,
             {"price": p.price, "old_price": p.old_price, "promos": p.promos, "in_stock": p.in_stock},
             prev.get("traklin", {}).get(m))
        for site, offs in result.offers.items():
            o = offs.get(m)
            if o and o.status in ("ok", "no_price"):
                diff(site, p.model, p.title, o.url,
                     {"price": o.price, "old_price": o.old_price, "promos": o.promos, "in_stock": o.in_stock},
                     prev.get("offers", {}).get(site, {}).get(m))


def save(result: RunResult, data_dir: Path) -> None:
    """Persist today's snapshot (for tomorrow's comparison) and append price changes to a log."""
    data_dir.mkdir(parents=True, exist_ok=True)
    snap = _snapshot(result)
    prev_path = data_dir / "latest.json"
    # a partial or failed run must not erase good data for sites it didn't reach
    if prev_path.exists() and (result.partial or not result.products):
        prev = json.loads(prev_path.read_text("utf-8"))
        for site, offs in prev.get("offers", {}).items():
            for m, rec in offs.items():
                snap["offers"].setdefault(site, {}).setdefault(m, rec)
        for m, rec in prev.get("traklin", {}).items():
            snap["traklin"].setdefault(m, rec)
    prev_path.write_text(json.dumps(snap, ensure_ascii=False), "utf-8")

    log_path = data_dir / "price_changes.csv"
    new_file = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "store", "model", "product", "old_price", "new_price", "change_pct", "kind", "url"])
        for c in result.changes:
            if c.kind in ("up", "down"):
                w.writerow([result.date, c.site_name, c.model, c.title, c.old, c.new, c.pct, c.kind, c.url])
    (data_dir / "last_run_health.json").write_text(
        json.dumps({"date": result.date, "partial": result.partial, **result.health}, ensure_ascii=False, indent=1),
        "utf-8")
