"""Hiring and dated-news evidence read from a company site that passed the exact-identity gate."""
from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from . import signals
from .evidence import evidence, utc_now
from .net import Response, fetch, robots_allowed, robots_sitemaps
from .website import SiteCrawl

HIRING_SOURCE = "company_owned_careers_page"
NEWS_SOURCE = "company_owned_news"
MAX_CAREER_TRIES = 2
MAX_PROOF_PAGES = 3
MAX_SITEMAP_FETCHES = 3
MAX_ARTICLES = 3
NEWS_WINDOW_DAYS = 1095

Page = tuple[Response, str, BeautifulSoup]


def _get_page(crawl: SiteCrawl, url: str, site_domain: str, timeout: float) -> Page | None:
    """Reuse a page the crawl already holds, otherwise fetch it once from the same site."""
    wanted = signals.clean_url(url).rstrip("/")
    for known_url, page in crawl.pages.items():
        if signals.clean_url(known_url).rstrip("/") == wanted:
            return page
    if not robots_allowed(url, timeout=timeout):
        return None
    response = fetch(url, timeout=timeout, max_bytes=1_500_000)
    if not response.ok or response.truncated or "html" not in response.content_type:
        return None
    if signals.registered_domain(response.final_url) != site_domain:
        return None
    html = response.text()
    page = (response, html, BeautifulSoup(html, "lxml"))
    crawl.pages[response.final_url] = page
    return page


def read_proof_pages(crawl: SiteCrawl, *, timeout: float = 12.0) -> int:
    """Fetch a few contact, about, privacy or terms pages linked from the homepage; returns how many were added."""
    if not crawl.pages:
        return 0
    homepage = next(iter(crawl.pages.values()))
    site_domain = signals.registered_domain(homepage[0].final_url)
    known = {signals.clean_url(url).rstrip("/") for url in crawl.pages}
    added = 0
    for url in signals.proof_links(homepage[0].final_url, homepage[2]):
        if added >= MAX_PROOF_PAGES:
            break
        if signals.clean_url(url).rstrip("/") in known:
            continue
        known.add(signals.clean_url(url).rstrip("/"))
        if _get_page(crawl, url, site_domain, timeout) is not None:
            added += 1
    return added


def site_sitemap(crawl: SiteCrawl, site_domain: str, timeout: float) -> dict[str, Any]:
    """Read the site's sitemap once: entries from the page sitemap and from the first post-like child."""
    if crawl.sitemap is not None:
        return crawl.sitemap
    crawl.sitemap = {"entries": [], "post_entries": []}
    homepage_url = next(iter(crawl.pages.values()))[0].final_url
    parsed = urllib.parse.urlparse(homepage_url)
    origin = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    declared = [url for url in robots_sitemaps(homepage_url, timeout=timeout) if signals.registered_domain(url) == site_domain]
    queue = [(url, False) for url in (declared[:2] or [origin + "/sitemap.xml"])]
    fetched = 0
    seen: set[str] = set()
    while queue and fetched < MAX_SITEMAP_FETCHES:
        url, post_like = queue.pop(0)
        if url in seen or not robots_allowed(url, timeout=timeout):
            continue
        seen.add(url)
        response = fetch(url, accept="application/xml,text/xml;q=0.9,*/*;q=0.5", timeout=timeout, max_bytes=3_000_000)
        fetched += 1
        if not response.ok or signals.registered_domain(response.final_url) != site_domain:
            continue
        children, entries = signals.parse_sitemap(response.final_url, response.body)
        crawl.sitemap["post_entries" if post_like else "entries"].extend(entries[:3000])
        ordered = signals.post_like_sitemaps(children)
        hinted = [child for child in ordered if signals.is_post_sitemap(child)]
        plain = [child for child in ordered if child not in hinted]
        queue = [(child, True) for child in hinted[:1]] + [(child, False) for child in plain[:1]] + queue
    return crawl.sitemap


