"""Website candidates for entities whose registry record lists no working site.

The only candidate source is the domain of the e-mail address the entity itself registered in
Brønnøysund. A candidate is never a fact: the site must still pass the exact-identity gate.
"""
from __future__ import annotations

import re
from typing import Any

import tldextract

from .identity import _tokens

SOURCE_TYPE = "registry_email_domain_website"
NAME_SOURCE_TYPE = "legal_name_domain_website"
# Entity kinds that almost never run a site under their own legal name; skipping them saves requests.
NO_OWN_SITE_TOKENS = {
    "holding", "invest", "investering", "eiendom", "eiendommer", "borettslag", "borettslaget", "sameie", "sameiet",
    "boligsameie", "boligsameiet", "eierseksjonssameiet", "eierseksjonssameie", "utvikling", "tomteselskap", "garasjelag",
}
ORG_NUMBER = re.compile(r"(?<!\d)([89]\d{2})[ .\u00a0]?(\d{3})[ .\u00a0]?(\d{3})(?!\d)")
PHONE = re.compile(r"(?<!\d)(?:\+47[ \u00a0]?)?(\d{2}[ \u00a0]?\d{2}[ \u00a0]?\d{2}[ \u00a0]?\d{2}|\d{3}[ \u00a0]?\d{2}[ \u00a0]?\d{3})(?!\d)")
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


def name_domain_candidates(profile: dict[str, Any], already_tried: set[str]) -> list[str]:
    """`.no` hostnames spelled exactly like the legal name, joined and hyphenated."""
    tokens = _tokens(profile.get("name"))
    compact = "".join(tokens)
    if len(compact) < 6 or len(tokens) > 4 or set(tokens) & NO_OWN_SITE_TOKENS or compact.isdigit():
        return []
    candidates = [compact + ".no"]
    if len(tokens) > 1:
        candidates.append("-".join(tokens) + ".no")
    return [domain for domain in candidates if domain not in already_tried]


def entity_proof(profile: dict[str, Any], pages: dict[str, Any]) -> dict[str, str] | None:
    """Proof that a site belongs to this entity, strongest first: its organisation number, its
    registered phone, or its registered postcode and place, on a fetched page."""
    org = str(profile.get("organisation_number") or "")
    live = ((profile.get("evidence") or {}).get("registry_live") or {}).get("value") or {}
    phone = re.sub(r"\D", "", str(live.get("phone") or ""))[-8:]
    places = []
    for address in (live.get("business_address"), live.get("postal_address")):
        if isinstance(address, dict) and re.fullmatch(r"\d{4}", str(address.get("postnummer") or "")) and address.get("poststed"):
            places.append(re.compile(rf"(?<!\d){address['postnummer']}[\s ,]+{re.escape(str(address['poststed']))}(?![a-zæøå])", re.I))
    texts = [(url, page[0].sha256, page[2].get_text(" ", strip=True)) for url, page in pages.items()]

    def found(kind: str, url: str, digest: str, span: str) -> dict[str, str]:
        return {"type": kind, "page_url": url, "span": span, "content_sha256": digest}

    for url, digest, text in texts:
        for match in ORG_NUMBER.finditer(text):
            if "".join(match.groups()) == org:
                return found("organisation_number_on_site", url, digest, match.group(0))
    if len(phone) == 8:
        for url, digest, text in texts:
            for match in PHONE.finditer(text):
                if re.sub(r"\D", "", match.group(1)) == phone:
                    return found("registered_phone_on_site", url, digest, match.group(0))
    for url, digest, text in texts:
        for pattern in places:
            match = pattern.search(text)
            if match:
                return found("registered_postcode_and_place_on_site", url, digest, match.group(0))
    return None
