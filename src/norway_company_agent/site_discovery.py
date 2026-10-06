"""Website candidates for entities whose registry record lists no working site.

The only candidate source is the domain of the e-mail address the entity itself registered in
Brønnøysund. A candidate is never a fact: the site must still pass the exact-identity gate.
"""
from __future__ import annotations

from typing import Any

import tldextract

SOURCE_TYPE = "registry_email_domain_website"
# Mailbox providers and access-network domains: an address there says nothing about a company site.
MAILBOX_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.no", "live.com",
    "msn.com", "yahoo.com", "yahoo.no", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "mail.com",
    "gmx.com", "gmx.net", "protonmail.com", "proton.me", "pm.me", "zoho.com", "yandex.com", "mail.ru",
    "online.no", "getmail.no", "broadpark.no", "lyse.net", "frisurf.no", "start.no", "c2i.net", "tele2.no",
    "epost.no", "altibox.no", "ebnett.no", "combitel.no", "bluezone.no", "loqal.no", "sf-nett.no", "bbnett.no",
    "mimer.no", "ntebb.no", "signal.no", "eidsiva.net", "svorka.net", "haugnett.no", "sfjbb.net", "enivest.net",
    "tussa.com", "monet.no", "kvamnet.no", "vikenfiber.no", "telenor.no", "telia.no", "nextgentel.com", "chello.no",
    "powertech.no", "nktv.no", "smartnett.no", "neasonline.no", "istad.no", "rfrisk.no", "hesbynett.no", "wemail.no",
    "vktv.no", "kraftlaget.no", "tikkbb.no", "vfiber.no", "fiber.no", "netcom.no", "sensewave.com", "ktvnett.no",
}


def registered_domain(host: str) -> str:
    return tldextract.extract(str(host or "").strip().lower()).top_domain_under_public_suffix


def email_domain_candidate(profile: dict[str, Any], already_tried: set[str]) -> str | None:
    records = profile.get("evidence") or {}
    live = (records.get("registry_live") or {}).get("value") or {}
    raw = (records.get("registry") or {}).get("value") or {}
    domain = live.get("email_domain")
    if not domain:
        address = str(raw.get("epostadresse") or "").strip().lower()
        domain = address.rsplit("@", 1)[1] if address.count("@") == 1 else None
    domain = registered_domain(domain or "")
    if not domain or domain in MAILBOX_DOMAINS or domain in already_tried:
        return None
    return domain
