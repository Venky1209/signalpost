"""Deterministic company brief: what it does, what changed, what is unknown — every line cited.

No language model is used. Each sentence is a template over facts already present in the profile
and carries the source URL and retrieval time of the record it was built from.
"""
from __future__ import annotations

from typing import Any

LEGAL_FORMS = {
    "AS": "private limited company (aksjeselskap)",
    "ASA": "public limited company (allmennaksjeselskap)",
    "ENK": "sole proprietorship (enkeltpersonforetak)",
    "ANS": "general partnership (ansvarlig selskap)",
    "DA": "partnership with shared liability (delt ansvar)",
    "NUF": "Norwegian branch of a foreign company (NUF)",
    "SA": "cooperative (samvirkeforetak)",
    "STI": "foundation (stiftelse)",
    "BRL": "housing cooperative (borettslag)",
    "FLI": "association (forening/lag/innretning)",
    "ESEK": "condominium owners' association (eierseksjonssameie)",
    "IKS": "inter-municipal company",
    "KS": "limited partnership (kommandittselskap)",
    "SF": "state enterprise (statsforetak)",
}
FINANCIAL_LABELS = (("revenue", "revenue"), ("operating_result", "operating result"), ("annual_result", "annual result"), ("equity", "equity"), ("assets", "total assets"))


def _money(value: Any, currency: str | None) -> str:
    number = float(value)
    text = f"{int(number):,}".replace(",", " ") if number == int(number) else f"{number:,.2f}".replace(",", " ")
    return f"{text} {currency or 'NOK'}"


def _cite(record: dict[str, Any]) -> dict[str, Any]:
    return {"source_url": record.get("source_url"), "retrieved_at": record.get("retrieved_at"), "source_class": record.get("source_class") or record.get("source_type")}


def _period_year(row: dict[str, Any]) -> str:
    return str((row.get("period") or {}).get("tilDato") or "")[:4]


def summary_text(brief: dict[str, Any]) -> str:
    """One readable paragraph per question, each sentence followed by its source."""
    def lines(rows: list[dict[str, Any]], key: str = "text") -> str:
        return " ".join(f"{row[key]} [Source: {row.get('source_url') or row.get('checked') or 'none'}]" for row in rows)

    parts = ["What it is and does: " + lines(brief["summary"])] if brief["summary"] else []
    parts.append("Changes: " + (lines(brief["what_changed"]) if brief["what_changed"] else "no dated change was found in the sources checked."))
    unknown = [{**row, "text": f"{row['topic']} ({row['state']}: {row['reason']})"} for row in brief["unknown"]]
    parts.append("Unknowns: " + (lines(unknown) if unknown else "none among the sources checked."))
    return "\n".join(parts)


