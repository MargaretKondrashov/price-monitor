"""Illustrative data used only by `test-email`, to check the email settings and
show what the report looks like. Prices here are made up."""
from __future__ import annotations

import datetime as dt

from .models import normalize
from .pipeline import Change, RunResult
from .stores import Offer
from .traklin import TraklinProduct


def sample_result(cfg: dict) -> RunResult:
    stores = [(s["key"], s["name"]) for s in cfg.get("stores", []) if s.get("enabled", True)]
    tk = "https://www.traklin.co.il/"
    prods = [
        TraklinProduct(tk, 'מסך "75 SAMSUNG SMART 4K דגם UE75U8000FUXIL סמסונג (דוגמה)', "UE75U8000FUXIL", 3290,
                       category="מוצרי אלקטרוניקה / טלוויזיות ומסכים"),
        TraklinProduct(tk, "מקרר 4 דלתות LG דגם GR-728B (דוגמה)", "GR-728B", 8990, 9990, ["מבצע השבוע"],
                       category="מקררים ומקפיאים / מקררים"),
        TraklinProduct(tk, "מכונת כביסה BOSCH דגם WGB244A0IL (דוגמה)", "WGB244A0IL", 3490,
                       category="נקיון וכביסה / מכונות כביסה"),
    ]
    # (price, old_price, promos, match) per store, per product
    table = {
        "UE75U8000FUXIL": [3190, 3349, None, 3250, (3049, None, [], "similar"), 3390, None],
        "GR-728B": [(8790, 9490, ["מחיר מועדון"], "exact"), 8990, 9190, None, 8890, None, 8690],
        "WGB244A0IL": [3590, 3490, None, 3390, None, (3290, 3690, ["הובלה חינם"], "exact"), 3550],
    }
    offers: dict = {k: {} for k, _ in stores}
    for p in prods:
        for (key, _), v in zip(stores, table[p.model]):
            if v is None:
                offers[key][normalize(p.model)] = Offer(key, p.model, status="not_found")
                continue
            price, old, promos, match = v if isinstance(v, tuple) else (v, None, [], "exact")
            offers[key][normalize(p.model)] = Offer(
                key, p.model, url="https://example.com/", title=p.title, price=price, old_price=old, promos=promos,
                match=match, matched_model="UE75U8000HUXIL" if match == "similar" else p.model, status="ok")
    names = dict(stores)
    k = [key for key, _ in stores]
    changes = [Change("traklin", "טרקלין חשמל", "WGB244A0IL", prods[2].title, 3590, 3490, "down", tk)]
    if len(k) >= 6:
        changes += [
            Change(k[0], names[k[0]], "UE75U8000FUXIL", prods[0].title, 3390, 3190, "down", "https://example.com/"),
            Change(k[2], names[k[2]], "GR-728B", prods[1].title, 8790, 9190, "up", "https://example.com/"),
            Change(k[5], names[k[5]], "WGB244A0IL", prods[2].title, 3290, 3290, "new_promo", "https://example.com/"),
        ]
    health = {"traklin": {"products": 3}, "stores": [
        {"site": key, "name": name, "searches": 3, "found": 2, "broken": False, "notes": []} for key, name in stores]}
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    return RunResult(date=dt.date.today().isoformat(), products=prods, offers=offers, stores=stores,
                     changes=changes, health=health, prev_date=yesterday)
