#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent import net  # noqa: E402
from norway_company_agent.batch import profile_complete_for_modules, profiles_from_bulk, read_organisation_inputs, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.brief import build_brief, summary_text  # noqa: E402
from norway_company_agent.claims import build_claims  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.identity import apply_website_identity_gate  # noqa: E402
from norway_company_agent.official import fetch_official_modules  # noqa: E402
from norway_company_agent.refresh import diff_profile  # noqa: E402
from norway_company_agent.nav_jobs import SOURCE as NAV_SOURCE, NavIndex, company_keys  # noqa: E402
from norway_company_agent.site_discovery import NAME_SOURCE_TYPE as NAME_DISCOVERY_SOURCE, SOURCE_TYPE as DISCOVERY_SOURCE, discovered_site_problem, email_domain_candidate, entity_proof, name_domain_candidates, redirect_problem, registered_domain  # noqa: E402
from norway_company_agent.site_signals import hiring_record, news_record, unverified_site_records  # noqa: E402
from norway_company_agent.website import crawl_site, normalize_homepage  # noqa: E402

DEFAULT_MODULES = "registry,accounting_obligation,registry_live,financials,roles,group,locations,website,hiring,news"
OFFICIAL_MODULES = {"registry_live", "financials", "financial_history", "roles", "group", "locations"}
SITE_MODULES = ("website", "hiring", "news")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def merge_nav_postings(profile: dict, postings: list[dict]) -> None:
    """Attach NAV vacancies to the hiring record; vacancies from an earlier pass are replaced, never duplicated."""
    records = profile["evidence"]
    hiring = records.get("hiring") or {}
    value = hiring.get("value") or {}
    own = [row for row in value.get("job_postings") or [] if row.get("source_class") != "official_public_feed"]
    if hiring.get("status") == "available" and (value.get("signals") or own or postings):
        hiring["value"] = {**value, "job_postings": own + postings}
    elif postings:
        records["hiring"] = evidence(
            "hiring", "available", NAV_SOURCE, postings[0]["source_url"],
            value={"signals": [], "job_postings": postings},
            note="Open vacancy in NAV's public feed; the ad's employer organisation number equals this entity or one of its registered workplaces",
            content_sha256=postings[0]["content_sha256"],
        )


def gate_site(profile: dict, record: dict, requested: str, *, discovered: bool, proof: dict | None = None) -> dict:
    """Kit identity gate, then the cross-domain redirect rule and (for discovered sites) the stricter bar."""
    website = apply_website_identity_gate(profile, record)["website"]
    value = website.get("value") or {}
    identity = value.get("identity_assessment") or {}
    if website.get("status") != "available" or not identity.get("publishable"):
        return website
    problem = redirect_problem(profile, requested, value) or (discovered_site_problem(profile, value, proof) if discovered else None)
    if problem:
        identity.update({"status": "review", "publishable": False, "score": min(float(identity.get("score") or 0), 0.85)})
        identity["reasons"] = [problem] + list(identity.get("reasons") or [])
        value["social_links"] = []
    return website


