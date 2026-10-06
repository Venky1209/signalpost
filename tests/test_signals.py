from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup  # noqa: E402

from norway_company_agent import signals  # noqa: E402
from norway_company_agent.batch import profiles_from_bulk, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.brief import build_brief, summary_text  # noqa: E402
from norway_company_agent.claims import build_claims  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.identity import assess_social_identity, assess_website_identity  # noqa: E402
from norway_company_agent.official import normalize_entity  # noqa: E402
from norway_company_agent.refresh import diff_profile  # noqa: E402
from norway_company_agent.nav_jobs import NavIndex, company_keys, name_key  # noqa: E402
from norway_company_agent.site_discovery import email_domain_candidate, entity_proof, name_domain_candidates  # noqa: E402


def soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, "html.parser")


class HiringSignalTests(unittest.TestCase):
    def test_careers_links_are_same_site_and_ranked(self):
        page = soup(
            '<a href="/karriere">Karriere</a><a href="/produkter">Produkter</a>'
            '<a href="https://other.no/jobb">Jobb</a><a href="/nyheter/ny-jobb-til-ola">Les mer</a>'
            '<a href="https://firma.teamtailor.com/jobs">Ledige stillinger</a>'
        )
        links = signals.career_links("https://www.firma.no/", page)
        self.assertEqual(links[0], {"url": "https://www.firma.no/karriere", "kind": "company_site"})
        self.assertIn({"url": "https://firma.teamtailor.com/jobs", "kind": "hosted_careers_site"}, links)
        self.assertNotIn("https://other.no/jobb", [item["url"] for item in links])
        self.assertNotIn("https://www.firma.no/nyheter/ny-jobb-til-ola", [item["url"] for item in links])

    def test_careers_page_needs_its_own_heading_or_title(self):
        self.assertEqual(signals.careers_page_proof(soup("<h1>Ledige stillinger</h1>")), "Ledige stillinger")
        self.assertIsNone(signals.careers_page_proof(soup("<title>Produkter</title><h1>Våre produkter</h1>")))


class DatedNewsTests(unittest.TestCase):
    def test_article_keeps_the_page_timestamp_verbatim(self):
        html = (
            '<html><head><script type="application/ld+json">{"@type":"NewsArticle","headline":"Ny fabrikk åpnet",'
            '"datePublished":"2025-09-22T20:00:00+02:00"}</script></head><body><h1>Ny fabrikk åpnet</h1></body></html>'
        )
        meta = signals.article_meta("https://firma.no/nyheter/ny-fabrikk", html, soup(html))
        self.assertEqual(meta["title"], "Ny fabrikk åpnet")
        self.assertEqual(meta["published_raw"], "2025-09-22T20:00:00+02:00")
        self.assertEqual(meta["method"], "json_ld_datePublished")

    def test_article_without_a_stated_date_is_not_published(self):
        html = "<html><body><h1>Om oss</h1><p>Vi lager møbler.</p></body></html>"
        self.assertIsNone(signals.article_meta("https://firma.no/om-oss", html, soup(html)))

    def test_feed_drops_placeholders_and_other_sites(self):
        feed = (
            b'<?xml version="1.0"?><rss><channel>'
            b"<item><title>Hei verden!</title><link>https://firma.no/hei-verden</link><pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item>"
            b"<item><title>Ny avtale signert</title><link>https://firma.no/ny-avtale</link><pubDate>Tue, 02 Sep 2025 08:30:00 +0200</pubDate></item>"
            b"<item><title>Ekstern sak</title><link>https://avis.no/sak</link><pubDate>Tue, 02 Sep 2025 08:30:00 +0200</pubDate></item>"
            b"</channel></rss>"
        )
        items = signals.parse_feed("https://firma.no/feed/", feed)
        self.assertEqual([item["url"] for item in items], ["https://firma.no/ny-avtale"])
        self.assertEqual(items[0]["published_at"], "2025-09-02T08:30:00+02:00")

    def test_listing_items_need_a_time_element_and_same_site_link(self):
        page = soup(
            '<article><h2><a href="/aktuelt/sak-1">Første sak</a></h2><time datetime="2025-05-01">1. mai</time></article>'
            '<article><h2><a href="/aktuelt/sak-2">Uten dato</a></h2></article>'
        )
        items = signals.listing_items("https://firma.no/aktuelt", page)
        self.assertEqual([(item["url"], item["published_at"]) for item in items], [("https://firma.no/aktuelt/sak-1", "2025-05-01")])

    def test_feed_links_exclude_comment_feeds(self):
        page = soup(
            '<link rel="alternate" type="application/rss+xml" href="/feed/">'
            '<link rel="alternate" type="application/rss+xml" href="/comments/feed/">'
        )
        self.assertEqual(signals.feed_links("https://firma.no/", page), ["https://firma.no/feed/"])

    def test_window_rejects_old_and_future_items(self):
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        self.assertTrue(signals.within_days("2025-09-22T20:00:00+02:00", now, 1095))
        self.assertFalse(signals.within_days("2019-01-01", now, 1095))
        self.assertFalse(signals.within_days("2027-01-01", now, 1095))


