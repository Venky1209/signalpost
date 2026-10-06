from __future__ import annotations

import json
import hashlib
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


@dataclass
class FetchResult:
    url: str
    status: int
    elapsed_ms: int
    bytes_received: int
    body: Any = None
    error: str | None = None
    content_sha256: str | None = None
    retrieved_at: str | None = None
    effective_at: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _cache_file(url: str) -> Path | None:
    directory = os.environ.get("SIGNALPOST_HTTP_CACHE") or ""
    if not directory:
        return None
    digest = hashlib.sha256(("json\n" + url).encode()).hexdigest()
    return Path(directory) / "registry" / digest[:2] / f"{digest}.json"


def fetch_json(url: str, *, timeout: float = 20.0, attempts: int = 5) -> FetchResult:
    """GET a registry JSON document. SIGNALPOST_HTTP_CACHE replays earlier answers in local development."""
    cache = _cache_file(url)
    if cache is not None and cache.exists():
        try:
            return FetchResult(**json.loads(cache.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
    result = _fetch_json(url, timeout=timeout, attempts=attempts)
    if cache is not None and result.status in {200, 404, 410}:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(f".{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps(asdict(result)), encoding="utf-8")
        temporary.replace(cache)
    return result


def _fetch_json(url: str, *, timeout: float = 20.0, attempts: int = 5) -> FetchResult:
    last_error = "request failed"
    for attempt in range(attempts):
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                elapsed = int((time.monotonic() - started) * 1000)
                return FetchResult(url, response.status, elapsed, len(raw), json.loads(raw), content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
        except urllib.error.HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raw = exc.read()
            if exc.code in {404, 410}:
                return FetchResult(url, exc.code, elapsed, len(raw), error=f"HTTP {exc.code}", content_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=_utc_now())
            last_error = f"HTTP {exc.code}"
            if exc.code == 429:
                # Rate limited: wait as asked (bounded) so the company keeps its official record.
                try:
                    time.sleep(min(15.0, float(exc.headers.get("Retry-After") or 2.0)))
                except (TypeError, ValueError):
                    time.sleep(2.0)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = type(exc).__name__
        if attempt + 1 < attempts:
            time.sleep(0.4 * (2**attempt))
    return FetchResult(url, 0, 0, 0, error=last_error, retrieved_at=_utc_now())
