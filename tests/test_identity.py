import json
import unittest
from urllib.parse import parse_qs, urlparse

from finder import identity


TITLE = "Defect-engineered interfaces via methyl gallate–modified ZIF-8 for plasticization-resistant propylene/propane separation membranes"
DOI = "10.1016/j.memsci.2026.125296"
PII = "S0376738826001766"


def work(item):
    return 200, {}, json.dumps({"message": item}).encode()


class IdentityTests(unittest.TestCase):
    def test_formatted_encoded_and_publisher_numeric_pii(self):
        self.assertEqual(identity.extract_pii("S0167-4889(08)00116-X"), "S016748890800116X")
        self.assertEqual(identity.extract_pii("https%253A%252F%252Fwww.sciencedirect.com%252Fscience%252Farticle%252Fpii%252FS0376738826001766"), PII)
        self.assertEqual(identity.extract_pii("https://www.sciencedirect.com/science/article/pii/0376738826001766"), PII)
        self.assertEqual(identity.extract_pii("https://unrelated.test/pii/0376738826001766"), "")
        self.assertEqual(identity.normalize_doi("DOI: https://doi.org/10.1000/ABC(12))."), "10.1000/abc(12)")

    def test_crossref_resource_primary_link_is_used_and_input_doi_normalized(self):
        calls = []
        def fetch(url, timeout):
            calls.append(url)
            return work({"DOI": DOI, "title": [TITLE], "resource": {"primary": {"URL": identity.article_url(PII)}}})
        result = identity.resolve_pii(doi="https://doi.org/" + DOI.upper() + ".", fetch=fetch)
        self.assertEqual(result["pii"], PII)
        self.assertEqual(result["doi"], DOI)
        self.assertEqual(result["title"], TITLE)
        self.assertEqual(len(calls), 1)

    def test_doi_failure_falls_back_to_exact_title_and_corrects_metadata(self):
        calls = []
        def fetch(url, timeout):
            calls.append(url)
            if "query.bibliographic" in url:
                return work({"items": [{"DOI": DOI, "title": [TITLE], "alternative-id": PII}]})
            return 404, {}, b"missing"
        result = identity.resolve_pii(url="https://doi.org/10.1000/wrong", doi="10.1000/wrong", title=TITLE, fetch=fetch)
        self.assertEqual(result["pii"], PII)
        self.assertEqual(result["doi"], DOI)
        self.assertEqual(result["source"], "crossref_title")
        self.assertEqual(calls.count("https://doi.org/10.1000/wrong"), 1)
        self.assertEqual(parse_qs(urlparse(calls[-1]).query)["query.bibliographic"], [TITLE])

    def test_crossref_timeout_does_not_cancel_linkinghub_redirect(self):
        def fetch(url, timeout):
            if "api.crossref" in url:
                raise TimeoutError()
            if "doi.org" in url:
                return 404, {}, b"missing"
            return 302, {"Location": identity.article_url(PII)}, b""
        result = identity.resolve_pii(doi=DOI, fetch=fetch)
        self.assertEqual(result["pii"], PII)
        self.assertEqual(result["source"], "publisher")

    def test_compact_title_tolerates_lost_spaces_and_markup(self):
        title = "[补充材料求助] Cross-tissue atlas of mucosa-associated lymphoidtissue lymphomas reveals intratumoralheterogeneity and microenvironmental subtypes"
        canonical = "Cross tissue atlas of mucosa–associated lymphoid tissue lymphomas reveals intratumoral heterogeneity and microenvironmental subtypes"
        def fetch(url, timeout):
            return work({"items": [{"title": [canonical], "DOI": DOI, "link": [{"URL": identity.article_url(PII)}]}]})
        result = identity.resolve_pii(title=title, fetch=fetch)
        self.assertEqual(result["pii"], PII)
        self.assertEqual(identity.normalized_title("<i>Ｍｅｍｂｒａｎｅ</i> &amp; transport"), "membrane transport")

    def test_exact_title_is_prioritized_over_compact_match(self):
        title = "A study on membrane transport"
        def fetch(url, timeout):
            return work({"items": [
                {"title": ["A study on membranetransport"], "alternative-id": "S0043135424017846"},
                {"title": [title], "alternative-id": PII},
            ]})
        self.assertEqual(identity.resolve_pii(title=title, fetch=fetch)["pii"], PII)

    def test_ambiguous_title_never_picks_first_hit(self):
        def fetch(url, timeout):
            return work({"items": [{"title": [TITLE], "alternative-id": pii} for pii in [PII, "S0043135424017846"]]})
        result = identity.resolve_pii(title=TITLE, fetch=fetch)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["pii"], "")

    def test_different_words_are_not_a_match(self):
        def fetch(url, timeout):
            return work({"items": [{"title": [TITLE.replace("propylene/propane", "ethylene/ethane")], "alternative-id": PII}]})
        self.assertEqual(identity.resolve_pii(title=TITLE, fetch=fetch)["pii"], "")

    def test_error_page_comments_scripts_and_related_json_are_ignored(self):
        pages = [
            f'<h1>Article Locator Error</h1><!-- {PII} --><meta name="citation_pii" content="{PII}">',
            f'<h1>A different article</h1><script>{{"related":{{"pii":"{PII}"}}}}</script><!-- {PII} -->',
        ]
        for page in pages:
            result = identity.resolve_pii(url="https://linkinghub.elsevier.com/retrieve/article", fetch=lambda *_: (200, {}, page.encode()))
            self.assertEqual(result["pii"], "")

    def test_structured_article_identity_and_doi_conflict(self):
        html = f'<meta name="Citation_PII" content="{PII}"><meta name="citation_doi" content="{DOI}">'
        self.assertEqual(identity.resolve_pii(url="https://www.sciencedirect.com/science/article/abstract", fetch=lambda *_: (200, {}, html.encode()))["pii"], PII)
        def fetch(url, timeout):
            return (404, {}, b"") if "crossref" in url else (200, {}, html.encode())
        self.assertEqual(identity.resolve_pii(doi="10.1000/different", fetch=fetch)["pii"], "")

    def test_untrusted_redirect_is_not_requested_and_budget_is_bounded(self):
        calls = []
        def fetch(url, timeout):
            calls.append(url)
            return 302, {"Location": "http://127.0.0.1/private"}, b""
        identity.resolve_pii(url="https://doi.org/10.1000/a", fetch=fetch)
        self.assertTrue(all(url.startswith("https://") and "127.0.0.1" not in url for url in calls))
        calls.clear()
        result = identity.resolve_pii(doi=DOI, fetch=fetch, budget=0)
        self.assertEqual(calls, [])
        self.assertTrue(result["retryable"])

    def test_keyword_title_fallback_without_provider_metadata(self):
        result = identity.resolve_pii(keyword=TITLE, fetch=lambda *_: work({"items": [{"title": [TITLE], "alternative-id": PII}]}))
        self.assertEqual(result["pii"], PII)


if __name__ == "__main__":
    unittest.main()
