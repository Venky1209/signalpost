"""Website candidates for entities whose registry record lists no working site.

The only candidate source is the domain of the e-mail address the entity itself registered in
Brønnøysund. A candidate is never a fact: the site must still pass the exact-identity gate.
"""
from __future__ import annotations

import re
from typing import Any

import tldextract

from .identity import _compact, _structured_names, _tokens

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


def name_related_label(profile: dict[str, Any], domain: str) -> bool:
    """True when a domain's own label is spelled from the legal name (either contains the other)."""
    core = "".join(_tokens(profile.get("name")))
    label = str(domain or "").split(".")[0]
    try:
        label = label.encode("ascii").decode("idna")  # håkensbakken.no arrives as xn--hkensbakken-...
    except (UnicodeError, ValueError):
        pass
    label = _compact(label)
    return len(label) >= 4 and len(core) >= 4 and (label in core or core in label)


def redirect_problem(profile: dict[str, Any], requested: str, value: dict[str, Any]) -> str | None:
    """A site that redirects to another registered domain is kept only when that domain carries the legal name.

    Otherwise the landing page is usually a parent group, a chain, or a third-party platform page.
    """
    final_domain = registered_domain(_host(value.get("final_url")))
    if not final_domain or final_domain == registered_domain(_host(requested) or requested):
        return None
    if name_related_label(profile, final_domain):
        return None
    return f"redirects to {final_domain}, a domain not spelled from the legal name (likely a group, chain or platform page)"


def _host(url: Any) -> str:
    text = str(url or "")
    if "//" not in text:
        text = "https://" + text
    try:
        from urllib.parse import urlparse

        return urlparse(text).hostname or ""
    except ValueError:
        return ""


def discovered_site_problem(profile: dict[str, Any], value: dict[str, Any], proof: dict[str, str] | None = None) -> str | None:
    """Extra bar for a site the registry did not declare: the name must sit in the title, the hostname
    or structured organisation data (or the organisation number on the homepage), and the page must
    have real content. A legal name that only appears in a footer or description is not enough —
    that is how group sites list their subsidiaries.
    """
    tokens = _tokens(profile.get("name"))
    core = set(tokens)
    compact = "".join(tokens)
    org = str(profile.get("organisation_number") or "")
    hostname = _host(value.get("final_url"))
    strong_parts = [value.get("title"), hostname, *_structured_names(value.get("structured_organisations") or [])]
    named = any(core and core <= set(_tokens(part)) for part in strong_parts if part) or any(
        len(compact) >= 6 and compact in _compact(part) for part in strong_parts if part
    )
    homepage_text = " ".join(str(value.get(key) or "") for key in ("title", "description", "identity_text_excerpt", "main_text_excerpt"))
    numbered = any("".join(match.groups()) == org for match in ORG_NUMBER.finditer(homepage_text))
    # `proof` is this entity's organisation number, registered phone or postcode and place on a fetched page.
    proven = bool(proof) and name_related_label(profile, registered_domain(hostname))
    if not (named or numbered or proven):
        return "the legal name is not in the homepage title, hostname or structured data, and the organisation number is not on the homepage"
    if len(core) == 1 and not (numbered or proof):
        return "a one-word legal name on its own cannot separate this entity from a parent or namesake; the site shows no organisation number, registered phone or address"
    if len(str(value.get("main_text_excerpt") or "").strip()) < 150 and not (numbered or proof):
        return "the homepage has too little content to be a working company site"
    return None
