"""Open vacancies from NAV's public feed (arbeidsplassen.nav.no), matched on employer organisation number.

The feed lists every vacancy change in order. Names in the list only narrow down which entries to
open; a vacancy is published only when the entry's own `employer.orgnr` equals the organisation or
one of its registered workplaces. Contact persons in an ad are never read into the output.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from .evidence import utc_now
from .identity import _tokens
from .net import fetch

BASE = "https://pam-stilling-feed.nav.no"
SOURCE = "nav_public_vacancy_feed"
MAX_PAGES = 400
MAX_DETAILS = 400


def _json(url: str, token: str, extra: dict[str, str] | None = None) -> tuple[Any, Any]:
    response = fetch(url, accept="application/json", timeout=30.0, max_bytes=8_000_000, attempts=3, retry_errors=True, headers={"Authorization": f"Bearer {token}", **(extra or {})})
    if not response.ok:
        return None, response
    try:
        return json.loads(response.body), response
    except ValueError:
        return None, response


def name_key(name: Any) -> str:
    return " ".join(_tokens(name))


class NavIndex:
    """Reads the feed once per run in a background thread; `postings_for` is called after `join`."""

    def __init__(self, *, days: int = 45, now: datetime | None = None) -> None:
        self.days = days
        self.now = now or datetime.now(timezone.utc)
        self.active: dict[str, dict[str, str]] = {}
        self.pages = 0
        self.error: str | None = None
        self.token = ""
        self._thread = threading.Thread(target=self._read, name="nav-feed", daemon=True)

    def start(self) -> "NavIndex":
        self._thread.start()
        return self

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.error = self.error or "feed read did not finish in time"

    def _read(self) -> None:
        try:
            token_response = fetch(BASE + "/api/publicToken", accept="text/plain,*/*", timeout=20.0, attempts=3, retry_errors=True)
            words = token_response.text().split()
            if not token_response.ok or not words:
                self.error = f"public token unavailable ({token_response.error or token_response.status})"
                return
            self.token = words[-1]
            since = format_datetime(self.now - timedelta(days=self.days), usegmt=True)
            url, headers = BASE + "/api/v1/feed", {"If-Modified-Since": since}
            while url and self.pages < MAX_PAGES:
                body, response = _json(url, self.token, headers)
                headers = None
                if not isinstance(body, dict):
                    self.error = f"feed page failed ({response.error or response.status})"
                    return
                self.pages += 1
                for item in body.get("items") or []:
                    entry = item.get("_feed_entry") or {}
                    uuid = str(entry.get("uuid") or item.get("id") or "")
                    if not uuid:
                        continue
                    if entry.get("status") == "ACTIVE":
                        self.active[uuid] = {"title": str(entry.get("title") or ""), "business": str(entry.get("businessName") or "")}
                    else:
                        self.active.pop(uuid, None)
                next_url = body.get("next_url")
                url = BASE + next_url if next_url else None
        except Exception as exc:  # the batch must finish even when the feed does not
            self.error = f"{type(exc).__name__}: {str(exc)[:160]}"

    def postings_for(self, companies: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """`companies` maps organisation number -> {"names": [...], "orgnrs": {...}}."""
        by_name: dict[str, set[str]] = {}
        owner: dict[str, str] = {}
        for org, info in companies.items():
            for name in info["names"]:
                key = name_key(name)
                if key:
                    by_name.setdefault(key, set()).add(org)
            for number in info["orgnrs"]:
                owner[number] = org
        candidates = sorted(uuid for uuid, entry in self.active.items() if name_key(entry["business"]) in by_name)[:MAX_DETAILS]
        found: dict[str, list[dict[str, Any]]] = {}
        for uuid in candidates:
            api_url = f"{BASE}/api/v1/feedentry/{uuid}"
            body, response = _json(api_url, self.token)
            if not isinstance(body, dict) or body.get("status") != "ACTIVE":
                continue
            ad = body.get("ad_content") or {}
            employer = ad.get("employer") or {}
            number = "".join(character for character in str(employer.get("orgnr") or "") if character.isdigit())
            org = owner.get(number)
            title = " ".join(str(ad.get("title") or "").split())
            link = str(ad.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}")
            if not org or not title:
                continue
            found.setdefault(org, []).append({
                "title": title[:300],
                "url": link,
                "date_posted": ad.get("published"),
                "application_due": ad.get("applicationDue"),
                "employer_span": f"{employer.get('name')} ({number})",
                "source_url": link,
                "api_url": api_url,
                "source_class": "official_public_feed",
                "content_sha256": response.sha256,
                "retrieved_at": utc_now(),
                "extraction_method": "nav_feed_entry_employer_orgnr",
            })
        return {org: sorted(rows, key=lambda row: (row["url"], row["title"])) for org, rows in found.items()}


def company_keys(profile: dict[str, Any]) -> dict[str, Any]:
    records = profile.get("evidence") or {}
    org = str(profile.get("organisation_number"))
    live = (records.get("registry_live") or {}).get("value") or {}
    names = [profile.get("name"), live.get("name")]
    numbers = {org}
    for row in ((records.get("locations") or {}).get("value") or {}).get("locations") or []:
        names.append(row.get("name"))
        if row.get("organisation_number"):
            numbers.add(str(row["organisation_number"]))
    return {"names": [name for name in names if name], "orgnrs": numbers}
