"""Pure extractors for hiring and dated-news signals on a company-owned site.

Nothing here performs a request. Callers pass HTML that was fetched from a site which already
passed the exact-identity gate, and decide separately what to publish.
"""
from __future__ import annotations

import html as html_module
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import tldextract
from bs4 import BeautifulSoup

CAREER_TOKENS = {
    "karriere", "karrierer", "career", "careers", "jobb", "jobs", "job", "stilling", "stillinger",
    "vacancies", "vacancy", "rekruttering", "recruitment", "ledige", "jobbmuligheter",
}
CAREER_PHRASES = ("work-with-us", "join-us", "bli-med", "jobbe-hos-oss", "jobb-hos-oss", "jobb-i-", "jobbe-i-", "open-positions")
CAREER_TEXT = (
    "karriere", "ledige stillinger", "ledig stilling", "jobb hos oss", "jobbe hos oss", "jobb i ", "jobbe i ",
    "careers", "career", "vacancies", "open positions", "join us", "work with us", "bli en av oss",
    "bli med på laget", "søk jobb", "stillinger", "jobb med oss", "jobbe med oss", "vil du jobbe",
)
NEWS_TOKENS = {
    "nyheter", "nyhet", "aktuelt", "news", "presse", "press", "pressemeldinger", "pressemelding",
    "blogg", "blog", "artikler", "artikkel", "siste-nytt", "nytt", "newsroom", "media",
}
NEWS_TEXT = ("nyheter", "aktuelt", "news", "presse", "blogg", "blog", "artikler", "siste nytt")
# Hosted recruiting sites that a company links to as its own careers page.
ATS_SUFFIXES = (
    "teamtailor.com", "recman.no", "recman.page", "webcruiter.no", "webcruiter.com", "hr-manager.net",
    "easycruit.com", "varbi.com", "jobylon.com", "reachmee.com", "talentech.io", "workable.com",
    "greenhouse.io", "lever.co", "smartrecruiters.com", "myworkdayjobs.com", "jobbnorge.no",
)
PLACEHOLDER_TITLES = {
    "hello world", "hello world!", "hei verden", "hei verden!", "hei, verden!", "sample page", "eksempelside",
    "uncategorized", "ukategorisert", "untitled", "uten tittel", "test",
}
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
ARTICLE_TYPES = {"NewsArticle", "Article", "BlogPosting", "PressRelease", "Report", "TechArticle"}


def registered_domain(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    return tldextract.extract(host).top_domain_under_public_suffix


def clean_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))


def _squash(text: Any) -> str:
    return " ".join(html_module.unescape(str(text or "")).split())


def _path_tokens(path: str) -> list[str]:
    return [token for token in re.split(r"[/_\-.]+", urllib.parse.unquote(path).casefold()) if token]


def _anchors(base_url: str, soup: BeautifulSoup) -> list[tuple[str, str]]:
    found = []
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            url = urllib.parse.urljoin(base_url, href)
        except ValueError:
            continue
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            continue
        found.append((clean_url(url), _squash(anchor.get_text(" ", strip=True))[:120]))
    return found


def career_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    """Careers-page candidates linked from a company page, best first."""
    site = registered_domain(base_url)
    ranked: dict[str, tuple[int, int, str, str]] = {}
    for url, text in _anchors(base_url, soup):
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").casefold()
        domain = registered_domain(url)
        tokens = _path_tokens(parsed.path)
        path = parsed.path.casefold()
        lowered = text.casefold()
        text_hit = bool(lowered) and len(lowered) <= 40 and any(term in lowered for term in CAREER_TEXT)
        path_hit = bool(set(tokens) & CAREER_TOKENS) or any(phrase in path for phrase in CAREER_PHRASES)
        host_hit = bool(set(host.split(".")[:-2]) & CAREER_TOKENS)
        if domain == site:
            if not (path_hit or host_hit or text_hit) or set(tokens) & NEWS_TOKENS:
                continue
            if len([part for part in parsed.path.split("/") if part]) > 3 or re.search(r"\.(pdf|docx?|jpg|png)$", path):
                continue
            kind = "company_site"
            rank = 0 if (path_hit or host_hit) and text_hit else 1 if (path_hit or host_hit) else 2
        elif any(domain == suffix or host.endswith("." + suffix) for suffix in ATS_SUFFIXES):
            if not (text_hit or path_hit) or host.split(".")[0] in {"www", "app", "login", "id"}:
                continue
            kind = "hosted_careers_site"
            rank = 3
        else:
            continue
        key = (rank, len(parsed.path), url, kind)
        if url not in ranked or key < ranked[url]:
            ranked[url] = key
    ordered = sorted(ranked.values())
    return [{"url": url, "kind": kind} for _, _, url, kind in ordered[:5]]


