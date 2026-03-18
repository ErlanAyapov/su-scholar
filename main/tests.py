from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from account.models import Department, Institute, University
from main.context_processors import layout_navigation
from main.models import Language, Publication, PublicationType, Venue
from main.services.publication_importer import (
    _build_record_id,
    _extract_abstract_from_html,
    _fetch_scholar_profile_works,
    _safe_year,
    import_works_from_scholar,
)
from main.utils import build_search_page_context
from project.asgi import application

User = get_user_model()


class LayoutNavigationTests(SimpleTestCase):
    def _build_request(self, *, url_name: str, query: str = ""):
        request = SimpleNamespace(
            resolver_match=SimpleNamespace(url_name=url_name),
            GET=QueryDict(query),
        )
        return request

    def test_main_page_marks_home_as_active(self):
        request = self._build_request(url_name="main")

        nav = layout_navigation(request)["layout_nav"]["active"]

        self.assertTrue(nav["home"])
        self.assertFalse(nav["researchers"])
        self.assertFalse(nav["projects"])
        self.assertFalse(nav["publications"])
        self.assertFalse(nav["analytics"])
        self.assertFalse(nav["account"])

    def test_documents_search_tab_marks_publications_as_active(self):
        request = self._build_request(url_name="advanced_search", query="tab=documents")

        nav = layout_navigation(request)["layout_nav"]["active"]

        self.assertFalse(nav["home"])
        self.assertFalse(nav["researchers"])
        self.assertFalse(nav["projects"])
        self.assertTrue(nav["publications"])
        self.assertFalse(nav["analytics"])
        self.assertFalse(nav["account"])

    def test_researchers_search_tab_marks_researchers_as_active(self):
        request = self._build_request(url_name="advanced_search", query="tab=researchers")

        nav = layout_navigation(request)["layout_nav"]["active"]

        self.assertFalse(nav["home"])
        self.assertTrue(nav["researchers"])
        self.assertFalse(nav["projects"])
        self.assertFalse(nav["publications"])
        self.assertFalse(nav["analytics"])
        self.assertFalse(nav["account"])

    def test_projects_page_marks_projects_as_active(self):
        request = self._build_request(url_name="projects_grants_demo")

        nav = layout_navigation(request)["layout_nav"]["active"]

        self.assertFalse(nav["home"])
        self.assertFalse(nav["researchers"])
        self.assertTrue(nav["projects"])
        self.assertFalse(nav["publications"])
        self.assertFalse(nav["analytics"])
        self.assertFalse(nav["account"])

    def test_analytics_page_marks_analytics_as_active(self):
        request = self._build_request(url_name="analytics_page")

        nav = layout_navigation(request)["layout_nav"]["active"]

        self.assertFalse(nav["home"])
        self.assertFalse(nav["researchers"])
        self.assertFalse(nav["projects"])
        self.assertFalse(nav["publications"])
        self.assertTrue(nav["analytics"])
        self.assertFalse(nav["account"])

    def test_employee_profile_marks_account_as_active(self):
        request = self._build_request(url_name="employee_profile")

        nav = layout_navigation(request)["layout_nav"]["active"]

        self.assertFalse(nav["home"])
        self.assertFalse(nav["researchers"])
        self.assertFalse(nav["projects"])
        self.assertFalse(nav["publications"])
        self.assertFalse(nav["analytics"])
        self.assertTrue(nav["account"])


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


class ResearcherHierarchyFilterTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.university_1 = University.objects.create(name="University A")
        self.university_2 = University.objects.create(name="University B")
        self.institute_1 = Institute.objects.create(name="Institute A1", university=self.university_1)
        self.institute_2 = Institute.objects.create(name="Institute B1", university=self.university_2)
        self.department_1 = Department.objects.create(name="Department A1", institute=self.institute_1)
        self.department_2 = Department.objects.create(name="Department B1", institute=self.institute_2)

        self.user_1 = User.objects.create_user(
            username="researcher-a",
            password="testpass123",
            first_name="A",
            last_name="One",
            department=self.department_1,
        )
        self.user_2 = User.objects.create_user(
            username="researcher-b",
            password="testpass123",
            first_name="B",
            last_name="Two",
            department=self.department_2,
        )

    def test_researcher_filter_by_university(self):
        request = self.factory.get(
            "/advanced_search/",
            {
                "tab": "researchers",
                "researcher_university": str(self.university_1.id),
            },
        )

        context = build_search_page_context(request)
        result_ids = {user.id for user in context["researcher_results"]}

        self.assertTrue(context["researcher_show_results"])
        self.assertIn(self.user_1.id, result_ids)
        self.assertNotIn(self.user_2.id, result_ids)

    def test_researcher_filter_by_department_fills_parent_hierarchy(self):
        request = self.factory.get(
            "/advanced_search/",
            {
                "tab": "researchers",
                "researcher_department": str(self.department_1.id),
            },
        )

        context = build_search_page_context(request)

        self.assertEqual(context["researcher_department"], self.department_1.id)
        self.assertEqual(context["researcher_institute"], self.institute_1.id)
        self.assertEqual(context["researcher_university"], self.university_1.id)