def _job_postings(page_url: str, soup: BeautifulSoup) -> list[dict[str, Any]]:
    postings: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kinds = node.get("@type")
            kinds = set(kinds if isinstance(kinds, list) else [kinds])
            title = node.get("title") or node.get("name")
            if "JobPosting" in kinds and isinstance(title, str) and title.strip():
                url = node.get("url") if isinstance(node.get("url"), str) else page_url
                try:
                    url = signals.clean_url(urllib.parse.urljoin(page_url, url))
                except ValueError:
                    url = page_url
                posted = signals.normalize_datetime(node.get("datePosted"))
                postings.setdefault(f"{url}|{title.strip()}", {"title": " ".join(title.split())[:300], "url": url, "date_posted": posted})
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(signals.jsonld_blocks(soup))
    return [postings[key] for key in sorted(postings)][:25]


def hiring_record(crawl: SiteCrawl, *, timeout: float = 12.0) -> dict[str, Any]:
    website = crawl.record
    value = website.get("value") or {}
    homepage_url = value.get("final_url") or website.get("source_url")
    site_domain = value.get("registered_domain") or signals.registered_domain(homepage_url)
    candidates: list[tuple[dict[str, str], str]] = []
    seen: set[str] = set()
    for page_url, (_, _, soup) in list(crawl.pages.items()):
        for candidate in signals.career_links(page_url, soup):
            if candidate["url"] not in seen:
                seen.add(candidate["url"])
                candidates.append((candidate, page_url))
    found: list[dict[str, Any]] = []
    postings: list[dict[str, Any]] = []
    tries = 0
    if not any(candidate["kind"] == "company_site" for candidate, _ in candidates):
        entries = site_sitemap(crawl, site_domain, timeout)["entries"]
        candidates = [({"url": url, "kind": "company_site"}, homepage_url) for url in signals.sitemap_career_urls(entries)] + candidates
    for candidate, _linked_from in candidates:
        if candidate["kind"] != "company_site" or tries >= MAX_CAREER_TRIES:
            continue
        tries += 1
        page = _get_page(crawl, candidate["url"], site_domain, timeout)
        if page is None:
            continue
        response, _, soup = page
        proof = signals.careers_page_proof(soup)
        if not proof:
            continue
        found.append({
            "url": signals.clean_url(response.final_url),
            "kind": "careers_page",
            "claim_span": proof,
            "source_url": response.final_url,
            "content_sha256": response.sha256,
            "retrieved_at": utc_now(),
            "extraction_method": "careers_page_heading_or_title",
        })
        postings = _job_postings(response.final_url, soup)
        for posting in postings:
            posting.update({"source_url": response.final_url, "content_sha256": response.sha256, "extraction_method": "json_ld_JobPosting"})
        break
    if not found:
        for candidate, linked_from in candidates:
            if candidate["kind"] != "hosted_careers_site":
                continue
            response = crawl.pages[linked_from][0]
            found.append({
                "url": candidate["url"],
                "kind": "hosted_careers_site",
                "claim_span": candidate["url"],
                "source_url": response.final_url,
                "content_sha256": response.sha256,
                "retrieved_at": utc_now(),
                "extraction_method": "careers_link_on_verified_company_site",
            })
            break
    if not found:
        return evidence("hiring", "not_found", HIRING_SOURCE, homepage_url, note="No careers page or vacancy was found on the verified company site")
    return evidence(
        "hiring", "available", HIRING_SOURCE, found[0]["source_url"],
        value={"signals": found, "job_postings": postings},
        note="Company-owned hiring signal. A careers page shows that the company recruits through it, not that a vacancy is open today.",
        content_sha256=found[0]["content_sha256"],
    )