class IdentityExtensionTests(unittest.TestCase):
    def test_full_name_without_separators_is_exact(self):
        row = {"organisation_number": "999999999", "name": "AFRODITES SKJØNNHET AS", "evidence": {"website": {"status": "available", "value": {"title": "Afrodite`s Skjønnhet"}}}}
        self.assertTrue(assess_website_identity(row)["publishable"])

    def test_partial_name_is_still_not_exact(self):
        row = {"organisation_number": "999999999", "name": "ELEKTRON COMMUNICATIONS AS", "evidence": {"website": {"status": "available", "value": {"title": "elektron.no – data, enøk, smarthus", "final_url": "https://www.elektron.no/"}}}}
        self.assertFalse(assess_website_identity(row)["publishable"])

    def test_social_handle_may_carry_the_verified_site_label(self):
        profile = {"name": "Norsk Emballasje Holding AS"}
        link = {"platform": "facebook", "url": "https://facebook.com/elopak"}
        self.assertFalse(assess_social_identity(profile, link)["publishable"])
        self.assertTrue(assess_social_identity(profile, link, "elopak.com")["publishable"])
        self.assertFalse(assess_social_identity(profile, {"platform": "facebook", "url": "https://facebook.com/someoneelse"}, "elopak.com")["publishable"])

    def test_mailbox_providers_are_never_site_candidates(self):
        def profile(address: str) -> dict:
            return {"evidence": {"registry_live": {"value": normalize_entity({"epostadresse": address})}}}

        self.assertIsNone(email_domain_candidate(profile("ola@gmail.com"), set()))
        self.assertIsNone(email_domain_candidate(profile("ola@online.no"), set()))
        self.assertEqual(email_domain_candidate(profile("post@sandneselektriske.no"), set()), "sandneselektriske.no")
        self.assertIsNone(email_domain_candidate(profile("post@sandneselektriske.no"), {"sandneselektriske.no"}))

    def test_entity_keeps_only_the_email_domain(self):
        value = normalize_entity({"epostadresse": "Ola.Nordmann@Firma.NO"})
        self.assertEqual(value["email_domain"], "firma.no")
        self.assertNotIn("Ola", str(value))


class _Page:
    sha256 = "d" * 64


def _pages(markup: str) -> dict:
    return {"https://firma.no/kontakt": (_Page(), markup, soup(markup))}


