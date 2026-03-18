from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from main.models import Author, Language, Publication, PublicationAuthor, PublicationType, Venue
from main.services.publication_pipeline import (
    detect_source_type,
    link_authors_to_users,
    merge_payloads,
    normalize_payload,
    update_publication_from_payload,
    validate_payload,
)
from main.services.publication_pipeline.utils import build_empty_payload, extract_quartile_data

User = get_user_model()


class PipelineDetectionTests(SimpleTestCase):
    def test_detects_google_scholar(self):
        html = '<div class="gs_rt">Example result</div><a href="/scholar?cites=123">Cited by 4</a>'
        self.assertEqual(detect_source_type("https://scholar.google.com/scholar?q=test", html), "google_scholar")

    def test_detects_doi_page(self):
        self.assertEqual(detect_source_type("https://doi.org/10.1000/test-doi"), "doi_page")


class PipelineNormalizationTests(SimpleTestCase):
    def test_normalize_pages_single_number_sets_total_pages(self):
        payload = build_empty_payload()
        payload["publication"]["title_original"] = "Example"
        payload["publication"]["year"] = 2024
        payload["publication"]["pages"] = "16"
        payload["publication"]["doi"] = "https://doi.org/10.1000/xyz-1"
        payload["source_meta"]["source_type"] = "publisher_page"
        payload["source_meta"]["source_url"] = "https://example.com/article"

        normalized = normalize_payload(payload)

        self.assertEqual(normalized["publication"]["pages"], "")
        self.assertEqual(normalized["publication"]["total_pages"], 16)
        self.assertEqual(normalized["publication"]["doi"], "10.1000/XYZ-1")
        self.assertIn({"id_type": "doi", "value": "10.1000/XYZ-1"}, normalized["identifiers"])

    def test_normalize_quartile_variants(self):
        payload = build_empty_payload()
        payload["publication"]["title_original"] = "Example"
        payload["publication"]["year"] = 2024
        payload["publication"]["quartile"] = "Quartile 1"
        payload["publication"]["quartile_year"] = None
        payload["source_meta"]["source_type"] = "publisher_page"
        payload["source_meta"]["source_url"] = "https://example.com/article"

        normalized = normalize_payload(payload)

        self.assertEqual(normalized["publication"]["quartile"], "Q1")
        self.assertEqual(normalized["publication"]["quartile_year"], 2024)

    def test_merge_prefers_publisher_abstract_over_scholar_snippet(self):
        scholar = build_empty_payload()
        scholar["publication"]["title_original"] = "Example title"
        scholar["publication"]["year"] = 2024
        scholar["publication"]["abstract"] = "Short snippet from Scholar."
        scholar["publication"]["needs_review"] = True
        scholar["links"]["scholar_url"] = "https://scholar.google.com/scholar?q=example"
        scholar["source_meta"]["source_type"] = "google_scholar"

        publisher = build_empty_payload()
        publisher["publication"]["title_original"] = "Example title"
        publisher["publication"]["year"] = 2024
        publisher["publication"]["abstract"] = (
            "This paper proposes a complete publisher-side abstract that is much longer and more reliable "
            "than a search snippet."
        )
        publisher["links"]["url_publisher"] = "https://publisher.example.org/article"
        publisher["source_meta"]["source_type"] = "publisher_page"

        merged = merge_payloads([scholar, publisher])

        self.assertIn("complete publisher-side abstract", merged["publication"]["abstract"])
        self.assertEqual(merged["links"]["url_publisher"], "https://publisher.example.org/article")

    def test_extract_quartile_data_from_text_and_metrics(self):
        extracted = extract_quartile_data(
            "Indexed in Scopus. Quartile 1 (2024). CiteScore 7.4. SJR 1.25."
        )

        self.assertEqual(extracted["quartile"], "Q1")
        self.assertEqual(extracted["quartile_year"], 2024)
        metric_names = {item["metric"] for item in extracted["venue_metrics"]}
        self.assertIn("citescore", metric_names)
        self.assertIn("sjr", metric_names)


class PipelineUpdaterTests(TestCase):
    def setUp(self):
        self.language = Language.objects.create(code="en", name="English")
        self.pub_type = PublicationType.objects.create(name="Journal Article")
        self.venue = Venue.objects.create(name="Initial Venue", kind="journal", character="scientific_journal")
        self.publication = Publication.objects.create(
            record_id="pipeline-test-record-1",
            pub_type=self.pub_type,
            title_original="Initial title",
            language=self.language,
            year=2023,
            venue=self.venue,
        )

    def test_update_publication_from_payload_creates_authors_and_identifier(self):
        payload = build_empty_payload()
        payload["publication"]["title_original"] = "Initial title"
        payload["publication"]["year"] = 2023
        payload["publication"]["doi"] = "10.1000/ABC-1"
        payload["authors"] = [
            {
                "full_name": "Alice Example",
                "raw_name": "Alice Example",
                "orcid": "",
                "affiliations": "",
                "is_department_staff": False,
                "order": 1,
                "role": "first",
            },
            {
                "full_name": "Bob Example",
                "raw_name": "Bob Example",
                "orcid": "",
                "affiliations": "",
                "is_department_staff": False,
                "order": 2,
                "role": "coauthor",
            },
        ]
        payload["identifiers"] = [{"id_type": "doi", "value": "10.1000/ABC-1"}]
        payload["source_meta"]["source_type"] = "publisher_page"
        payload["source_meta"]["source_url"] = "https://example.com/article"

        validation = validate_payload(normalize_payload(payload))
        self.assertTrue(validation["is_valid"], validation["errors"])

        update_publication_from_payload(self.publication, validation["payload"])

        self.publication.refresh_from_db()
        self.assertEqual(self.publication.doi, "10.1000/ABC-1")
        self.assertEqual(Author.objects.count(), 2)
        self.assertEqual(PublicationAuthor.objects.filter(publication=self.publication).count(), 2)
        first_link = PublicationAuthor.objects.get(publication=self.publication, order=1)
        self.assertEqual(first_link.role, "first")

    def test_author_linking_matches_user_across_scripts_and_initials(self):
        user = User.objects.create_user(
            username="nurtay-user",
            password="testpass123",
            first_name="Nurtay",
            last_name="Albanbay",
            father_name="",
            orc_id="0000-0001-1111-1111",
        )

        linked_authors, logs = link_authors_to_users(
            [
                {
                    "full_name": "Нуртай Албанбай",
                    "raw_name": "Нуртай Албанбай",
                    "orcid": "",
                    "affiliations": "",
                    "is_department_staff": False,
                    "order": 1,
                    "role": "first",
                },
                {
                    "full_name": "N Albanbay",
                    "raw_name": "N Albanbay",
                    "orcid": "",
                    "affiliations": "",
                    "is_department_staff": False,
                    "order": 2,
                    "role": "coauthor",
                },
            ],
            return_logs=True,
        )

        self.assertEqual(linked_authors[0]["matched_user_id"], user.id)
        self.assertTrue(linked_authors[0]["is_department_staff"])
        self.assertEqual(linked_authors[0]["name_initials"], "n albanbay")
        self.assertGreaterEqual(linked_authors[1]["match_confidence"], 0.75)
        self.assertEqual(len(logs), 2)
