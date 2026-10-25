"""Local fake stores that mimic the real platforms, for end-to-end tests.

Each site binds its own loopback address (127.0.0.x) so hostnames differ,
like separate real domains do.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

# Mutable "world" so tests can change prices between runs
WORLD = {
    "traklin": {
        # slug -> (title, price, old_price)
        "מוצרי_אלקטרוניקה/טלוויזיות_ומסכים/מסך_75_samsung_smart_4k_דגם_ue75u8000fuxil_סמסונג":
            ('מסך "75 SAMSUNG SMART 4K דגם UE75U8000FUXIL סמסונג', 3290, None),
        "מקררים_ומקפיאים/מקררים/מקרר_4_דלתות_lg_דגם_gr-728b":
            ("מקרר 4 דלתות LG דגם GR-728B נירוסטה מושחרת", 8990, 9990),
        "נקיון_וכביסה/מכונות_כביסה/מכונת_כביסה_bosch_דגם_wgb244a0il":
            ("מכונת כביסה BOSCH 9 ק\"ג דגם WGB244A0IL", 3490, None),
        "product.aspx?id=11563": ("מקרר HAIER דגם HRF-825FB זכוכית שחורה", 7490, None),
        "בישול_ואפייה/תנורים/תנור_בילט_אין_ללא_דגם": ("תנור בילט אין יוקרתי", 2990, None),
    },
    "magento": {   # Payngo-like
        "337350": ("מקרר 4 דלתות 665 ליטר LG GR-728B התקן שבת", 8790, 9490, "GR-728B"),
        "512211": ("מסך 75 Samsung Crystal UHD UE75U8000FUXIL", 3349, None, "UE75U8000FUXIL"),
    },
    "woo": {   # Big Electric-like
        1: ("מכונת כביסה בוש WGB244A0IL 9 ק\"ג", "WGB244A0IL", 329000, 369000, True),
        2: ("מקרר LG GR-728S נירוסטה", "GR-728S", 899000, 899000, False),
    },
    "custom": {  # Bug-like: search for a single hit redirects to product; JSON-LD price
        "5102021ue75u8000huxil": ("טלוויזיה חכמה 75\" Samsung 4K UHD Crystal UHD UE75U8000HUXIL", 3049),
        "9577452452": ("מקרר 4 דלתות אינוורטר - 665 ליטר LG GR-728B נירוסטה מושחרת", 8890),
    },
    "ksp": {
        "402265": {"name": "מסך 75 Samsung UE75U8000FUXIL 4K", "price": 3390, "discount": 3190,
                   "label": "מחיר מבצע לזמן מוגבל"},
    },
}

CAROUSEL = """<div class="related-products carousel"><div class="item"><span class="price">99 ₪</span>
<a href="/999.html">מוצר אחר GR-728BXX</a></div></div>"""
BANNER = '<div class="top-strip">מבצעי השבוע באתר! משלוח חינם</div>'


def _send(h, code, body, ctype="text/html; charset=utf-8", location=None):
    data = body.encode("utf-8") if isinstance(body, str) else body
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    if location:
        h.send_header("Location", location)
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


class Traklin(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlsplit(self.path)
        path = unquote(u.path).strip("/")
        items = WORLD["traklin"]
        if path == "sitemap.aspx":
            # list all but one product: the last one must be found via category crawl
            keys = [k for k in items if not k.startswith("נקיון_וכביסה")]
            links = "".join(f'<li><a href="/{quote(k, safe="/?=._-")}">{items[k][0]}</a></li>' for k in keys)
            links += '<li><a href="/מוצרי_אלקטרוניקה">מוצרי אלקטרוניקה</a></li><li><a href="/login.aspx">כניסה</a></li>'
            return _send(self, 200, f"<html><body><ul>{links}</ul></body></html>")
        key = path + ("?" + u.query if u.query else "")
        if key in items:
            title, price, old = items[key]
            old_html = f'<span class="old-price">{old:,} ₪</span>' if old else ""
            promo = '<div class="product-badge">מבצע השבוע</div>' if old else ""
            return _send(self, 200, f"""<html><head><title>{title} | טרקלין</title></head><body>
            <header><nav><a href="/מוצרי_אלקטרוניקה">מוצרי אלקטרוניקה</a></nav>{BANNER}</header>
            <main><h1>{title}</h1>{promo}<div class="price-box"><span class="product-price">{price:,} ₪</span>{old_html}</div>
            <div>{BANNER}</div></main></body></html>""")
        if path in ("מוצרי_אלקטרוניקה", "מקררים_ומקפיאים", "נקיון_וכביסה", "בישול_ואפייה", "לבית_ולגן",
                    "מוצרי_מיזוג_וחימום_לבית", "מוצרי_חשמל_למטבח", "מוצרי_טיפוח_ועיצוב", "גיימינג_וסלולר"):
            page = parse_qs(u.query).get("page", ["1"])[0]
            prods = [k for k in items if k.startswith(path + "/")]
            if path == "נקיון_וכביסה" and page == "1":
                # product is on page 2 only -> tests pagination following
                body = f'<a href="/{quote(path)}?page=2">2</a>'
            else:
                body = "".join(f'<div class="card"><a href="/{quote(k)}">{items[k][0]}</a></div>' for k in prods)
            return _send(self, 200, f"<html><body><main>{body}</main></body></html>")
        return _send(self, 404, "not found")


class Magento(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/":
            return _send(self, 200, """<html><body><form id="search_mini_form" action="/catalogsearch/result/" method="get">
            <input id="search" type="text" name="q"></form></body></html>""")
        if u.path == "/catalogsearch/result/":
            q = parse_qs(u.query).get("q", [""])[0].upper().replace("-", "")
            hits = [(pid, v) for pid, v in WORLD["magento"].items() if q and q in v[3].replace("-", "")]
            cards = "".join(f'<li class="product-item"><a class="product-item-link" href="/{pid}.html">{v[0]}</a>'
                            f'<span data-price-type="finalPrice" data-price-amount="{v[1]}">{v[1]:,} ₪</span></li>'
                            for pid, v in hits)
            return _send(self, 200, f"<html><body><ol>{cards or 'לא נמצאו תוצאות'}</ol>"
                                    f'<a href="/cart">סל</a></body></html>')
        pid = u.path.strip("/").removesuffix(".html")
        if pid in WORLD["magento"]:
            title, price, old, model = WORLD["magento"][pid]
            old_html = (f'<span data-price-type="oldPrice" data-price-amount="{old}"><span class="price">{old:,} ₪</span></span>'
                        if old else "")
            return _send(self, 200, f"""<html><body><main><h1 class="page-title">{title}</h1>
            <div class="product-info-price"><span data-price-type="finalPrice" data-price-amount="{price}">
            <span class="price">{price:,} ₪</span></span>{old_html}</div>
            <div class="product attribute sku">מק"ט: {model}</div>
            {'<div class="promo-label">מחיר מועדון לחברי מועדון</div>' if old else ''}
            {CAROUSEL}</main></body></html>""")
        return _send(self, 404, "nope")


class Woo(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/":
            return _send(self, 200, '<html><body><form role="search" action="/"><input type="search" name="s">'
                                    '<input type="hidden" name="post_type" value="product"></form></body></html>')
        if u.path == "/wp-json/wc/store/v1/products":
            q = parse_qs(u.query).get("search", [""])[0].upper().replace("-", "")
            res = []
            for pid, (name, sku, price, regular, on_sale) in WORLD["woo"].items():
                if not q or q in sku.replace("-", ""):
                    res.append({"id": pid, "name": name, "sku": sku, "permalink": f"http://{self.headers['Host']}/p/{pid}",
                                "on_sale": on_sale, "is_in_stock": True,
                                "prices": {"price": str(price), "regular_price": str(regular),
                                           "sale_price": str(price), "currency_minor_unit": 2}})
            return _send(self, 200, json.dumps(res[:20] if q else res[:1], ensure_ascii=False), "application/json")
        return _send(self, 404, "nope")


class Custom(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/":
            return _send(self, 200, """<html><head><script type="application/ld+json">
            {"@context":"https://schema.org","@type":"WebSite","url":"/","potentialAction":{"@type":"SearchAction",
            "target":"http://HOST/search?q={search_term_string}","query-input":"required name=search_term_string"}}
            </script></head><body>home</body></html>""".replace("HOST", self.headers["Host"]))
        if u.path == "/search":
            q = parse_qs(u.query).get("q", [""])[0].upper().replace("-", "")
            norm = lambda t: "".join(c for c in t.upper() if c.isalnum())
            # loose search like real sites: first 9 characters of the model
            hits = [k for k, (title, _) in WORLD["custom"].items() if len(q) >= 6 and q[:9] in norm(title)]
            if len(hits) == 1:   # single result: jump to product
                return _send(self, 302, "", location=f"/{quote(hits[0])}")
            return _send(self, 200, "<html><body>לא נמצאו מוצרים</body></html>")
        slug = unquote(u.path.strip("/"))
        if slug in WORLD["custom"]:
            title, price = WORLD["custom"][slug]
            ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": title,
                             "offers": {"@type": "Offer", "price": str(price), "priceCurrency": "ILS",
                                        "availability": "https://schema.org/InStock"}}, ensure_ascii=False)
            return _send(self, 200, f"""<html><head><script type="application/ld+json">{ld}</script></head>
            <body><h1>{title}</h1><div class="price">{price:,} ₪</div>
            <div>ברכישת מוצר זה תוכלו לקבל עד {price:,} נקודות מועדון!</div></body></html>""")
        return _send(self, 404, "nope")


class Ksp(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path.startswith("/m_action/api/category"):
            q = parse_qs(u.query).get("search", [""])[0].upper()
            items = [{"uin": int(uin), "name": d["name"], "price": d["price"],
                      "labels": [{"msg": d["label"]}] if d.get("label") else []}
                     for uin, d in WORLD["ksp"].items() if q and q.replace("-", "") in d["name"].upper().replace("-", "")]
            if q == "SAMSUNG":
                items = [{"uin": 1, "name": "probe", "price": 1}]
            return _send(self, 200, json.dumps({"result": {"items": items, "products_total": len(items)}}),
                         "application/json")
        if u.path.startswith("/m_action/api/item/"):
            uin = u.path.rsplit("/", 1)[-1]
            d = WORLD["ksp"].get(uin)
            if not d:
                return _send(self, 404, "{}", "application/json")
            bms = {uin: {"discount": {"value": d["discount"], "name": "<b>הנחת חג</b>"}}} if d.get("discount") else {}
            return _send(self, 200, json.dumps({"result": {"data": {"uin": uin, "name": d["name"], "price": d["price"],
                                                                     "addToCart": 1}, "bms": bms}}), "application/json")
        return _send(self, 404, "nope")


class Blocked(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        _send(self, 403, "<html><body>Attention Required! | Cloudflare</body></html>")


SITES = {
    "traklin": ("127.0.0.10", Traklin),
    "magento": ("127.0.0.11", Magento),
    "woo": ("127.0.0.12", Woo),
    "custom": ("127.0.0.13", Custom),
    "ksp": ("127.0.0.14", Ksp),
    "blocked": ("127.0.0.15", Blocked),
}


def start(port: int = 8765) -> dict[str, str]:
    urls = {}
    for name, (ip, handler) in SITES.items():
        srv = ThreadingHTTPServer((ip, port), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        urls[name] = f"http://{ip}:{port}"
    return urls