class NameDomainDiscoveryTests(unittest.TestCase):
    def _profile(self) -> dict:
        live = normalize_entity({"telefon": "51 68 57 00", "forretningsadresse": {"postnummer": "4306", "poststed": "SANDNES"}})
        return {"organisation_number": "810034882", "name": "SANDNES ELEKTRISKE AS", "evidence": {"registry_live": {"value": live}}}

    def test_candidates_are_spelled_from_the_full_legal_name(self):
        self.assertEqual(name_domain_candidates(self._profile(), set()), ["sandneselektriske.no", "sandnes-elektriske.no"])
        self.assertEqual(name_domain_candidates({"name": "ADV INVEST AS"}, set()), [])
        self.assertEqual(name_domain_candidates({"name": "BO AS"}, set()), [])

    def test_site_needs_this_entitys_number_phone_or_postcode_and_place(self):
        profile = self._profile()
        self.assertEqual(entity_proof(profile, _pages("<p>Org.nr. 810 034 882 MVA</p>"))["type"], "organisation_number_on_site")
        self.assertEqual(entity_proof(profile, _pages("<p>Ring oss: 51 68 57 00</p>"))["type"], "registered_phone_on_site")
        self.assertEqual(entity_proof(profile, _pages("<p>Gata 1, 4306 Sandnes</p>"))["type"], "registered_postcode_and_place_on_site")
        self.assertIsNone(entity_proof(profile, _pages("<p>Org.nr. 999 888 777. Gata 1, 0150 Oslo. Tlf 22 33 44 55</p>")))


class NavVacancyTests(unittest.TestCase):
    def test_vacancy_is_published_only_on_employer_number_match(self):
        import norway_company_agent.nav_jobs as nav_jobs

        entries = {
            "u1": {"status": "ACTIVE", "ad_content": {"title": "Elektriker", "published": "2026-10-01T08:00:00+02:00", "applicationDue": "2026-10-20", "link": "https://arbeidsplassen.nav.no/stillinger/stilling/u1", "employer": {"name": "Sandnes Elektriske AS", "orgnr": "973477986"}, "contactList": [{"name": "Ola"}]}},
            "u2": {"status": "ACTIVE", "ad_content": {"title": "Rørlegger", "employer": {"name": "Sandnes Elektriske AS", "orgnr": "999999999"}}},
        }

        def fake_json(url, token, extra=None):
            return entries[url.rsplit("/", 1)[1]], _Page()

        index = NavIndex(days=1)
        index.active = {"u1": {"title": "Elektriker", "business": "Sandnes Elektriske AS"}, "u2": {"title": "Rørlegger", "business": "SANDNES ELEKTRISKE AS"}, "u3": {"title": "Kokk", "business": "Annet Firma AS"}}
        original, nav_jobs._json = nav_jobs._json, fake_json
        try:
            found = index.postings_for({"810034882": {"names": ["SANDNES ELEKTRISKE AS"], "orgnrs": {"810034882", "973477986"}}})
        finally:
            nav_jobs._json = original
        self.assertEqual(list(found), ["810034882"])
        self.assertEqual([row["title"] for row in found["810034882"]], ["Elektriker"])
        self.assertNotIn("Ola", str(found))

    def test_company_keys_include_registered_workplaces(self):
        profile = {"organisation_number": "810034882", "name": "SANDNES ELEKTRISKE AS", "evidence": {"locations": {"value": {"locations": [{"organisation_number": "973477986", "name": "SANDNES ELEKTRISKE AVD FORUS"}]}}}}
        keys = company_keys(profile)
        self.assertEqual(keys["orgnrs"], {"810034882", "973477986"})
        self.assertEqual(name_key("Sandnes Elektriske AS"), name_key("SANDNES ELEKTRISKE AS"))