def build_brief(profile: dict[str, Any], refresh_changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    records = profile.get("evidence") or {}
    org = str(profile.get("organisation_number"))
    summary: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    def say(topic: str, text: str, record: dict[str, Any], bucket: list[dict[str, Any]] | None = None) -> None:
        (summary if bucket is None else bucket).append({"topic": topic, "text": text, **_cite(record)})

    def missing(topic: str, record: dict[str, Any] | None, reason: str) -> None:
        record = record or {}
        unknown.append({"topic": topic, "reason": str(record.get("note") or reason), "state": record.get("status") or "not_fetched", "checked": record.get("source_url")})

    live = records.get("registry_live") or {}
    entity = live.get("value") or {}
    anchor = live if live.get("status") == "available" else records.get("registry") or {}
    name = entity.get("name") or profile.get("name")
    if name:
        form = entity.get("legal_form") or profile.get("legal_form") or ""
        place = (entity.get("business_address") or {}).get("poststed") or profile.get("municipality") or ""
        text = f"{name} (organisation number {org}) is a {LEGAL_FORMS.get(form, 'registered entity of form ' + form if form else 'registered entity')}"
        text += f" registered in {place}." if place else "."
        say("identity", text, anchor)
        industry = entity.get("industry") if isinstance(entity.get("industry"), dict) else None
        code = (industry or {}).get("kode") or profile.get("industry_code")
        label = (industry or {}).get("beskrivelse") or profile.get("industry_label")
        if code:
            say("what_it_does", f"Its registered industry is {code} {label or ''}".strip() + ".", anchor)
        else:
            missing("registered industry", anchor, "The registry lists no industry code")
        stated = " ".join(str(entity.get("activity") or entity.get("statutory_purpose") or "").split())
        if stated:
            label = "registered activity" if entity.get("activity") else "statutory purpose"
            say("what_it_does", f"Its {label}, as filed with the registry: “{stated[:400]}”", live)
        if entity.get("bankrupt") or entity.get("liquidating") or profile.get("bankrupt") or profile.get("liquidating"):
            say("status", "The registry flags the entity as bankrupt or under liquidation.", anchor)
    else:
        missing("legal identity", anchor, "The organisation number was not returned by the registry")

    website = records.get("website") or {}
    site = website.get("value") or {}
    identity = site.get("identity_assessment") or {}
    exact_site = website.get("status") == "available" and bool(identity.get("publishable"))
    if exact_site:
        description = " ".join(str(site.get("description") or "").split())
        if len(description) >= 30:
            say("what_it_does", f"In its own words (company-owned site, promotional copy): “{description[:400]}”", website)
        else:
            missing("company's own description", {**website, "status": "not_found", "note": "The verified homepage states no description"}, "")
        say("website", f"Verified website: {site.get('final_url')} ({'; '.join(identity.get('reasons') or [])}).", website)
        links = site.get("social_links") or []
        if links:
            say("profiles", "Company-owned profiles linked from that site: " + ", ".join(item["url"] for item in links) + ".", website)
        else:
            missing("social profiles", {**website, "status": "not_found", "note": "No profile linked from the verified site passed the identity gate"}, "")
    elif website.get("status") == "available":
        missing("official website", {**website, "status": "ambiguous", "note": "A site loads at the registry-listed address but could not be tied to this exact legal entity (" + "; ".join(identity.get("reasons") or []) + ")"}, "")
        missing("social profiles", {**website, "status": "ambiguous", "note": "Not published because the website is not verified for this entity"}, "")
    else:
        missing("official website", website, "No website could be tied to this exact entity")
        missing("social profiles", website, "No verified website to read profiles from")

    financials = records.get("financials") or {}
    rows = sorted((financials.get("value") or {}).get("records") or [], key=lambda row: str((row.get("period") or {}).get("tilDato") or ""), reverse=True)
    rows = [row for row in rows if row.get("account_type") == rows[0].get("account_type")] if rows else []
    if financials.get("status") == "available" and rows:
        latest = rows[0]
        parts = [f"{label} {_money(latest[field], latest.get('currency'))}" for field, label in FINANCIAL_LABELS if latest.get(field) is not None]
        period = latest.get("period") or {}
        if parts:
            say("financials", f"Latest filed accounts ({period.get('fraDato')} to {period.get('tilDato')}): " + ", ".join(parts) + ".", financials)
        if len(rows) > 1:
            previous = rows[1]
            for field, label in FINANCIAL_LABELS[:3]:
                new, old = latest.get(field), previous.get(field)
                if new is None or old is None or new == old:
                    continue
                direction = "rose" if new > old else "fell"
                percent = f" ({abs(new - old) / abs(old) * 100:.1f}%)" if old else ""
                say("financial_change", f"{label.capitalize()} {direction} from {_money(old, previous.get('currency'))} in {_period_year(previous)} to {_money(new, latest.get('currency'))} in {_period_year(latest)}{percent}.", financials, changed)
        else:
            missing("year-on-year financial change", {**financials, "status": "not_found", "note": "Only one filed period was returned, so no comparison is made"}, "")
    else:
        missing("latest filed accounts", financials, "No normalized annual accounts were returned")

    roles = records.get("roles") or {}
    role_rows = [row for row in (roles.get("value") or {}).get("roles") or [] if row.get("name") and not row.get("inactive")]
    if roles.get("status") == "available" and role_rows:
        leaders = sorted((row for row in role_rows if row.get("role_code") in {"DAGL", "LEDE", "INNH", "KONT", "BEST"}), key=lambda row: (str(row.get("role_code")), str(row.get("name"))))
        shown = leaders or sorted(role_rows, key=lambda row: (str(row.get("role_code")), str(row.get("name"))))[:3]
        say("leadership", "Public role holders: " + "; ".join(f"{row.get('role')}: {row.get('name')}" for row in shown[:5]) + ".", roles)
        dates = sorted({str(row.get("last_changed")) for row in role_rows if row.get("last_changed")})
        if dates:
            say("leadership_change", f"The registry last changed a role group on {dates[-1]}.", roles, changed)
    else:
        missing("leadership", roles, "No public role holders were returned")

    locations = records.get("locations") or {}
    location_rows = (locations.get("value") or {}).get("locations") or []
    if locations.get("status") == "available" and location_rows:
        places = sorted({str((row.get("address") or {}).get("poststed") or "") for row in location_rows} - {""})
        say("workplaces", f"{len(location_rows)} registered workplace(s)" + (": " + ", ".join(places[:8]) if places else "") + ".", locations)
    else:
        missing("registered workplaces", locations, "No registered workplaces were returned")

    hiring = records.get("hiring") or {}
    hiring_value = hiring.get("value") or {}
    if hiring.get("status") == "available":
        signals = hiring_value.get("signals") or []
        postings = hiring_value.get("job_postings") or []
        if signals:
            say("hiring", f"Hiring signal: the company recruits through {signals[0]['url']}.", {**hiring, "source_url": signals[0]["source_url"]})
        if postings:
            say("hiring", f"{len(postings)} open vacancy(ies) found, e.g. “{postings[0]['title']}”.", {**hiring, "source_url": postings[0]["source_url"]})
    else:
        missing("hiring", hiring, "No hiring signal was found")

    news = records.get("news") or {}
    items = (news.get("value") or {}).get("items") or []
    if news.get("status") == "available" and items:
        newest = items[0]
        say("recent_activity", f"Most recent dated item on the company's own site: “{newest['title']}” ({newest['published_at']}).", {**news, "source_url": newest["source_url"]}, changed)
    else:
        missing("dated public activity", news, "No dated company news was found")

    for change in refresh_changes or []:
        changed.append({
            "topic": "since_previous_run",
            "text": f"{change['field']} changed since the previous run.",
            "source_url": change.get("source_url"), "retrieved_at": change.get("retrieved_at"), "source_class": change.get("source_class"),
        })
    if not changed:
        missing("what changed", None, "No dated change was found in the sources checked")

    return {
        "method": "deterministic_templates_v1",
        "text": " ".join(item["text"] for item in summary),
        "summary": summary,
        "what_changed": changed,
        "unknown": unknown,
    }