def careers_page_proof(soup: BeautifulSoup) -> str | None:
    """Return the page's own heading or title when it names careers or vacancies."""
    candidates = [node.get_text(" ", strip=True) for node in soup.select("h1, h2")[:8]]
    if soup.title:
        candidates.append(soup.title.get_text(" ", strip=True))
    for candidate in candidates:
        text = _squash(candidate)
        lowered = text.casefold()
        if text and (any(term in lowered for term in CAREER_TEXT) or re.search(r"\b(jobb|stilling|job)\b", lowered)):
            return text[:300]
    return None


def news_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    """News or blog listing pages on the same site, best first."""
    site = registered_domain(base_url)
    ranked: dict[str, tuple[int, int, str]] = {}
    for url, text in _anchors(base_url, soup):
        parsed = urllib.parse.urlparse(url)
        if registered_domain(url) != site:
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or len(parts) > 2 or parsed.query:
            continue
        tokens = set(_path_tokens(parsed.path))
        lowered = text.casefold()
        path_hit = bool(tokens & NEWS_TOKENS)
        text_hit = bool(lowered) and len(lowered) <= 30 and any(term in lowered for term in NEWS_TEXT)
        if not (path_hit or text_hit):
            continue
        key = (0 if path_hit and text_hit else 1 if path_hit else 2, len(parsed.path), url)
        if url not in ranked or key < ranked[url]:
            ranked[url] = key
    return [url for _, _, url in sorted(ranked.values())[:3]]


