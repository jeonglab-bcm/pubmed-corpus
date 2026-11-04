"""Polite HTTP: per-host rate limits, retries, and an on-disk cache of raw responses.

Every response body is cached gzipped under the project's ``cache/`` directory,
keyed by method, URL and parameters, so an interrupted step resumes without
re-downloading and a finished step can be replayed offline. ``refresh=True``
bypasses the cache for one client.

Tests replace the network with ``Http(fetch=...)``: a callable taking
``(method, url, params)`` and returning bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

USER_AGENT = "pubmed-corpus/0.1 (+https://github.com/jeonglab-bcm/pubmed-corpus)"
RETRY_STATUS = {429, 500, 502, 503, 504}
Fetch = Callable[[str, str, dict], bytes]


class HttpError(RuntimeError):
    pass


class Http:
    def __init__(
        self,
        cache_dir: Path | None = None,
        *,
        intervals: dict[str, float] | None = None,
        retries: int = 4,
        timeout: float = 90.0,
        refresh: bool = False,
        fetch: Fetch | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.intervals = intervals or {}
        self.retries = retries
        self.timeout = timeout
        self.refresh = refresh
        self._fetch_override = fetch
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self.network_calls = 0

    # -- public ---------------------------------------------------------
    def request(self, url: str, params: dict | None = None, *, method: str = "GET", cache: bool = True) -> bytes:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        path = self._cache_path(method, url, params) if cache else None
        if path is not None and path.exists() and not self.refresh:
            return gzip.decompress(path.read_bytes())
        body = self._fetch(method, url, params)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(body, mtime=0))
            tmp.replace(path)
        return body

    def get_json(self, url: str, params: dict | None = None, *, method: str = "GET", cache: bool = True):
        return json.loads(self.request(url, params, method=method, cache=cache))

    # -- internals ------------------------------------------------------
    def _cache_path(self, method: str, url: str, params: dict) -> Path | None:
        if self.cache_dir is None:
            return None
        blob = json.dumps([method, url, sorted((k, str(v)) for k, v in params.items())])
        key = hashlib.sha256(blob.encode()).hexdigest()
        return self.cache_dir / "http" / key[:2] / f"{key}.gz"

    def _throttle(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        gap = self.intervals.get(host, 0.0)
        if gap:
            wait = self._last.get(host, 0.0) + gap - time.monotonic()
            if wait > 0:
                self._sleep(wait)
        self._last[host] = time.monotonic()

    def _fetch(self, method: str, url: str, params: dict) -> bytes:
        self.network_calls += 1
        if self._fetch_override is not None:
            return self._fetch_override(method, url, params)
        data = None
        full = url
        if method == "GET":
            if params:
                full = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        else:
            data = urllib.parse.urlencode(params, doseq=True).encode()
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(url)
            req = urllib.request.Request(full, data=data, method=method, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY_STATUS:
                    raise HttpError(f"HTTP {exc.code} for {full[:300]}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
            if attempt < self.retries:
                self._sleep(2 ** (attempt + 1))
        raise HttpError(f"giving up after {self.retries + 1} attempts: {full[:300]}: {last_error}")
