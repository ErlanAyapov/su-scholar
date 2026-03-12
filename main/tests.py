from datetime import date
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from main.models import Language, Publication, PublicationType, Venue
from main.services.publication_importer import (
    _build_record_id,
    _fetch_scholar_profile_works,
    _safe_year,
    import_works_from_scholar,
)

User = get_user_model()


class ScholarParsingTests(SimpleTestCase):
    def test_safe_year_does_not_fallback_to_current_year(self):
        self.assertEqual(_safe_year(""), 0)
        self.assertEqual(_safe_year("unknown"), 0)
        self.assertEqual(_safe_year("NovaInfo. Ru 2 (32), 25-32, 2015"), 2015)

    @patch("main.services.publication_importer._resolve_scholar_user_id", return_value="adnm_LkAAAAJ")
    @patch("main.services.publication_importer.requests.get")
    def test_fetch_scholar_profile_works_parses_years_from_author_table(self, mock_get, _mock_resolve):
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.text = """
        <tbody id="gsc_a_b">
            <tr class="gsc_a_tr">
                <td class="gsc_a_t">
                    <a href="/citations?view_op=view_citation&amp;hl=en&amp;user=adnm_LkAAAAJ&amp;citation_for_view=adnm_LkAAAAJ:IjCSPb-OGe4C" class="gsc_a_at">Задачи и функций логистической системы</a>
                    <div class="gs_gray">ЕК Майлыбаев, ГИ Хасенова</div>
                    <div class="gs_gray">NovaInfo. Ru 2 (32), 25-32<span class="gs_oph">, 2015</span></div>
                </td>
                <td class="gsc_a_c"><a class="gsc_a_ac gs_ibl">2</a></td>
                <td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">2015</span></td>
            </tr>
            <tr class="gsc_a_tr">
                <td class="gsc_a_t">
                    <a href="/citations?view_op=view_citation&amp;hl=en&amp;user=adnm_LkAAAAJ&amp;citation_for_view=adnm_LkAAAAJ:ufrVoPGSRksC" class="gsc_a_at">Опыт применения онлайн-преподавания</a>
                    <div class="gs_gray">ГС Морокина, У Умбетов</div>
                    <div class="gs_gray">Современное образование<span class="gs_oph">, 2019</span></div>
                </td>
                <td class="gsc_a_c"><a class="gsc_a_ac gs_ibl">1</a></td>
                <td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl">2019</span></td>
            </tr>
            <tr class="gsc_a_tr">
                <td class="gsc_a_t">
                    <a href="/citations?view_op=view_citation&amp;hl=en&amp;user=adnm_LkAAAAJ&amp;citation_for_view=adnm_LkAAAAJ:3fE2CSJIrl8C" class="gsc_a_at">ИНФОРМАЦИОННЫЕ ТЕХНОЛОГИИ</a>
                    <div class="gs_gray">ЕК МАЙЛЫБАЕВ, У УМБЕТОВ</div>
                    <div class="gs_gray">ВЕСТНИК ТОРАЙГЫРОВ УНИВЕРСИТЕТА<span class="gs_oph">, 0</span></div>
                </td>
                <td class="gsc_a_c"><a class="gsc_a_ac gs_ibl"></a></td>
                <td class="gsc_a_y"><span class="gsc_a_h gsc_a_hc gs_ibl"></span></td>
            </tr>
        </tbody>
        """
        mock_get.return_value = mock_response

        works = _fetch_scholar_profile_works("adnm_LkAAAAJ")

        self.assertEqual(len(works), 3)
        self.assertEqual(works[0]["year"], 2015)
        self.assertEqual(works[1]["year"], 2019)
        self.assertEqual(works[2]["year"], 0)
        self.assertEqual(works[0]["venue"], "NovaInfo. Ru 2 (32), 25-32")
        self.assertEqual(works[2]["venue"], "ВЕСТНИК ТОРАЙГЫРОВ УНИВЕРСИТЕТА")


class ScholarImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="scholar-user",
            password="testpass123",
            first_name="Ерлан",
            last_name="Майлыбаев",
        )
        self.language = Language.objects.create(code="und", name="Unknown")
        self.pub_type = PublicationType.objects.create(name="Journal Article")
        self.venue = Venue.objects.create(
            name="Unknown venue",
            kind="journal",
            character="scientific_journal",
        )

    @patch("main.services.publication_importer._resolve_scholar_user_id", return_value="adnm_LkAAAAJ")
    @patch("main.services.publication_importer._fetch_scholar_profile_works")
    def test_import_updates_existing_scholar_publication_year(self, mock_fetch, _mock_resolve):
        source_id = "adnm_LkAAAAJ:IjCSPb-OGe4C"
        record_id = _build_record_id("google_scholar", source_id, "")
        publication = Publication.objects.create(
            record_id=record_id,
            pub_type=self.pub_type,
            title_original="Задачи и функций логистической системы",
            language=self.language,
            year=date.today().year,
            venue=self.venue,
            created_by=self.user,
        )

        mock_fetch.return_value = [
            {
                "source": "google_scholar",
                "source_id": source_id,
                "title": "Задачи и функций логистической системы",
                "venue": "NovaInfo. Ru 2 (32), 25-32",
                "year": 2015,
                "pub_type": "journal-article",
                "language": "und",
                "doi": "",
                "url_publisher": "https://scholar.google.com/citations?view_op=view_citation",
                "open_access": False,
                "external_ids": [],
                "authors": [],
            }
        ]

        result = import_works_from_scholar(user=self.user, query="adnm_LkAAAAJ")

        publication.refresh_from_db()

        self.assertEqual(publication.year, 2015)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)
