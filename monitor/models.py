"""Model-number handling: extract, normalize, and match across stores.

Matching is done on the manufacturer model (e.g. "UE75U8000FUXIL", "GR-728B"),
the only identifier shared between retailers. Two levels:
  * exact   – the same model appears as a standalone token on the page
  * similar – a near-identical model (one letter apart, e.g. FUXIL vs HUXIL),
              reported separately so it is never mistaken for an exact match.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import unquote

SEP = r"[\s\-_/\.]?"
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9](?:[A-Za-z0-9]|[\-/\.](?=[A-Za-z0-9])){3,30}(?![A-Za-z0-9])")
MODEL_AFTER_LABEL = re.compile(
    r"(?:דגם|מק\"ט|מק״ט|מקט|model)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/\.]{2,30}[A-Za-z0-9])",
    re.IGNORECASE,
)
# Words that look like tokens but are never model numbers
STOPWORDS = {
    "SMART", "SAMSUNG", "OLED", "QLED", "QNED", "NANOCELL", "INVERTER", "WIFI", "HDMI",
    "ANDROID", "GOOGLE", "CRYSTAL", "ULTRA", "PREMIUM", "DIGITAL", "BLACK", "WHITE",
    "SILVER", "INOX", "STEEL", "GLASS", "PLUS", "SERIES", "LITER", "WATT", "PHILIPS",
    "BOSCH", "SIEMENS", "ELECTRA", "TORNADO", "TADIRAN", "HAIER", "SHARP", "TOSHIBA",
    "HISENSE", "XIAOMI", "DYSON", "BRAUN", "DELONGHI", "KENWOOD", "TEFAL", "NINJA",
    "SONY", "TCL", "LG", "AMCOR", "CROSS", "BEKO", "MIELE", "GORENJE", "FUJICOM",
    "WHIRLPOOL", "CANDY", "HOOVER", "ROSIERES", "BLOMBERG", "NESPRESSO", "KITCHENAID",
}


def normalize(model: str) -> str:
    """'GR-728B ' -> 'GR728B'"""
    return re.sub(r"[^A-Za-z0-9]", "", model or "").upper()


def looks_like_model(token: str) -> bool:
    n = normalize(token)
    if len(n) < 4 or len(n) > 30:
        return False
    if n in STOPWORDS:
        return False
    has_d = any(c.isdigit() for c in n)
    has_a = any(c.isalpha() for c in n)
    if not (has_d and has_a):
        # pure numbers like 4K/2025 are not models; pure words neither
        return False
    # reject things like "4K", "8K", "75INCH", "12000BTU", "665L"
    if re.fullmatch(r"\d+(K|L|W|BTU|INCH|GB|TB|MM|CM|HZ|KG)", n):
        return False
    return True


def extract_model(title: str, url: str = "") -> str | None:
    """Best-effort model extraction from a product title (and its URL slug)."""
    title = title or ""
    m = MODEL_AFTER_LABEL.search(title)
    if m and looks_like_model(m.group(1)):
        return m.group(1).strip(".-/").upper()
    slug = unquote(url or "")
    m = re.search(r"_דגם_([A-Za-z0-9][A-Za-z0-9\-\.]+)", slug, re.IGNORECASE)
    if m and looks_like_model(m.group(1)):
        return m.group(1).strip(".-").upper()
    # Fall back: the longest letter+digit token in the title
    cands = [t for t in TOKEN_RE.findall(title) if looks_like_model(t)]
    if not cands:
        return None
    cands.sort(key=lambda t: (sum(c.isdigit() for c in t) > 0, len(normalize(t))), reverse=True)
    return cands[0].upper()


def model_from_label(text: str) -> str | None:
    """Only trust an explicit 'דגם: X' / 'מק"ט: X' label in free page text."""
    for m in MODEL_AFTER_LABEL.finditer(text or ""):
        if looks_like_model(m.group(1)):
            return m.group(1).strip(".-/").upper()
    return None


def model_regex(model: str) -> re.Pattern:
    """Regex that finds the model as a standalone token, tolerating separators.
    'GR-728B' matches 'GR728B', 'GR 728B', 'gr-728b' but not 'GR-728BS'."""
    n = normalize(model)
    body = SEP.join(re.escape(c) for c in n)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.IGNORECASE)


def find_exact(model: str, text: str) -> bool:
    return bool(text) and bool(model_regex(model).search(text))


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def find_similar(model: str, text: str, threshold: float = 0.86) -> str | None:
    """Return the most similar model-like token in text, if close enough.
    Requires identical digits and the same first 4 characters, so different
    sizes/capacities (55" vs 65", 600L vs 700L) are never paired."""
    target = normalize(model)
    if len(target) < 6:
        return None
    best, best_score = None, 0.0
    seen = set()
    for tok in TOKEN_RE.findall(text or ""):
        n = normalize(tok)
        if n in seen or not looks_like_model(tok):
            continue
        seen.add(n)
        if n == target or abs(len(n) - len(target)) > 2:
            continue
        if n[:4] != target[:4] or _digits(n) != _digits(target):
            continue
        s = similarity(n, target)
        if s >= threshold and s > best_score:
            best, best_score = tok.upper(), s
    return best