class EnvelopeTests(unittest.TestCase):
    def _profile(self) -> dict:
        live = evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/enhetsregisteret/api/enheter/923609016", value=normalize_entity({
            "organisasjonsnummer": "923609016", "navn": "EKSEMPEL AS", "organisasjonsform": {"kode": "AS"},
            "aktivitet": ["Produksjon av møbler"], "forretningsadresse": {"adresse": ["Gata 1"], "postnummer": "0001", "poststed": "OSLO"},
        }), content_sha256="a" * 64)
        website = evidence("website", "available", "registry_linked_company_website", "https://eksempel.no/", value={
            "final_url": "https://eksempel.no/", "registered_domain": "eksempel.no", "title": "Eksempel AS", "description": "",
            "social_links": [{"platform": "facebook", "url": "https://facebook.com/eksempel"}],
            "identity_assessment": {"publishable": True, "score": 0.95, "reasons": ["exact"]},
        }, content_sha256="b" * 64)
        news = evidence("news", "available", "company_owned_news", "https://eksempel.no/nyheter/a", value={"items": [{
            "title": "Ny avtale", "published_at": "2025-09-02T08:30:00+02:00", "url": "https://eksempel.no/nyheter/a",
            "value": "Ny avtale (2025-09-02T08:30:00+02:00)", "claim_span": "Ny avtale", "date_span": "2025-09-02T08:30:00+02:00",
            "source_url": "https://eksempel.no/nyheter/a", "content_sha256": "c" * 64, "retrieved_at": "2026-10-06T00:00:00Z", "extraction_method": "json_ld_datePublished",
        }]})
        hiring = evidence("hiring", "not_found", "company_owned_careers_page", "https://eksempel.no/", note="No careers page or vacancy was found on the verified company site")
        return {"organisation_number": "923609016", "name": "EKSEMPEL AS", "legal_form": "AS", "evidence": {"registry_live": live, "website": website, "news": news, "hiring": hiring}}

    def test_every_claim_resolves_to_evidence_and_unknowns_are_explicit(self):
        claims, rows = build_claims(self._profile())
        ids = {row["id"] for row in rows}
        self.assertTrue(all(set(claim["evidence_ids"]) <= ids for claim in claims))
        by_field = {claim["field"]: claim for claim in claims}
        self.assertEqual(by_field["official_website"]["value"], "https://eksempel.no/")
        self.assertEqual(by_field["dated_news"]["value"], "Ny avtale (2025-09-02T08:30:00+02:00)")
        self.assertEqual(by_field["hiring_signal"]["availability"], "not_available")
        self.assertIsNone(by_field["financials.latest"]["value"])
        self.assertTrue(all(not isinstance(claim["value"], (list, dict)) for claim in claims))

    def test_claims_are_identical_across_builds(self):
        def stable(result):
            claims, rows = result
            return claims, [{key: value for key, value in row.items() if key != "retrieved_at"} for row in rows]

        self.assertEqual(stable(build_claims(self._profile())), stable(build_claims(self._profile())))

    def test_brief_cites_every_sentence_and_lists_unknowns(self):
        brief = build_brief(self._profile())
        self.assertTrue(all(item["source_url"] for item in brief["summary"]))
        self.assertIn("hiring", {item["topic"] for item in brief["unknown"]})
        self.assertIn("latest filed accounts", {item["topic"] for item in brief["unknown"]})
        text = summary_text(brief)
        self.assertIn("Changes:", text)
        self.assertIn("Unknowns:", text)

    def test_refresh_ignores_retrieval_metadata(self):
        previous, current = self._profile(), self._profile()
        current["evidence"]["news"]["value"]["items"][0]["retrieved_at"] = "2026-10-07T00:00:00Z"
        self.assertEqual(diff_profile(previous, current), [])
        current["evidence"]["news"]["value"]["items"][0]["value"] = "Annen sak (2025-10-01)"
        self.assertEqual([change["field"] for change in diff_profile(previous, current)], ["news.items"])

    def test_number_missing_from_snapshot_still_gets_an_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            bulk = Path(directory) / "bulk.csv.gz"
            with gzip.open(bulk, "wt", encoding="utf-8", newline="") as handle:
                handle.write('"organisasjonsnummer","navn","organisasjonsform.kode"\n"923609016","EKSEMPEL AS","AS"\n')
            profiles, metadata = profiles_from_bulk(bulk, ["923609016", "999999999"])
        self.assertEqual(metadata["absent_from_snapshot"], ["999999999"])
        self.assertEqual(profiles[1]["evidence"]["registry"]["status"], "not_found")
        envelopes = [terminal_envelope(profile, run_id="t", modules=["registry"], started_at="a", completed_at="b") for profile in profiles]
        self.assertTrue(validate_envelopes(envelopes, 2)["passed"])
        self.assertEqual(list(envelopes[0]), ["run_id", "organisation_number", "state", "started_at", "completed_at", "modules", "profile"])


if __name__ == "__main__":
    unittest.main()
