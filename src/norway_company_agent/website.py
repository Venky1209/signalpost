from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence
from .net import SAFE_OPENER, USER_AGENT, Response, assert_public_url, current_meter, fetch, robots_allowed  # noqa: F401

SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter",
)
SOURCE_TYPE = "registry_linked_company_website"


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        try:
            url = urllib.parse.urljoin(base_url, candidate)
            parsed_candidate = urllib.parse.urlparse(url)
        except ValueError:
            continue
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def _social_raw(base_url: str, soup: BeautifulSoup) -> dict[str, str]:
    """Map each canonical social URL on a page to the href exactly as the page writes it."""
    raw_by_url: dict[str, str] = {}
    for node in soup.select("a[href]"):
        raw = str(node.get("href") or "").strip()
        try:
            normalized = normalize_social_url(urllib.parse.urljoin(base_url, raw))
        except ValueError:
            continue
        if normalized:
            raw_by_url.setdefault(normalized["url"], raw)
    return raw_by_url


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        try:
            url = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if term in haystack), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        candidates[clean] = min(rank, candidates.get(clean, rank))
    return [url for url, _ in sorted(candidates.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def _identity_text(soup: BeautifulSoup) -> str:
    nodes = soup.select('footer, address, [itemprop="legalName"], [itemprop="address"], [itemprop="telephone"], [itemprop="email"]')
    return " ".join(" ".join(node.get_text(" ", strip=True) for node in nodes).split())[:3000]


@dataclass
class SiteCrawl:
    """A website evidence record plus the parsed pages it was built from (not serialised)."""

    record: dict[str, Any]
    pages: dict[str, tuple[Response, str, BeautifulSoup]] = field(default_factory=dict)
    sitemap: dict[str, Any] | None = None


def _homepage_candidates(supplied_url: str, normalized: str) -> list[str]:
    candidates = [normalized]
    parsed = urllib.parse.urlparse(normalized)
    host = parsed.netloc
    if not re.match(r"^https?://", supplied_url, re.I):
        if not host.lower().startswith("www."):
            candidates.append(urllib.parse.urlunparse(("https", "www." + host, parsed.path or "/", "", "", "")))
        candidates.append(urllib.parse.urlunparse(("http", host, parsed.path or "/", "", "", "")))
    return candidates


def _secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], str | None, tuple[Response, str, BeautifulSoup] | None]:
    if not robots_allowed(url, timeout=timeout):
        return None, [], "robots.txt disallows page", None
    response = fetch(url, timeout=timeout, max_bytes=max_bytes)
    if not response.ok:
        return None, [], response.error or f"HTTP {response.status}", None
    if response.truncated or "html" not in response.content_type:
        return None, [], "unsupported or oversized page", None
    if _registered_domain(response.final_url) != homepage_domain:
        return None, [], "redirected outside registered domain", None
    page_html = response.text()
    page_soup = BeautifulSoup(page_html, "lxml")
    page_text = trafilatura.extract(page_html, url=response.final_url, include_links=False, include_tables=False, favor_precision=True) or ""
    page = {
        "url": response.final_url,
        "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
        "main_text_excerpt": page_text[:5000],
        "identity_text_excerpt": _identity_text(page_soup),
        "content_sha256": response.sha256,
    }
    return page, _social_links(response.final_url, page_soup), None, (response, page_html, page_soup)


