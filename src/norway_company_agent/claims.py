"""Contract-shaped claims: one scalar claim per fact, each tied to reopenable evidence.

The kit profile stays the source of truth. This module only projects it into the
`claims` / `evidence` lists described in OUTPUT_CONTRACT.md, in a stable order.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

REGISTRY_API = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
FINANCIAL_FIELDS = ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt")
AVAILABILITY = {
    "available": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
    "source_error": "failed",
    "not_fetched": "failed",
}


class ClaimBuilder:
    def __init__(self, organisation_number: str) -> None:
        self.org = organisation_number
        self.claims: list[dict[str, Any]] = []
        self.evidence: dict[str, dict[str, Any]] = {}

    def add_evidence(self, *, source_url: str, source_class: str, retrieved_at: str | None, content_sha256: str | None, claim_span: str, extraction_method: str, **extra: Any) -> str:
        key = "|".join([self.org, source_url or "", content_sha256 or "", claim_span or ""])
        evidence_id = "ev-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        if evidence_id not in self.evidence:
            row = {
                "id": evidence_id,
                "source_url": source_url,
                "source_class": source_class,
                "retrieved_at": retrieved_at,
                "content_sha256": content_sha256,
                "claim_span": claim_span,
                "extraction_method": extraction_method,
            }
            row.update({name: value for name, value in extra.items() if value is not None})
            self.evidence[evidence_id] = row
        return evidence_id

    def add(self, field: str, value: Any, availability: str, confidence: float, evidence_ids: list[str], **extra: Any) -> None:
        claim = {"field": field, "value": value, "availability": availability, "confidence": confidence, "evidence_ids": evidence_ids}
        claim.update({name: item for name, item in extra.items() if item is not None})
        self.claims.append(claim)

    def record_evidence(self, record: dict[str, Any], span: str, method: str, **extra: Any) -> str:
        return self.add_evidence(
            source_url=record.get("source_url") or "", source_class=record.get("source_class") or record.get("source_type") or "",
            retrieved_at=record.get("retrieved_at"), content_sha256=record.get("content_sha256"), claim_span=span, extraction_method=method, **extra,
        )

    def unavailable(self, field: str, record: dict[str, Any] | None, default_note: str) -> None:
        record = record or {}
        availability = AVAILABILITY.get(str(record.get("status")), "failed") if record else "failed"
        if availability == "available":
            availability = "not_available"
        note = str(record.get("note") or default_note)
        ids = [self.record_evidence(record, note, "source_checked")] if record.get("source_url") else []
        self.add(field, None, availability, 0.0, ids, note=note)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _address(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    lines = value.get("adresse") or []
    parts = [", ".join(str(line) for line in lines if line), " ".join(str(value.get(key) or "") for key in ("postnummer", "poststed")).strip(), str(value.get("land") or "")]
    text = ", ".join(part for part in parts if part)
    return text or None


def _registry_claims(builder: ClaimBuilder, profile: dict[str, Any]) -> None:
    records = profile.get("evidence") or {}
    live = records.get("registry_live") or {}
    entity = live.get("value") or {}
    bulk = records.get("registry") or {}
    if live.get("status") == "available" and entity.get("organisation_number") == builder.org:
        def fact(field: str, value: Any, span_key: str, span_value: Any) -> None:
            if value in (None, ""):
                return
            ids = [builder.record_evidence(live, f'"{span_key}":{_compact(span_value)}', "registry_api_json_field")]
            builder.add(field, value, "available", 1.0, ids)

        fact("legal_name", entity.get("name"), "navn", entity.get("name"))
        fact("organisation_form", entity.get("legal_form"), "kode", entity.get("legal_form"))
        industry = entity.get("industry") or {}
        if isinstance(industry, dict) and industry.get("kode"):
            fact("industry", f"{industry.get('kode')} {industry.get('beskrivelse') or ''}".strip(), "kode", industry.get("kode"))
        address = _address(entity.get("business_address"))
        if address and isinstance(entity.get("business_address"), dict):
            fact("registered_address", address, "poststed", entity["business_address"].get("poststed"))
        if entity.get("employees") is not None:
            fact("registered_employees", entity.get("employees"), "antallAnsatte", entity.get("employees"))
        if entity.get("latest_submitted_accounts"):
            fact("latest_submitted_accounts_year", str(entity.get("latest_submitted_accounts")), "sisteInnsendteAarsregnskap", str(entity.get("latest_submitted_accounts")))
    elif bulk.get("status") == "available" and profile.get("name"):
        ids = [builder.record_evidence(bulk, str(profile.get("name")), "registry_bulk_row", source_row_key=builder.org)]
        builder.add("legal_name", profile.get("name"), "available", 1.0, ids)
    else:
        builder.unavailable("legal_name", live or bulk, "The organisation number was not returned by the registry")

    financials = records.get("financials") or {}
    rows = (financials.get("value") or {}).get("records") or []
    if financials.get("status") == "available" and rows:
        latest = max(rows, key=lambda row: str((row.get("period") or {}).get("tilDato") or ""))
        period = latest.get("period") or {}
        period_label = f"{period.get('fraDato')}/{period.get('tilDato')}"
        for field in FINANCIAL_FIELDS:
            if latest.get(field) is None:
                continue
            ids = [builder.record_evidence(financials, f"{field}={latest[field]} {latest.get('currency') or ''} ({period_label})".strip(), "registry_accounts_normalized_field", reporting_period=period_label)]
            builder.add(f"financials.{field}", latest[field], "available", 1.0, ids, reporting_period=period_label, currency=latest.get("currency"))
    else:
        builder.unavailable("financials.latest", financials, "No normalized annual accounts were returned")

    roles = records.get("roles") or {}
    role_rows = [row for row in (roles.get("value") or {}).get("roles") or [] if row.get("name") and not row.get("inactive")]
    if roles.get("status") == "available" and role_rows:
        for row in sorted(role_rows, key=lambda item: (str(item.get("role_code")), str(item.get("name")))):
            ids = [builder.record_evidence(roles, f"{row.get('role')}: {row.get('name')}", "registry_roles_field", effective_at=row.get("last_changed"))]
            builder.add("leadership.role", f"{row.get('role')}: {row.get('name')}", "available", 1.0, ids, role_code=row.get("role_code"))
    else:
        builder.unavailable("leadership.role", roles, "No public role holders were returned")

    locations = records.get("locations") or {}
    location_rows = (locations.get("value") or {}).get("locations") or []
    if locations.get("status") == "available" and location_rows:
        for row in sorted(location_rows, key=lambda item: str(item.get("organisation_number"))):
            address = _address(row.get("address"))
            label = f"{row.get('name')} ({row.get('organisation_number')})" + (f", {address}" if address else "")
            ids = [builder.record_evidence(locations, f"{row.get('organisation_number')}", "registry_subunit_field")]
            builder.add("registered_workplace", label, "available", 1.0, ids)
    else:
        builder.unavailable("registered_workplace", locations, "No registered workplaces were returned")


def _site_claims(builder: ClaimBuilder, profile: dict[str, Any]) -> None:
    records = profile.get("evidence") or {}
    website = records.get("website") or {}
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}
    exact = website.get("status") == "available" and bool(identity.get("publishable"))
    if exact:
        proof = "; ".join(identity.get("reasons") or [])
        span = value.get("title") or value.get("registered_domain") or ""
        ids = [builder.record_evidence(website, span, "homepage_identity_gate", identity_proof=proof)]
        builder.add("official_website", value.get("final_url"), "available", float(identity.get("score") or 0.9), ids, registered_domain=value.get("registered_domain"))
        description = " ".join(str(value.get("description") or "").split())
        if len(description) >= 30:
            ids = [builder.record_evidence(website, description[:300], "homepage_meta_description")]
            builder.add("company_description", description[:500], "available", 0.9, ids, note="Company-owned description; promotional copy, not an independent assessment")
        else:
            builder.unavailable("company_description", {**website, "status": "not_found", "note": "The verified homepage states no description"}, "")
        sources = value.get("social_link_sources") or {}
        for link in sorted(value.get("social_links") or [], key=lambda item: (item["platform"], item["url"])):
            origin = sources.get(link["url"]) or {}
            ids = [builder.add_evidence(
                source_url=origin.get("page_url") or value.get("final_url"), source_class="company_owned",
                retrieved_at=website.get("retrieved_at"), content_sha256=origin.get("content_sha256") or website.get("content_sha256"),
                claim_span=origin.get("raw") or link["url"], extraction_method="link_on_verified_company_site",
            )]
            builder.add("social_profile", link["url"], "available", 0.9, ids, platform=link["platform"])
        if not value.get("social_links"):
            builder.unavailable("social_profile", {**website, "status": "not_found", "note": "No company-owned social profile passed the identity gate on the verified site"}, "")
    elif website.get("status") == "available":
        note = "A site loads at the registry-listed address but could not be tied to this exact legal entity: " + "; ".join(identity.get("reasons") or [])
        ids = [builder.record_evidence(website, value.get("title") or value.get("final_url") or "", "homepage_identity_gate")]
        builder.add("official_website", None, "ambiguous", float(identity.get("score") or 0.0), ids, candidate=value.get("final_url"), note=note)
    else:
        builder.unavailable("official_website", website, "No website could be tied to this exact entity")

    hiring = records.get("hiring") or {}
    hiring_value = hiring.get("value") or {}
    if hiring.get("status") == "available":
        for signal in hiring_value.get("signals") or []:
            ids = [builder.add_evidence(source_url=signal["source_url"], source_class="company_owned", retrieved_at=signal.get("retrieved_at"), content_sha256=signal.get("content_sha256"), claim_span=signal.get("claim_span") or "", extraction_method=signal.get("extraction_method") or "")]
            builder.add("hiring_signal", signal["url"], "available", 0.9, ids, kind=signal.get("kind"))
        for posting in hiring_value.get("job_postings") or []:
            ids = [builder.add_evidence(source_url=posting["source_url"], source_class=posting.get("source_class") or "company_owned", retrieved_at=posting.get("retrieved_at") or hiring.get("retrieved_at"), content_sha256=posting.get("content_sha256"), claim_span=posting["title"], extraction_method=posting.get("extraction_method") or "", employer_span=posting.get("employer_span"))]
            builder.add("job_posting", posting["title"], "available", 0.95, ids, url=posting.get("url"), date_posted=posting.get("date_posted"), application_due=posting.get("application_due"))
    else:
        builder.unavailable("hiring_signal", hiring, "No hiring signal was found")

    news = records.get("news") or {}
    if news.get("status") == "available":
        for item in (news.get("value") or {}).get("items") or []:
            ids = [builder.add_evidence(source_url=item["source_url"], source_class="company_owned", retrieved_at=item.get("retrieved_at"), content_sha256=item.get("content_sha256"), claim_span=item["claim_span"], extraction_method=item.get("extraction_method") or "", date_span=item.get("date_span"))]
            builder.add("dated_news", item["value"], "available", 0.9, ids, title=item["title"], published_at=item["published_at"], url=item["url"])
    else:
        builder.unavailable("dated_news", news, "No dated company news was found")


def build_claims(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    builder = ClaimBuilder(str(profile.get("organisation_number")))
    _registry_claims(builder, profile)
    _site_claims(builder, profile)
    return builder.claims, [builder.evidence[key] for key in sorted(builder.evidence)]