def feed_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Feeds the site itself declares, limited to its own domain and excluding comment feeds."""
    site = registered_domain(base_url)
    found = []
    for node in soup.select('link[rel~="alternate"][href]'):
        kind = str(node.get("type") or "").casefold()
        if "rss" not in kind and "atom" not in kind:
            continue
        url = urllib.parse.urljoin(base_url, str(node.get("href")).strip())
        lowered = url.casefold()
        if registered_domain(url) != site or "comment" in lowered or "kommentar" in lowered:
            continue
        if url not in found:
            found.append(url)
    return sorted(found, key=lambda item: (len(item), item))[:2]


def normalize_datetime(raw: Any) -> str | None:
    """Keep a source timestamp as written when it is ISO 8601; convert RFC 822 feed dates."""
    text = _squash(raw)
    if not text:
        return None
    if ISO_DATETIME.match(text):
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return None
        return text.replace(" ", "T", 1)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def datetime_sort_key(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def is_placeholder_title(title: str) -> bool:
    lowered = _squash(title).casefold().strip(" .!")
    return not lowered or len(lowered) < 4 or lowered in {item.strip(" .!") for item in PLACEHOLDER_TITLES} or "lorem ipsum" in lowered


def _walk_jsonld(node: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        kinds = node.get("@type")
        kinds = set(kinds if isinstance(kinds, list) else [kinds])
        if kinds & ARTICLE_TYPES:
            found.append(node)
        for child in node.values():
            found.extend(_walk_jsonld(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_walk_jsonld(child))
    return found


def jsonld_blocks(soup: BeautifulSoup) -> list[Any]:
    blocks = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            blocks.append(json.loads(node.string or node.get_text() or ""))
        except (ValueError, TypeError):
            continue
    return blocks


def article_meta(page_url: str, page_html: str, soup: BeautifulSoup) -> dict[str, str] | None:
    """Title and publication time stated by an article page itself, with the verbatim date string."""
    title = ""
    published_raw = ""
    method = ""
    for item in _walk_jsonld(jsonld_blocks(soup)):
        raw = item.get("datePublished")
        if isinstance(raw, str) and normalize_datetime(raw):
            published_raw, method = raw.strip(), "json_ld_datePublished"
            headline = item.get("headline") or item.get("name")
            if isinstance(headline, str):
                title = _squash(headline)
            break
    if not published_raw:
        node = soup.select_one('meta[property="article:published_time"], meta[name="article:published_time"], meta[itemprop="datePublished"], meta[name="date"], meta[property="og:article:published_time"]')
        raw = str(node.get("content") or "").strip() if node else ""
        if raw and normalize_datetime(raw):
            published_raw, method = raw, "meta_published_time"
    if not published_raw:
        node = soup.select_one("article time[datetime], main time[datetime], time[itemprop='datePublished'][datetime], time[datetime]")
        raw = str(node.get("datetime") or "").strip() if node else ""
        if raw and normalize_datetime(raw):
            published_raw, method = raw, "time_datetime"
    if not published_raw or published_raw not in page_html:
        return None
    if not title:
        heading = soup.select_one("article h1, main h1, h1")
        title = _squash(heading.get_text(" ", strip=True)) if heading else ""
    if not title:
        node = soup.select_one('meta[property="og:title"]')
        title = _squash(node.get("content")) if node else ""
    if is_placeholder_title(title):
        return None
    return {"url": page_url, "title": title[:300], "published_raw": published_raw, "published_at": normalize_datetime(published_raw) or "", "method": method}


def listing_items(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    """Dated article links on a listing page: an article-like block with a <time datetime> and a same-site link."""
    site = registered_domain(base_url)
    base_clean = clean_url(base_url).rstrip("/")
    items: dict[str, dict[str, str]] = {}
    for time_node in soup.select("time[datetime]"):
        published = normalize_datetime(time_node.get("datetime"))
        if not published:
            continue
        block = time_node
        link = None
        for _ in range(4):
            block = block.parent
            if block is None or block.name in {"body", "html", "main"}:
                break
            anchors = [node for node in block.select("a[href]") if node.get("href")]
            hrefs = {clean_url(urllib.parse.urljoin(base_url, str(node.get("href")))) for node in anchors}
            if len(hrefs) == 1 or (anchors and block.name in {"article", "li"}):
                link = anchors[0]
                heading = block.select_one("h1 a[href], h2 a[href], h3 a[href], h4 a[href]")
                link = heading or link
                break
        if link is None:
            continue
        url = clean_url(urllib.parse.urljoin(base_url, str(link.get("href"))))
        if registered_domain(url) != site or url.rstrip("/") == base_clean:
            continue
        heading = block.select_one("h1, h2, h3, h4") if block is not None else None
        title = _squash((heading or link).get_text(" ", strip=True))
        if is_placeholder_title(title):
            continue
        items.setdefault(url, {"url": url, "title": title[:300], "published_at": published})
    return sorted(items.values(), key=lambda item: (datetime_sort_key(item["published_at"]), item["url"]), reverse=True)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def parse_feed(feed_url: str, body: bytes) -> list[dict[str, str]]:
    """RSS/Atom items as {url, title, published_at, published_raw}, newest first, same site only."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    site = registered_domain(feed_url)
    items: dict[str, dict[str, str]] = {}
    for node in root.iter():
        if _local(node.tag) not in {"item", "entry"}:
            continue
        title = link = raw_date = ""
        for child in node:
            name = _local(child.tag)
            if name == "title" and not title:
                title = _squash("".join(child.itertext()))
            elif name == "link" and not link:
                link = (child.get("href") or child.text or "").strip()
            elif name in {"pubdate", "published", "date"} and not raw_date:
                raw_date = (child.text or "").strip()
            elif name == "updated" and not raw_date:
                raw_date = (child.text or "").strip()
        published = normalize_datetime(raw_date)
        if not link or not published or is_placeholder_title(title):
            continue
        url = clean_url(urllib.parse.urljoin(feed_url, link))
        if registered_domain(url) != site:
            continue
        items.setdefault(url, {"url": url, "title": title[:300], "published_at": published, "published_raw": raw_date})
    return sorted(items.values(), key=lambda item: (datetime_sort_key(item["published_at"]), item["url"]), reverse=True)


def within_days(published_at: str, reference: datetime, days: int) -> bool:
    try:
        parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = (reference - parsed).total_seconds() / 86400
    return -2 <= age <= days


