"""HTTP layer: browser-fingerprinted requests, per-host politeness, retries."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

log = logging.getLogger(__name__)

# Real-browser TLS/HTTP2 fingerprints. Several Israeli retailers (KSP, Payngo)
# sit behind Cloudflare/bot protection that rejects plain Python clients.
PROFILES = ["chrome136", "chrome131", "chrome124", "edge101", "safari184", "firefox144"]

BASE_HEADERS = {
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.7,en;q=0.6",
}

RETRY_STATUS = {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


@dataclass
class Response:
    url: str            # final URL after redirects
    status: int
    text: str
    headers: dict

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        import json
        return json.loads(self.text)


class HostLimiter:
    """Limits concurrency and enforces a minimum gap between requests per host."""

    def __init__(self, concurrency: int, delay: float):
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.delay = max(0.0, delay)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self):
        await self.sem.acquire()
        async with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, self.delay * 0.3))
            self._last = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        self.sem.release()


class Http:
    def __init__(self, timeout: float = 30.0, max_retries: int = 4, verify: bool = True):
        self.timeout = timeout
        self.max_retries = max_retries
        self.verify = verify
        self.limiters: dict[str, HostLimiter] = {}
        self.host_settings: dict[str, tuple[int, float]] = {}
        self._sessions: dict[str, AsyncSession] = {}
        self.stats: dict[str, dict[str, int]] = {}

    def configure_host(self, base_url: str, concurrency: int, delay: float):
        host = urlsplit(base_url).hostname or base_url
        self.host_settings[host] = (concurrency, delay)
        bare = host[4:] if host.startswith("www.") else "www." + host
        self.host_settings.setdefault(bare, (concurrency, delay))

    def _limiter(self, host: str) -> HostLimiter:
        if host not in self.limiters:
            c, d = self.host_settings.get(host, (2, 1.0))
            self.limiters[host] = HostLimiter(c, d)
        return self.limiters[host]

    def _session(self, profile: str) -> AsyncSession:
        if profile not in self._sessions:
            self._sessions[profile] = AsyncSession(impersonate=profile, verify=self.verify)
        return self._sessions[profile]

    def _stat(self, host: str, key: str):
        self.stats.setdefault(host, {}).setdefault(key, 0)
        self.stats[host][key] += 1

    async def get(self, url: str, *, headers: dict | None = None, referer: str | None = None,
                  accept_json: bool = False) -> Response | None:
        host = urlsplit(url).hostname or ""
        hdrs = dict(BASE_HEADERS)
        if accept_json:
            hdrs["Accept"] = "application/json, text/plain, */*"
        if referer:
            hdrs["Referer"] = referer
        if headers:
            hdrs.update(headers)

        last_status = None
        for attempt in range(self.max_retries + 1):
            profile = PROFILES[attempt % len(PROFILES)]
            try:
                async with self._limiter(host):
                    r = await self._session(profile).get(
                        url, headers=hdrs, timeout=self.timeout, allow_redirects=True)
                last_status = r.status_code
                if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                    self._stat(host, f"retry_{r.status_code}")
                    await asyncio.sleep(min(30, (2 ** attempt) + random.random() * 2))
                    continue
                self._stat(host, "ok" if r.status_code < 400 else f"http_{r.status_code}")
                return Response(url=str(r.url), status=r.status_code, text=r.text or "",
                                headers=dict(r.headers))
            except Exception as e:  # network errors, timeouts
                self._stat(host, "error")
                log.debug("GET %s failed (%s): %s", url, profile, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(min(30, (2 ** attempt) + random.random() * 2))
        log.debug("GET %s gave up (last status %s)", url, last_status)
        return None

    async def close(self):
        for s in self._sessions.values():
            try:
                await s.close()
            except Exception:
                pass
