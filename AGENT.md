# AGENT.md — build spec and research policy

This file is the operating brief for anyone (human or coding agent) working on this repository.
Read it before changing code. When a change conflicts with a rule here, the rule wins.

## 1. Mission

Given a batch of Norwegian organisation numbers, return exactly one evidence-backed profile per
number. Score at least 65/100 on Builderr's official Signalpost run, then maximise the mean daily
score.

## 2. How the score works (what we optimise)

| Dimension | Points | What moves it |
|---|---:|---|
| Recall and coverage | 50 | Per external field family: 70% companies covered + 30% facts found, against the verified union of all entrants' findings |
| Precision, identity, evidence | 30 | Right company, valid source, date, reopenable evidence. Wrong or unsupported facts lose points |
| Synthesis | 12 | What the company does, what changed, what is unknown, each with a source |
| UX | 8 | Find, compare and verify a company on desktop and mobile |

Working assumptions (inferred from published text and the public board, not confirmed):

- The external families are **company website, social profile, hiring signal, dated news**.
- Registry facts (accounts, roles, workplaces) protect precision and synthesis; they do not add recall.
- The reference kit lands near 13/50 recall: it covers website and social, and nothing for hiring or news.
- Target: recall >= 18 with evidence >= 27, synthesis 12, UX 8.

## 3. Hard rules (never trade these for coverage)

1. **Organisation number is the anchor.** A site, profile, vacancy or article is published only
   after it resolves to the exact legal entity. Parent, brand, franchise and namesake are not exact.
2. **No fact without reopenable evidence**: source URL, retrieval time, content hash, and the text
   span that supports it. News and hiring evidence point at the page itself, not a listing.
3. **Uncertain means `ambiguous` or `not_available`.** Never guess, never fill a gap with zero.
4. **One terminal envelope per input**, always, including for numbers missing from the registry
   snapshot and for companies where every source failed.
5. **Deterministic output.** The same retained inputs must produce byte-identical scored fields:
   sorted collections, content-derived ids, no run timestamps or random values inside claims, no
   fallback whose result depends on timing.
6. **Permitted sources only**: Brønnøysund open data, the company's own site (robots.txt honoured,
   identifying User-Agent), NAV's public vacancy feed. No LinkedIn, Meta, Indeed, Glassdoor or
   Google scraping. Search results are never evidence.
7. **No secrets, no personal accounts.** The official command needs no key. Third-party cost: $0.
8. **Keep the kit envelope shape.** `run_id, organisation_number, state, started_at, completed_at,
   modules, profile` with `profile.evidence.website.value.social_links` exactly as the kit emits
   them. New facts are added beside this shape, never instead of it.

## 4. Architecture

```
batch input ─► registry snapshot (identity anchor)
            ─► official modules: live entity, accounts, roles, group, workplaces
            ─► website: registry URL ─► homepage + priority pages ─► identity gate
            │     └─ discovery when the registry has no URL (registry e-mail domain, org-number proof)
            ─► on an exact-identity site only:
            │     social profiles · hiring signal (careers page, vacancies) · dated news (article pages)
            ─► NAV vacancy feed matched on employer organisation number
            ─► claims + evidence (one scalar claim per fact) ─► company brief + unknowns
            ─► terminal envelope ─► run report ─► static viewer
```

| Module | File | Publishes |
|---|---|---|
| HTTP gate | `src/norway_company_agent/net.py` | nothing; every site request, robots cache, retries, accounting |
| Website crawl | `website.py` | homepage record, pages, discovered social links |
| Identity gate | `identity.py` | `exact` / `review` / `related_or_uncertain` |
| Signal extractors | `signals.py` | pure parsing of careers links, feeds, listings, article dates |
| Site signals | `site_signals.py` | `hiring` and `news` evidence records |
| Claims | `claims.py` | contract-shaped `claims` and `evidence` lists |
| Brief | `brief.py` | cited summary, changes, unknowns |
| Batch runner | `scripts/run_competition_batch.py` | envelopes, profiles, run report |

## 5. Build order and gates

Each milestone ends with a measured run on the frozen 200-company development batch. A change is
kept only when it adds coverage with zero new wrong-company publications in the audit sample.

| # | Milestone | Gate |
|---|---|---|
| M0 | Kit baseline measured on dev-200 | numbers recorded in `reports/` |
| M1 | Shared HTTP gate, robots cache, charset handling, footer identity | kit tests pass; website/social coverage >= kit |
| M2 | Hiring signal from exact sites | careers URL verified on its own page |
| M3 | Dated news from exact sites | title and timestamp both verbatim on the article page |
| M4 | Claims layer, brief, unknowns, viewer | every claim resolves to evidence; viewer builds |
| M5 | Determinism and robustness | two runs on one cache: zero differences; missing org number still returns an envelope |
| M6 | NAV vacancies; e-mail-domain website discovery | employer org number equals entity or its subunit; discovery needs org-number or exact-name proof |
| M7 | Clean-clone check, 100-company smoke report, submit v1 | fresh venv, declared install, one command |

## 6. Abstention policy

- Site loads but identity is not exact → website stays recorded with `publishable: false`; no
  social, hiring or news facts are published from it.
- Careers link found but the page does not name careers or vacancies → not published.
- Article without a machine-readable date on the page → not published.
- Social link whose handle carries neither the legal name nor the verified site's domain label → quarantined.
- Source refused the request (robots, 401/403/429) → `blocked`, never `not_available`.

## 7. Definition of done for a submission

- `uv sync --frozen` then the one command in `README.md` works in an empty clone.
- N inputs → N envelopes, validation passed, zero silent drops.
- Second run against the first run's profiles reports zero false changes.
- `reports/smoke-100/` holds envelopes, run report and a built viewer.
- README states models/APIs (none), licences, cost per run ($0) and known limitations.