PROOF_TERMS = (
    "kontakt", "contact", "om-oss", "om_oss", "omoss", "about", "personvern", "privacy", "vilkar", "vilkår",
    "betingelser", "terms", "salgsbetingelser", "kjopsbetingelser", "kjøpsbetingelser", "impressum", "firmainfo",
)
POST_SITEMAP_HINTS = ("post", "news", "nyhet", "aktuelt", "artik", "blog", "press")
# Dated pages that are not news: staff profiles, products, taxonomy and author archives.
NON_ARTICLE_TOKENS = {
    "ansatte", "ansatt", "medarbeidere", "medarbeider", "team", "people", "employees", "staff", "author", "forfatter",
    "produkt", "produkter", "product", "products", "kategori", "category", "tag", "tags", "butikk", "shop", "tjenester",
    "services", "prosjekter", "prosjekt", "projects", "referanser", "kunder", "stillinger", "jobb", "karriere",
}


def is_post_sitemap(url: str) -> bool:
    """True for a child sitemap of blog or news posts; WordPress `posts-<type>` maps count only for type `post`."""
    name = urllib.parse.urlparse(url).path.casefold()
    typed = re.search(r"posts-([a-z0-9_]+)-\d+\.xml", name)
    if typed:
        return typed.group(1) == "post"
    return any(hint in name for hint in POST_SITEMAP_HINTS)


def proof_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Same-site pages that usually state who runs the site: contact, about, privacy and terms."""
    site = registered_domain(base_url)
    ranked: dict[str, tuple[int, int, str]] = {}
    for url, text in _anchors(base_url, soup):
        parsed = urllib.parse.urlparse(url)
        if registered_domain(url) != site or len([part for part in parsed.path.split("/") if part]) > 2:
            continue
        haystack = urllib.parse.unquote(parsed.path).casefold() + " " + text.casefold()
        rank = next((index for index, term in enumerate(PROOF_TERMS) if term in haystack), None)
        if rank is None:
            continue
        key = (rank, len(parsed.path), url)
        if url not in ranked or key < ranked[url]:
            ranked[url] = key
    return [url for _, _, url in sorted(ranked.values())]


def parse_sitemap(sitemap_url: str, body: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Return (child sitemap URLs, page entries) from a sitemap or sitemap index, same site only."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [], []
    site = registered_domain(sitemap_url)
    children: list[str] = []
    entries: dict[str, dict[str, str]] = {}
    for node in root.iter():
        kind = _local(node.tag)
        if kind not in {"sitemap", "url"}:
            continue
        location = lastmod = ""
        for child in node:
            name = _local(child.tag)
            if name == "loc":
                location = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = (child.text or "").strip()
        if not location or registered_domain(location) != site:
            continue
        if kind == "sitemap":
            children.append(location)
        else:
            entries.setdefault(clean_url(location), {"url": clean_url(location), "lastmod": normalize_datetime(lastmod) or ""})
    return sorted(set(children)), [entries[key] for key in sorted(entries)]


def post_like_sitemaps(children: list[str]) -> list[str]:
    """Child sitemaps most likely to list dated posts, best first, then the rest."""
    def rank(url: str) -> tuple[int, str]:
        return (0 if is_post_sitemap(url) else 1, url)

    return sorted(children, key=rank)


def sitemap_career_urls(entries: list[dict[str, str]]) -> list[str]:
    ranked = []
    for entry in entries:
        parsed = urllib.parse.urlparse(entry["url"])
        parts = [part for part in parsed.path.split("/") if part]
        tokens = set(_path_tokens(parsed.path))
        path = parsed.path.casefold()
        if not parts or len(parts) > 2 or tokens & NEWS_TOKENS:
            continue
        if tokens & CAREER_TOKENS or any(phrase in path for phrase in CAREER_PHRASES):
            ranked.append((len(parts), len(parsed.path), entry["url"]))
    return [url for _, _, url in sorted(ranked)[:3]]


def sitemap_article_urls(entries: list[dict[str, str]], *, from_post_sitemap: bool) -> list[dict[str, str]]:
    """Dated article candidates: entries with a lastmod that sit under a news path, or come from a post sitemap."""
    found = []
    for entry in entries:
        if not entry["lastmod"]:
            continue
        parsed = urllib.parse.urlparse(entry["url"])
        parts = [part for part in parsed.path.split("/") if part]
        tokens = set(_path_tokens(parsed.path))
        under_news = bool(tokens & NEWS_TOKENS) and len(parts) >= 2
        if not parts or not (under_news or from_post_sitemap) or tokens & NON_ARTICLE_TOKENS:
            continue
        found.append({"url": entry["url"], "title": "", "published_at": entry["lastmod"]})
    return sorted(found, key=lambda item: (datetime_sort_key(item["published_at"]), item["url"]), reverse=True)
