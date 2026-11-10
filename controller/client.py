"""HTTP client for the simulator. Retries transport errors and 5xx with backoff; 4xx are
returned to the caller as ApiErr because they carry meaning (409 stale, 410 lapsed, 402 credits)."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("ctl.client")


class ApiErr(Exception):
    def __init__(self, status: int, code: str, detail=None):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class Sim:
    def __init__(self, url: str, retries: int = 8):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=httpx.Timeout(300.0, connect=10.0))
        self.retries = retries

    def _req(self, method: str, path: str, **kw):
        delay = 0.5
        for attempt in range(self.retries + 1):
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise
                log.warning("transport error %s %s: %s (retry in %.1fs)", method, path, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code >= 500:
                if attempt == self.retries:
                    raise ApiErr(r.status_code, "server_error", r.text[:300])
                log.warning("%s %s -> %s (retry in %.1fs)", method, path, r.status_code, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code >= 400:
                try:
                    j = r.json()
                except ValueError:
                    j = {}
                raise ApiErr(r.status_code, j.get("error") or str(j.get("detail") or "error"), j.get("detail"))
            return r
        raise RuntimeError("unreachable")

    def get(self, path: str, **params):
        return self._req("GET", path, params=params or None).json()

    def post(self, path: str, body: dict):
        return self._req("POST", path, json=body).json()

    def put(self, path: str, body: dict):
        return self._req("PUT", path, json=body).json()

    def chunk_text(self, n: int) -> bytes:
        return self._req("GET", f"/events/chunks/{n}").content

    def wait_ready(self, timeout_s: float = 900):
        t0 = time.time()
        while True:
            try:
                self.http.get("/health").raise_for_status()
                return
            except Exception:
                if time.time() - t0 > timeout_s:
                    raise
                time.sleep(1.0)