def crawl_site(url: str | None, *, timeout: float = 12.0, max_bytes: int = 2_000_000, source_type: str = SOURCE_TYPE) -> SiteCrawl:
    """Read a company homepage and a few same-site priority pages through the shared HTTP gate."""
    supplied_url = str(url or "").strip()
    normalized = normalize_homepage(url)
    if not normalized:
        return SiteCrawl(evidence("website", "not_found", source_type, "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"))
    response: Response | None = None
    failure = evidence("website", "source_error", source_type, normalized, note="No homepage candidate answered")
    for candidate in _homepage_candidates(supplied_url, normalized):
        try:
            assert_public_url(candidate)
        except ValueError as exc:
            status = "source_error" if "did not resolve" in str(exc) else "blocked"
            failure = evidence("website", status, source_type, candidate, note=str(exc))
            continue
        if not robots_allowed(candidate, timeout=timeout):
            failure = evidence("website", "blocked", source_type, candidate, note="robots.txt disallows this user agent")
            break
        attempt = fetch(candidate, timeout=timeout, max_bytes=max_bytes)
        if attempt.ok and attempt.truncated:
            failure = evidence("website", "blocked", source_type, candidate, note="Homepage exceeds byte limit")
            break
        if attempt.ok and "html" not in attempt.content_type:
            failure = evidence("website", "source_error", source_type, candidate, note=f"Unsupported content type: {attempt.content_type[:80]}")
            break
        if attempt.ok:
            response = attempt
            break
        if attempt.status in {404, 410}:
            failure = evidence("website", "not_found", source_type, candidate, note=f"HTTP {attempt.status}")
        elif attempt.status in {401, 403, 429}:
            failure = evidence("website", "blocked", source_type, candidate, note=f"HTTP {attempt.status}")
            break
        else:
            failure = evidence("website", "source_error", source_type, candidate, note=(attempt.error or f"HTTP {attempt.status}")[:200])
    if response is None:
        return SiteCrawl(failure)
    final_url = response.final_url
    html = response.text()
    soup = BeautifulSoup(html, "lxml")
    try:
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
    except Exception:
        structured = {}
    text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
    description = str(description_tag.get("content") or "").strip() if description_tag else ""
    organisations = _jsonld_organisations(structured)
    social = _social_links(final_url, soup) + structured_social_links(organisations)
    value = {
        "requested_url": normalized,
        "final_url": final_url,
        "registered_domain": _registered_domain(final_url),
        "title": title[:500],
        "description": description[:2000],
        "main_text_excerpt": text[:5000],
        "identity_text_excerpt": _identity_text(soup),
        "social_links": [],
        "structured_organisations": organisations,
        "content_sha256": response.sha256,
        "extraction_state": _extraction_state(text, soup),
    }
    crawl = SiteCrawl({}, {final_url: (response, html, soup)})
    social_sources = {url: {"page_url": final_url, "content_sha256": response.sha256, "raw": raw} for url, raw in _social_raw(final_url, soup).items()}
    pages = [{"url": final_url, "title": title[:500], "main_text_excerpt": text[:5000], "identity_text_excerpt": value["identity_text_excerpt"], "content_sha256": response.sha256}]
    crawl_errors = []
    homepage_domain = value["registered_domain"]
    for page_url in _priority_links(final_url, soup):
        page, page_social, page_error, parsed = _secondary_page(page_url, homepage_domain=homepage_domain, timeout=timeout, max_bytes=min(max_bytes, 1_000_000))
        if page and parsed:
            if page["url"] not in crawl.pages:
                pages.append(page)
                crawl.pages[page["url"]] = parsed
                for social_url, raw in _social_raw(page["url"], parsed[2]).items():
                    social_sources.setdefault(social_url, {"page_url": page["url"], "content_sha256": page["content_sha256"], "raw": raw})
            social.extend(page_social)
        elif page_error:
            crawl_errors.append({"url": page_url, "error": page_error})
    value["pages"] = pages
    value["social_links"] = sorted({(item["platform"], item["url"]): item for item in social}.values(), key=lambda item: (item["platform"], item["url"]))
    value["social_link_sources"] = {url: social_sources[url] for url in sorted(social_sources)}
    value["crawl_errors"] = crawl_errors
    crawl.record = evidence("website", "available", source_type, final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=response.sha256)
    return crawl


def fetch_website(url: str | None, *, timeout: float = 12.0, max_bytes: int = 2_000_000) -> tuple[dict[str, Any], dict[str, Any]]:
    meter = current_meter()
    before = (meter.requests, meter.bytes, len(meter.latencies_ms))
    crawl = crawl_site(url, timeout=timeout, max_bytes=max_bytes)
    return crawl.record, {"requests": meter.requests - before[0], "bytes": meter.bytes - before[1], "latencies_ms": meter.latencies_ms[before[2]:]}
