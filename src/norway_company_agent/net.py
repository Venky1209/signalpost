"""Single outbound HTTP gate for company sites and open feeds.

Every non-registry request goes through `fetch`: identifying User-Agent, public-address check on
each hop, one cached robots.txt read per host, bounded retries with backoff, and per-company
request accounting. Set SIGNALPOST_HTTP_CACHE to a directory to replay earlier responses during
local development; the official command never sets it.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USER_AGENT = os.environ.get("SIGNALPOST_USER_AGENT", "builderr-signalpost-poc/0.1 (+https://builderr.ai)")
RETRY_STATUSES = {429, 500, 502, 503, 504}
_CACHE_DIR = os.environ.get("SIGNALPOST_HTTP_CACHE") or ""

_META_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)

_local = threading.local()
_robots_lock = threading.Lock()
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_dns_lock = threading.Lock()
_dns: dict[tuple[str, int], bool] = {}


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    error: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").lower()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self) -> str:
        """Decode with the declared charset; older Norwegian sites still serve ISO-8859-1."""
        declared = ""
        if "charset=" in self.content_type:
            declared = self.content_type.split("charset=", 1)[1].split(";")[0].strip().strip("\"'")
        if not declared:
            match = _META_CHARSET.search(self.body[:4096])
            declared = match.group(1).decode("ascii", "ignore") if match else ""
        for encoding in (declared, "utf-8"):
            if not encoding:
                continue
            try:
                return self.body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("cp1252", errors="replace")


@dataclass
class Meter:
    requests: int = 0
    bytes: int = 0
    latencies_ms: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "bytes": self.bytes, "latencies_ms": list(self.latencies_ms)}


def start_meter() -> Meter:
    _local.meter = Meter()
    return _local.meter


def current_meter() -> Meter:
    meter = getattr(_local, "meter", None)
    return meter if meter is not None else start_meter()


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with _dns_lock:
        known = _dns.get((host, port))
    if known is True:
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except (socket.gaierror, UnicodeError) as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")
    with _dns_lock:
        _dns[(host, port)] = True


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def _cache_path(url: str, accept: str) -> Path | None:
    if not _CACHE_DIR:
        return None
    digest = hashlib.sha256(f"{accept}\n{url}".encode()).hexdigest()
    return Path(_CACHE_DIR) / digest[:2] / f"{digest}.json"


def _cache_read(path: Path | None) -> Response | None:
    if path is None or not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        return Response(row["url"], row["final_url"], row["status"], row["headers"], base64.b64decode(row["body"]), row["elapsed_ms"], row.get("error"), row.get("truncated", False))
    except (OSError, ValueError, KeyError):
        return None


def _cache_write(path: Path | None, response: Response) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "url": response.url, "final_url": response.final_url, "status": response.status, "headers": response.headers,
        "body": base64.b64encode(response.body).decode(), "elapsed_ms": response.elapsed_ms, "error": response.error,
        "truncated": response.truncated,
    }
    temporary = path.with_suffix(f".{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(row), encoding="utf-8")
    temporary.replace(path)


def fetch(
    url: str,
    *,
    accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
    attempts: int = 2,
    retry_errors: bool = False,
    headers: dict[str, str] | None = None,
) -> Response:
    """GET one public URL. Never raises; failures come back as status 0 with `error` set."""
    cache = _cache_path(url, accept)
    cached = _cache_read(cache)
    meter = current_meter()
    if cached is not None:
        meter.requests += 1
        meter.bytes += len(cached.body)
        return cached
    response = Response(url, url, 0, {}, b"", 0, "request failed")
    for attempt in range(attempts):
        started = time.monotonic()
        retry_after = 0.0
        try:
            assert_public_url(url)
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "nb,no;q=0.9,en;q=0.7", **(headers or {})})
            with SAFE_OPENER.open(request, timeout=timeout) as raw:
                body = raw.read(max_bytes + 1)
                response = Response(
                    url, raw.geturl(), int(raw.status), {key.lower(): value for key, value in raw.headers.items()},
                    body[:max_bytes], int((time.monotonic() - started) * 1000), None, len(body) > max_bytes,
                )
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(200_000)
            except Exception:
                body = b""
            response = Response(url, exc.geturl() or url, int(exc.code), {key.lower(): value for key, value in (exc.headers or {}).items()}, body, int((time.monotonic() - started) * 1000), f"HTTP {exc.code}")
            try:
                retry_after = min(5.0, float(response.headers.get("retry-after", "0") or 0))
            except ValueError:
                retry_after = 0.0
        except ValueError as exc:
            response = Response(url, url, 0, {}, b"", int((time.monotonic() - started) * 1000), f"blocked: {exc}")
            break
        except Exception as exc:  # URLError, timeouts, TLS and protocol errors
            reason = getattr(exc, "reason", exc)
            response = Response(url, url, 0, {}, b"", int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(reason)[:160]}")
        meter.requests += 1
        meter.bytes += len(response.body)
        meter.latencies_ms.append(response.elapsed_ms)
        if response.status and response.status not in RETRY_STATUSES:
            break
        if not response.status and not retry_errors:
            break
        if attempt + 1 < attempts:
            time.sleep(max(retry_after, 0.6 * (2**attempt)))
    _cache_write(cache, response)
    return response


def robots_allowed(url: str, *, timeout: float = 10.0) -> bool:
    """One robots.txt read per scheme+host. A missing or unreadable file allows ordinary GETs."""
    parsed = urllib.parse.urlparse(url)
    key = f"{parsed.scheme}://{parsed.netloc.lower()}"
    with _robots_lock:
        known = key in _robots
        parser = _robots.get(key)
    if not known:
        parser = None
        response = fetch(key + "/robots.txt", accept="text/plain,*/*;q=0.5", timeout=timeout, max_bytes=500_000, attempts=1)
        if response.ok and "html" not in response.content_type:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(response.text().splitlines())
        with _robots_lock:
            _robots[key] = parser
    if parser is None:
        return True
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def robots_sitemaps(url: str, *, timeout: float = 10.0) -> list[str]:
    """Sitemap URLs the site declares in robots.txt (read once per host, shared with `robots_allowed`)."""
    robots_allowed(url, timeout=timeout)
    parsed = urllib.parse.urlparse(url)
    with _robots_lock:
        parser = _robots.get(f"{parsed.scheme}://{parsed.netloc.lower()}")
    try:
        return sorted(parser.site_maps() or []) if parser is not None else []
    except Exception:
        return []