def research_site(profile: dict, requested: list[str], run_date: datetime, crawl_allowed: bool) -> None:
    """Website, then hiring and news only when the site is tied to this exact legal entity."""
    records = profile["evidence"]
    live = (records.get("registry_live") or {}).get("value") or {}
    url = profile.get("website") or live.get("website")
    if not crawl_allowed:
        skipped = "Run deadline reached before this site was read"
        for module in SITE_MODULES:
            if module in requested:
                records[module] = evidence(module, "blocked", "run_budget", str(url or "https://data.brreg.no/enhetsregisteret/api/enheter"), note=skipped)
        return
    crawl = crawl_site(url)
    records["website"] = gate_site(profile, crawl.record, str(url or ""), discovered=False)
    website = records["website"]
    identity = (website.get("value") or {}).get("identity_assessment") or {}

    def exact(record: dict, assessment: dict) -> bool:
        return record.get("status") == "available" and bool(assessment.get("publishable"))

    if not exact(website, identity):
        tried = {registered_domain(urlparse(str((website.get("value") or {}).get("final_url") or normalize_homepage(url) or "")).hostname or "")}
        attempts = []
        email_domain = email_domain_candidate(profile, tried)
        candidates = [(email_domain, "registry_email_domain", DISCOVERY_SOURCE)] if email_domain else []
        candidates += [(domain, "legal_name_domain", NAME_DISCOVERY_SOURCE) for domain in name_domain_candidates(profile, tried | {email_domain or ""})]
        for domain, method, source_type in candidates:
            candidate = crawl_site(domain, source_type=source_type)
            proof = entity_proof(profile, candidate.pages) if candidate.pages else None
            gated = gate_site(profile, candidate.record, domain, discovered=True, proof=proof)
            candidate_identity = (gated.get("value") or {}).get("identity_assessment") or {}
            outcome = {"method": method, "candidate": domain, "accepted": False}
            # A domain merely spelled like the legal name could be a namesake: the site itself must
            # show this entity's organisation number, registered phone, or registered postcode and place.
            if exact(gated, candidate_identity) and (method == "registry_email_domain" or proof):
                outcome["accepted"] = True
                if proof:
                    outcome["proof"] = proof
                    candidate_identity["reasons"] = [f"{proof['type'].replace('_', ' ')} ({proof['span']}) at {proof['page_url']}"] + list(candidate_identity.get("reasons") or [])
                gated["value"]["discovery"] = {**outcome, "registry_website": url or None, "registry_website_state": website.get("status"), "earlier_attempts": attempts}
                gated["note"] = (
                    "Found through the e-mail domain the entity registered in Brønnøysund; published only because the site names this exact legal entity"
                    if method == "registry_email_domain" else
                    "Found at a domain spelled like the legal name; published only because the site shows this entity's organisation number or registered phone"
                )
                records["website"], crawl, website, identity = gated, candidate, gated, candidate_identity
                break
            if gated.get("status") != "available":
                outcome["reason"] = str(gated.get("note") or gated.get("status"))
            elif method == "legal_name_domain" and not proof:
                outcome["reason"] = "site shows neither this entity's organisation number nor its registered phone"
            else:
                outcome["reason"] = "; ".join(candidate_identity.get("reasons") or [])
            attempts.append(outcome)
        if attempts and not exact(website, identity):
            if isinstance(website.get("value"), dict):
                website["value"]["discovery_attempts"] = attempts
            else:
                website["discovery_attempts"] = attempts
    if website.get("status") == "available" and identity.get("publishable"):
        crawl.record = website
        extra = {}
        if "hiring" in requested:
            extra["hiring"] = hiring_record(crawl)
        if "news" in requested:
            extra["news"] = news_record(crawl, run_date=run_date)
    else:
        reason = "No company site is tied to this exact legal entity, so no hiring or news facts are published from it"
        extra = {key: value for key, value in unverified_site_records(website, reason).items() if key in requested}
    records.update(extra)


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost batch: one terminal envelope per organisation number")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=None, help="Fail fast when the input size differs")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--previous", help="Profiles JSONL from an earlier run; enables change reporting")
    parser.add_argument("--deadline-minutes", type=float, default=0.0, help="After this many minutes remaining companies skip site crawling but still return envelopes (0 = no deadline)")
    parser.add_argument("--nav-days", type=int, default=45, help="Days of NAV's public vacancy feed to read (0 disables the feed)")
    parser.add_argument("--modules", default=DEFAULT_MODULES)
    args = parser.parse_args()

    started_at = utc_now()
    started_clock = time.monotonic()
    run_date = datetime.now(timezone.utc)
    organisation_inputs = read_organisation_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in organisation_inputs]
    expected = len(orgs) if args.expected_count is None else args.expected_count
    if len(orgs) != expected:
        raise SystemExit(f"Expected {expected} organisations, received {len(orgs)}")
    profiles, registry_metadata = profiles_from_bulk(args.bulk, orgs)
    annotations = {item["organisation_number"]: item for item in organisation_inputs}
    for profile in profiles:
        for key in ("evaluation_split", "sample_slice"):
            if key in annotations[profile["organisation_number"]]:
                profile[key] = annotations[profile["organisation_number"]][key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) & OFFICIAL_MODULES
    operations = {"requests": 0, "bytes": 0, "latencies_ms": []}
    errors: dict[str, list[dict]] = {}

    profiles_output = Path(args.profiles_output)
    previous_path = Path(args.previous) if args.previous else profiles_output if profiles_output.exists() and not args.resume else None
    previous_by_org = {}
    if previous_path and previous_path.exists():
        previous_by_org = {row["organisation_number"]: row for row in read_jsonl(previous_path)}
        if previous_path == profiles_output:
            profiles_output.replace(profiles_output.with_name(f"{profiles_output.stem}.before-{args.run_id}{profiles_output.suffix}"))

    def enrich(profile: dict) -> tuple[dict, dict]:
        meter = net.start_meter()
        org = profile["organisation_number"]
        official_metrics = []
        try:
            records, official_metrics = fetch_official_modules(org, fetch_modules)
            profile["evidence"].update(records)
        except Exception as exc:  # one company must never take the batch down
            errors.setdefault(org, []).append({"stage": "official", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        for module in sorted(fetch_modules):
            profile["evidence"].setdefault(module, evidence(module, "source_error", "official_registry", "https://data.brreg.no/", note="Module did not complete"))
        if any(module in requested_modules for module in SITE_MODULES):
            crawl_allowed = not args.deadline_minutes or (time.monotonic() - started_clock) < args.deadline_minutes * 60
            try:
                research_site(profile, requested_modules, run_date, crawl_allowed)
            except Exception as exc:
                errors.setdefault(org, []).append({"stage": "website", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            for module in SITE_MODULES:
                if module in requested_modules:
                    profile["evidence"].setdefault(module, evidence(module, "source_error", "company_site", str(profile.get("website") or "https://data.brreg.no/"), note="Module did not complete"))
        metric = {
            "requests": len(official_metrics) + meter.requests,
            "bytes": sum(item.bytes_received for item in official_metrics) + meter.bytes,
            "latencies_ms": [item.elapsed_ms for item in official_metrics] + list(meter.latencies_ms),
        }
        profile["run_metrics"] = metric
        return profile, metric

    state: dict[str, dict] = {}
    resumed_profiles = 0
    if args.resume and profiles_output.exists():
        prior = read_jsonl(profiles_output)
        if not set(item["organisation_number"] for item in prior).issubset(set(orgs)):
            raise SystemExit("Resume profile membership is not a subset of this batch")
        state = {item["organisation_number"]: item for item in prior if profile_complete_for_modules(item, requested_modules)}
        resumed_profiles = len(state)
    pending_profiles = [profile for profile in profiles if profile["organisation_number"] not in state]
    nav = NavIndex(days=args.nav_days, now=run_date).start() if args.nav_days > 0 and "hiring" in requested_modules else None
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending_profiles}
        for index, future in enumerate(as_completed(futures), 1):
            profile, metric = future.result()
            state[profile["organisation_number"]] = profile
            operations["requests"] += metric["requests"]
            operations["bytes"] += metric["bytes"]
            operations["latencies_ms"].extend(metric["latencies_ms"])
            if index % args.checkpoint_every == 0 or index == len(pending_profiles):
                write_jsonl(profiles_output, [state[org] for org in orgs if org in state])

    ordered_profiles = [state[org] for org in orgs]
    nav_report = {"enabled": bool(nav)}
    if nav:
        nav.join()
        postings_by_org = {} if nav.error else nav.postings_for({profile["organisation_number"]: company_keys(profile) for profile in ordered_profiles})
        for profile in ordered_profiles:
            merge_nav_postings(profile, postings_by_org.get(profile["organisation_number"], []))
        nav_report.update({"feed_pages": nav.pages, "active_vacancies_seen": len(nav.active), "companies_with_vacancies": len(postings_by_org), "vacancies": sum(len(rows) for rows in postings_by_org.values()), "error": nav.error})
    completed_at = utc_now()
    envelopes = []
    change_count = 0
    for profile in ordered_profiles:
        org = profile["organisation_number"]
        previous = previous_by_org.get(org)
        changes = diff_profile(previous, profile) if previous else []
        change_count += len(changes)
        profile["refresh"] = {"compared_with_previous_run": bool(previous), "changes": changes}
        brief = build_brief(profile, changes)
        profile["company_brief"] = brief
        profile["summary"] = summary_text(brief)
        # Contract-shaped claims live inside the profile so the envelope keeps the kit's exact top-level keys.
        profile["claims"], profile["claim_evidence"] = build_claims(profile)
        profile["errors"] = errors.get(org, [])
        envelopes.append(terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at))
    validation = validate_envelopes(envelopes, expected)
    write_jsonl(profiles_output, ordered_profiles)
    write_jsonl(Path(args.output), envelopes)
    latencies = sorted(operations.pop("latencies_ms"))
    operations["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    operations["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None

    def covered(module: str, check=lambda record: record.get("status") == "available") -> int:
        return sum(1 for profile in ordered_profiles if check((profile.get("evidence") or {}).get(module) or {}))

    def exact_site(record: dict) -> bool:
        return record.get("status") == "available" and bool(((record.get("value") or {}).get("identity_assessment") or {}).get("publishable"))

    report = {
        "run_id": args.run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "wall_seconds": round(time.monotonic() - started_clock, 1),
        "expected_count": expected,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": resumed_profiles,
        "profiles_fetched_this_run": len(pending_profiles),
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": operations,
        "third_party_cost_usd": 0,
        "coverage": {
            "financials": covered("financials"),
            "roles": covered("roles"),
            "website_loaded": covered("website"),
            "website_exact_identity": covered("website", exact_site),
            "social_profile_companies": covered("website", lambda record: exact_site(record) and bool((record.get("value") or {}).get("social_links"))),
            "social_profiles": sum(len(((profile["evidence"].get("website") or {}).get("value") or {}).get("social_links") or []) for profile in ordered_profiles),
            "hiring_companies": covered("hiring"),
            "discovered_sites": covered("website", lambda record: exact_site(record) and bool((record.get("value") or {}).get("discovery"))),
            "news_companies": covered("news"),
            "news_items": sum(len(((profile["evidence"].get("news") or {}).get("value") or {}).get("items") or []) for profile in ordered_profiles),
        },
        "nav_vacancy_feed": nav_report,
        "refresh": {"compared_with": str(previous_path) if previous_by_org else None, "companies_compared": len(set(previous_by_org) & set(orgs)), "changes": change_count},
        "entrant_errors": sum(len(items) for items in errors.values()),
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
