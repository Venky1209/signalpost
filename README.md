# Signalpost company-research agent

Entry for the Builderr Signalpost challenge. Give it a batch of Norwegian organisation numbers;
it returns one evidence-backed profile per number. Built on the Builderr reference agent and keeps
its envelope shape, registry modules and tests.

## One command

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen && uv run python scripts/run_competition_batch.py \
  --organisations <batch.jsonl> --bulk <brreg-enheter.csv> \
  --profiles-output out/profiles.jsonl --output out/envelopes.jsonl \
  --report out/run-report.json --run-id <id>
```

- `--organisations`: JSON, JSONL or text list of organisation numbers.
- `--bulk`: the Brønnøysund entity snapshot (`https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv`).
- `--expected-count N` is optional and fails fast when the input size differs.
- Refresh: run the same command again with the same `--profiles-output`, or pass
  `--previous <earlier profiles.jsonl>`. Each profile then lists what changed; the earlier file is kept.

Every input returns exactly one terminal envelope, including numbers missing from the snapshot and
companies where every source failed.

## What it finds

| Area | Source | Published only when |
|---|---|---|
| Legal identity, industry, registered activity, address | Brønnøysund entity register | the organisation number matches |
| Latest filed accounts and year-on-year change | Regnskapsregisteret | a filed period is returned; missing values are never zero |
| Role holders, registered workplaces, group links | Brønnøysund | returned by the register |
| Official website | registry-listed URL; else the domain of the e-mail address the entity registered; else a `.no` domain spelled exactly like the legal name | the homepage names this exact legal entity or shows its organisation number. A name-spelled domain also needs the entity's organisation number, registered phone, or registered postcode and place on the site |
| Social profiles | links and `sameAs` data on the verified site | the handle carries the legal name or the verified site's domain label |
| Hiring signal | careers page linked from the verified site, plus `JobPosting` data on it | the page's own heading or title names careers or vacancies |
| Open vacancies | NAV's public vacancy feed (arbeidsplassen.nav.no) | the ad's employer organisation number equals the entity or one of its registered workplaces |
| Dated news | the site's declared feed or news listing | title and timestamp are both stated on the article page |

A site that loads but cannot be tied to the exact entity is recorded with `publishable: false`, and
nothing further is published from it. Parent, brand and property-manager sites fail this gate by design.
Three further rules came out of a manual audit of 1,500 companies:

- A site that redirects to another registered domain is kept only when that domain is spelled from
  the legal name; otherwise it is usually a group, chain or platform page.
- A site the registry did not declare needs the legal name in its title, hostname or structured
  data (a footer mention is how group sites list subsidiaries), and a one-word legal name also
  needs the entity's organisation number, registered phone or address on the site.
- Parked domains and hosting placeholders are never a company website.

## Output

Each line of the envelope file keeps the reference shape:
`run_id, organisation_number, state, started_at, completed_at, modules, profile`.

Inside `profile`:

- `evidence.<module>`: one record per module with `status`, `source_url`, `retrieved_at`, `content_sha256` and `value`.
  Modules: `registry`, `accounting_obligation`, `registry_live`, `financials`, `roles`, `group`, `locations`, `website`, `hiring`, `news`.
- `claims` and `claim_evidence`: one scalar claim per fact in the `OUTPUT_CONTRACT.md` shape, each with the
  evidence that supports it. Unknown facts carry `not_available`, `blocked`, `ambiguous` or `failed`.
- `summary` and `company_brief`: what the company is and does, what changed, and what is unknown —
  every sentence with its source. Built from templates; no language model.
- `refresh`: changes since the previous run.

## Models, APIs, licences, cost

- No language model and no paid API. No secrets. Third-party cost per official run: **$0**.
- Sources: Brønnøysund open data (NLOD 2.0), NAV's public vacancy feed (read with the public token
  NAV publishes; contact persons in ads are never stored) and company-owned websites (robots.txt
  honoured, one robots read per host, the reference kit's User-Agent; override with `SIGNALPOST_USER_AGENT`).
- Not used: LinkedIn, Meta, Indeed, Glassdoor, Google or any search engine. The optional example
  scripts from the reference kit that touch those services are not imported by the official command.
- Roughly seven requests per company, plus about 100 requests for the vacancy feed per run
  (`--nav-days 0` turns the feed off). A 1,500-company run takes about 20 minutes with 16 workers.

## Checks

```bash
uv run --with pytest pytest -q
```

- `.github/workflows/clean-install.yml`: on every push, a Linux runner installs from the lockfile,
  runs the tests and runs the one command twice on three companies.
- `reports/smoke-100/`: a 100-company run from the public universe — envelopes, run report and a built viewer (`site/index.html`).
- Two consecutive runs over the same retained inputs produce identical records apart from timestamps.

## Viewer

```bash
uv run python scripts/build_prototype.py --input out/profiles.jsonl --output out/site/index.html
```

## Limitations

- JavaScript-only homepages are not rendered, so their identity often stays unverified.
- Companies whose public brand differs from the legal name are withheld unless the site shows the organisation number.
- News is limited to the three most recent dated articles per company.
- `AGENT.md` holds the research and abstention policy; `docs/` holds the reference kit's design notes.