class AbstractExtractionTests(SimpleTestCase):
    def test_extract_abstract_ignores_author_blocks_inside_abstract_section(self):
        html = """
        <section class="abstract">
            <div class="authors">Authors: A. A. Ivanov, M. K. Sadykova</div>
            <p>
                Abstract: This paper proposes a lightweight architecture for IoT telemetry
                processing with adaptive filtering and anomaly detection under noisy channels.
            </p>
        </section>
        """

        abstract = _extract_abstract_from_html(html)

        self.assertIn("This paper proposes a lightweight architecture", abstract)
        self.assertNotIn("Ivanov", abstract)
        self.assertNotIn("Authors:", abstract)

    def test_extract_abstract_from_heading_uses_content_after_heading_only(self):
        html = """
        <div class="article-body">
            <h2>Abstract</h2>
            <div class="byline">Authors: A. A. Ivanov, M. K. Sadykova</div>
            <p>
                We evaluate a university-scale dataset and show stable precision across
                multiple domains with reduced annotation costs.
            </p>
            <h3>Keywords</h3>
            <p>IoT, University analytics</p>
        </div>
        """

        abstract = _extract_abstract_from_html(html)

        self.assertIn("We evaluate a university-scale dataset", abstract)
        self.assertNotIn("Authors:", abstract)
        self.assertNotIn("Keywords", abstract)


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


class PublicationPipelineRunViewTests(TestCase):
    def setUp(self):
        self.language = Language.objects.create(code="en", name="English")
        self.pub_type = PublicationType.objects.create(name="Journal Article")
        self.venue = Venue.objects.create(name="Test Venue", kind="journal", character="scientific_journal")
        self.publication = Publication.objects.create(
            record_id="pipeline-run-view-test-1",
            pub_type=self.pub_type,
            title_original="Test Publication",
            language=self.language,
            year=date.today().year,
            venue=self.venue,
        )

    @patch("main.tasks.run_publication_pipeline_single_task.delay")
    def test_post_queues_single_publication_pipeline_task(self, mock_delay):
        mock_delay.return_value = SimpleNamespace(id="task-publication-pipeline-1")

        response = self.client.post(
            reverse("publication_pipeline_run", kwargs={"pk": self.publication.id}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["task_id"], "task-publication-pipeline-1")
        mock_delay.assert_called_once_with(publication_id=self.publication.id, force_refresh=True)

    def test_post_returns_404_for_missing_publication(self):
        response = self.client.post(reverse("publication_pipeline_run", kwargs={"pk": 999999}))

        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "not_found")


class LlmWebSocketTests(SimpleTestCase):
    def test_llm_websocket_connects_and_sends_ready(self):
        async def scenario():
            communicator = WebsocketCommunicator(application, "/ws/llm/")
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            payload = await communicator.receive_json_from()
            self.assertEqual(payload.get("type"), "llm_ready")

            await communicator.disconnect()

        async_to_sync(scenario)()

    def test_llm_websocket_rejects_empty_prompt(self):
        async def scenario():
            communicator = WebsocketCommunicator(application, "/ws/llm/")
            connected, _ = await communicator.connect()

            self.assertTrue(connected)
            await communicator.receive_json_from()  # llm_ready

            await communicator.send_json_to({"action": "ask", "prompt": ""})
            payload = await communicator.receive_json_from()

            self.assertEqual(payload.get("type"), "llm_error")
            self.assertEqual(payload.get("message"), "Prompt is empty")

            await communicator.disconnect()

        async_to_sync(scenario)()