def _candidate_items(crawl: SiteCrawl, site_domain: str, timeout: float) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """Dated article candidates, newest first, plus the feed they came from when a feed supplied them."""
    homepage = next(iter(crawl.pages.values()))
    feeds: list[str] = []
    for page_url, (_, _, soup) in list(crawl.pages.items()):
        for feed_url in signals.feed_links(page_url, soup):
            if feed_url not in feeds:
                feeds.append(feed_url)
    if not feeds and "wp-content" in homepage[1]:
        parsed = urllib.parse.urlparse(homepage[0].final_url)
        feeds.append(urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/feed/", "", "", "")))
    for feed_url in feeds[:1]:
        if not robots_allowed(feed_url, timeout=timeout):
            continue
        response = fetch(feed_url, accept="application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5", timeout=timeout, max_bytes=1_500_000)
        if response.ok and signals.registered_domain(response.final_url) == site_domain:
            items = signals.parse_feed(response.final_url, response.body)
            if items:
                return items, {"url": response.final_url, "content_sha256": response.sha256, "retrieved_at": utc_now()}
    items: dict[str, dict[str, str]] = {}
    for page_url, (_, _, soup) in list(crawl.pages.items()):
        for item in signals.listing_items(page_url, soup):
            items.setdefault(item["url"], item)
    if not items:
        for listing_url in signals.news_links(homepage[0].final_url, homepage[2])[:1]:
            page = _get_page(crawl, listing_url, site_domain, timeout)
            if page is None:
                continue
            for item in signals.listing_items(page[0].final_url, page[2]):
                items.setdefault(item["url"], item)
    if not items:
        sitemap = site_sitemap(crawl, site_domain, timeout)
        for item in signals.sitemap_article_urls(sitemap["post_entries"], from_post_sitemap=True) + signals.sitemap_article_urls(sitemap["entries"], from_post_sitemap=False):
            items.setdefault(item["url"], item)
    ordered = sorted(items.values(), key=lambda item: (signals.datetime_sort_key(item["published_at"]), item["url"]), reverse=True)
    return ordered, None


def news_record(crawl: SiteCrawl, *, run_date: datetime, timeout: float = 12.0) -> dict[str, Any]:
    website = crawl.record
    value = website.get("value") or {}
    homepage_url = value.get("final_url") or website.get("source_url")
    site_domain = value.get("registered_domain") or signals.registered_domain(homepage_url)
    candidates, feed = _candidate_items(crawl, site_domain, timeout)
    candidates = [item for item in candidates if signals.within_days(item["published_at"], run_date, NEWS_WINDOW_DAYS)]
    published: list[dict[str, Any]] = []
    for item in candidates[:MAX_ARTICLES]:
        page = _get_page(crawl, item["url"], site_domain, timeout)
        meta = signals.article_meta(page[0].final_url, page[1], page[2]) if page else None
        if page and meta and signals.within_days(meta["published_at"], run_date, NEWS_WINDOW_DAYS):
            response = page[0]
            published.append({
                "title": meta["title"],
                "published_at": meta["published_raw"],
                "url": signals.clean_url(response.final_url),
                "value": f"{meta['title']} ({meta['published_raw']})",
                "claim_span": meta["title"],
                "date_span": meta["published_raw"],
                "source_url": response.final_url,
                "content_sha256": response.sha256,
                "retrieved_at": utc_now(),
                "extraction_method": meta["method"],
            })
        elif feed and item.get("published_raw") and item.get("title"):
            published.append({
                "title": item["title"],
                "published_at": item["published_at"],
                "url": item["url"],
                "value": f"{item['title']} ({item['published_at']})",
                "claim_span": item["title"],
                "date_span": item["published_raw"],
                "source_url": feed["url"],
                "content_sha256": feed["content_sha256"],
                "retrieved_at": feed["retrieved_at"],
                "extraction_method": "site_declared_feed_item",
            })
    if not published:
        return evidence("news", "not_found", NEWS_SOURCE, homepage_url, note="No dated article was found on the verified company site")
    published.sort(key=lambda item: (signals.datetime_sort_key(signals.normalize_datetime(item["published_at"]) or ""), item["url"]), reverse=True)
    return evidence(
        "news", "available", NEWS_SOURCE, published[0]["source_url"],
        value={"items": published},
        note="Company-owned dated activity; not independent coverage or sentiment.",
        content_sha256=published[0]["content_sha256"],
        effective_at=signals.normalize_datetime(published[0]["published_at"]),
    )


def unverified_site_records(website: dict[str, Any], reason: str) -> dict[str, dict[str, Any]]:
    source_url = website.get("source_url") or "https://data.brreg.no/enhetsregisteret/api/enheter"
    return {
        "hiring": evidence("hiring", "not_found", HIRING_SOURCE, source_url, note=reason),
        "news": evidence("news", "not_found", NEWS_SOURCE, source_url, note=reason),
    }
