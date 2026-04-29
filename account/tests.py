import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from account.services.satbayev_scraper import extract_teacher_profile, load_teacher_profile_data
from account.tasks import _build_update_payload, _update_user_photo_from_satbayev, enqueue_satbayev_enrichment
from document.models import Document, DocumentGenerator
from main.models import Author, Language, Project, Publication, PublicationAuthor, PublicationProject, PublicationType, Venue

User = get_user_model()


class BulkOperationsAdminTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="adminpass123",
        )
        self.client.force_login(self.admin_user)
        self.url = reverse("admin:account_user_bulk_operations")
        self.changelist_url = reverse("admin:account_user_changelist")

    def test_bulk_operations_page_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Массовые операции")
        self.assertContains(response, "Live-логи")
        self.assertContains(response, "Publication Pipeline")
        self.assertContains(response, "Author Normalization")
        self.assertContains(response, "Relink Authors to Users")

    def test_changelist_contains_bulk_operations_button(self):
        response = self.client.get(self.changelist_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bulk-ops-toolbar__button")
        self.assertContains(response, reverse("admin:account_user_bulk_operations"))

    @patch("account.admin.import_publications_from_google_scholar_for_all_users_task.delay")
    def test_bulk_operations_ajax_queues_scholar_import_with_force(self, mock_delay):
        mock_delay.return_value = SimpleNamespace(id="task-123")

        response = self.client.post(
            self.url,
            {"operation": "import_scholar_all", "force": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task_id"], "task-123")
        self.assertTrue(response.json()["ok"])
        mock_delay.assert_called_once_with(force=True)

    @patch("account.admin.enrich_publications_with_abstracts_task.delay")
    def test_bulk_operations_ajax_queues_publication_abstract_enrichment(self, mock_delay):
        mock_delay.return_value = SimpleNamespace(id="task-abstracts-1")

        response = self.client.post(
            self.url,
            {"operation": "enrich_publications_abstracts_all", "limit": "25", "force": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["task_id"], "task-abstracts-1")
        self.assertTrue(payload["ok"])
        mock_delay.assert_called_once_with(limit=25, force=True)


    @patch("account.admin.run_publication_pipeline_batch_task.delay")
    def test_bulk_operations_ajax_queues_publication_pipeline_batch(self, mock_delay):
        mock_delay.return_value = SimpleNamespace(id="task-pipeline-1")

        response = self.client.post(
            self.url,
            {"operation": "run_publication_pipeline_batch", "limit": "30", "force": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["task_id"], "task-pipeline-1")
        self.assertTrue(payload["ok"])
        mock_delay.assert_called_once_with(limit=30, force_refresh=True)

    @patch("account.admin.backfill_author_normalization_task.delay")
    def test_bulk_operations_ajax_queues_author_normalization_backfill(self, mock_delay):
        mock_delay.return_value = SimpleNamespace(id="task-authors-1")

        response = self.client.post(
            self.url,
            {"operation": "backfill_author_normalization"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["task_id"], "task-authors-1")
        self.assertTrue(payload["ok"])
        mock_delay.assert_called_once_with(relink=True)

    @patch("account.admin.relink_authors_to_users_task.delay")
    def test_bulk_operations_ajax_queues_author_relink(self, mock_delay):
        mock_delay.return_value = SimpleNamespace(id="task-relink-1")

        response = self.client.post(
            self.url,
            {"operation": "relink_authors_to_users"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["task_id"], "task-relink-1")
        self.assertTrue(payload["ok"])
        mock_delay.assert_called_once_with()


class RegistrationActivationFlowTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staff-inactive",
            email="staff.inactive@satbayev.university",
            is_user=False,
            is_active=False,
        )
        self.active_user = User.objects.create_user(
            username="regular-user",
            email="regular.user@satbayev.university",
            password="RegularPass123!",
            is_user=True,
            is_active=True,
        )

    def test_register_email_status_returns_needs_activation_for_staff_profile(self):
        response = self.client.get(
            reverse("register_email_status"),
            {"email": self.staff_user.email},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "needs_activation")

    def test_register_email_status_returns_already_registered_for_regular_user(self):
        response = self.client.get(
            reverse("register_email_status"),
            {"email": self.active_user.email},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "already_registered")

    @patch("account.views.send_mail")
    def test_register_send_activation_email_sends_link(self, mock_send_mail):
        mock_send_mail.return_value = 1

        response = self.client.post(
            reverse("register_send_activation_email"),
            data=json.dumps({"email": self.staff_user.email}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        mock_send_mail.assert_called_once()
        kwargs = mock_send_mail.call_args.kwargs
        self.assertIn("/account/activate/", kwargs["message"])
        self.assertIn("subject", kwargs)
        self.assertEqual(kwargs["recipient_list"], [self.staff_user.email])

    def test_account_activate_marks_user_active_and_redirects_to_set_password(self):
        uidb64 = urlsafe_base64_encode(force_bytes(self.staff_user.pk))
        token = default_token_generator.make_token(self.staff_user)

        response = self.client.get(
            reverse("account_activate", kwargs={"uidb64": uidb64, "token": token}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(
            response,
            reverse("account_set_password", kwargs={"uidb64": uidb64, "token": token}),
            fetch_redirect_response=False,
        )
        self.staff_user.refresh_from_db()
        self.assertTrue(self.staff_user.is_active)

    def test_account_set_password_saves_password_and_redirects_to_login(self):
        uidb64 = urlsafe_base64_encode(force_bytes(self.staff_user.pk))
        token = default_token_generator.make_token(self.staff_user)

        response = self.client.post(
            reverse("account_set_password", kwargs={"uidb64": uidb64, "token": token}),
            {
                "new_password1": "StrongPass123!@#",
                "new_password2": "StrongPass123!@#",
                "next": reverse("account_page"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)
        self.staff_user.refresh_from_db()
        self.assertTrue(self.staff_user.check_password("StrongPass123!@#"))
        self.assertTrue(self.staff_user.is_active)


class AccountLoginByUsernameOrEmailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="login-user",
            email="login.user@satbayev.university",
            password="LoginPass123!",
            is_active=True,
            is_user=True,
        )

    def test_account_login_accepts_username(self):
        response = self.client.post(
            reverse("account_login"),
            {
                "username": self.user.username,
                "password": "LoginPass123!",
                "next": reverse("account_page"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("account_page"))
        self.assertEqual(str(self.client.session.get("_auth_user_id")), str(self.user.id))

    def test_account_login_accepts_email_case_insensitive(self):
        response = self.client.post(
            reverse("account_login"),
            {
                "username": "LOGIN.USER@SATBAYEV.UNIVERSITY",
                "password": "LoginPass123!",
                "next": reverse("account_page"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("account_page"))
        self.assertEqual(str(self.client.session.get("_auth_user_id")), str(self.user.id))


class EmployeeCreateViewTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="root-admin",
            email="root.admin@example.com",
            password="RootPass123!",
        )
        self.regular_user = User.objects.create_user(
            username="regular-member",
            email="regular.member@example.com",
            password="MemberPass123!",
            is_active=True,
            is_user=True,
        )

    def test_superuser_can_open_create_page(self):
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("employee_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add Inactive User")

    def test_non_superuser_cannot_open_create_page(self):
        self.client.force_login(self.regular_user)

        response = self.client.get(reverse("employee_create"))

        self.assertEqual(response.status_code, 403)

    def test_superuser_creates_inactive_user_without_password(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            reverse("employee_create"),
            {
                "username": "new.inactive.user",
                "first_name": "New",
                "last_name": "Inactive",
                "father_name": "NoPassword",
                "email": "new.inactive.user@satbayev.university",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("employee_create"), response.url)

        created_user = User.objects.get(username="new.inactive.user")
        self.assertFalse(created_user.is_active)
        self.assertFalse(created_user.is_user)
        self.assertFalse(created_user.is_staff)
        self.assertFalse(created_user.is_superuser)
        self.assertFalse(created_user.has_usable_password())


class EmployeeProfileSyncVisibilityTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="sync-admin",
            email="sync.admin@example.com",
            password="SyncAdminPass123!",
        )
        self.target_user = User.objects.create_user(
            username="sync-target",
            email="sync.target@example.com",
            password="SyncTargetPass123!",
            is_active=True,
            is_user=True,
        )
        self.pub_type = PublicationType.objects.create(name="Sync Fragment Journal")
        self.language = Language.objects.create(code="ru", name="Russian")
        self.venue = Venue.objects.create(
            name="Sync Fragment Venue",
            kind="journal",
            character="scientific_journal",
        )
        Publication.objects.create(
            record_id="sync-fragment-1",
            pub_type=self.pub_type,
            title_original="Realtime publication item",
            language=self.language,
            year=2026,
            status="published",
            venue=self.venue,
            created_by=self.target_user,
        )

    def test_superuser_sees_sync_controls_on_foreign_profile(self):
        self.client.force_login(self.superuser)

        response = self.client.get(
            reverse("employee_profile", kwargs={"user_id": self.target_user.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="userProfileSynchButton"', html=False)
        self.assertContains(response, 'id="userProfileSyncModal"', html=False)
        self.assertNotContains(response, 'id="userProfileEditButton"', html=False)

    def test_superuser_gets_publications_fragment(self):
        self.client.force_login(self.superuser)

        response = self.client.get(
            reverse("employee_profile_publications_fragment", kwargs={"user_id": self.target_user.id})
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertIn("Realtime publication item", payload.get("html", ""))
        self.assertIn("metric_texts", payload)
        self.assertIn("profileMetricPublicationsValue", payload.get("metric_texts", {}))


class EmployeeProfileExportTests(TestCase):
    def setUp(self):
        self.profile_user = User.objects.create_user(
            username="profile-owner",
            password="OwnerPass123!",
            first_name="Nurtay",
            last_name="Albanbay",
        )
        self.request_user = User.objects.create_user(
            username="request-user",
            password="RequesterPass123!",
            first_name="Report",
            last_name="User",
        )
        self.pub_type = PublicationType.objects.create(name="Journal Article")
        self.language = Language.objects.create(code="en", name="English")
        self.venue = Venue.objects.create(
            name="Test Journal",
            kind="journal",
            character="scientific_journal",
        )
        Publication.objects.create(
            record_id="profile-export-1",
            pub_type=self.pub_type,
            title_original="Profile export publication",
            language=self.language,
            year=2025,
            status="published",
            venue=self.venue,
            created_by=self.profile_user,
        )

    def _create_generator(self, *, title: str, access_to_all: bool, page: str):
        return DocumentGenerator.objects.create(
            title=title,
            content="Count: {{ publications|length }}",
            file="synthetic_documents/template.txt",
            file_type="txt",
            user=self.request_user,
            access_to_all=access_to_all,
            page=page,
        )

    def test_authenticated_export_creates_document_and_redirects_to_download(self):
        self.client.force_login(self.request_user)
        generator = self._create_generator(
            title="Employee export template",
            access_to_all=True,
            page="employee",
        )

        response = self.client.post(
            reverse("employee_profile_export", kwargs={"user_id": self.profile_user.id}),
            {"template_key": f"g:{generator.id}"},
        )

        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(generated_by=generator, user=self.request_user)
        expected_download_url = f"{reverse('document_file', kwargs={'pk': document.pk})}?download=1"
        self.assertEqual(response.url, expected_download_url)
        self.assertEqual(document.file_type, "txt")

    def test_export_rejects_templates_for_other_pages(self):
        self.client.force_login(self.request_user)
        generator = self._create_generator(
            title="Search-only template",
            access_to_all=True,
            page="main_search",
        )

        response = self.client.post(
            reverse("employee_profile_export", kwargs={"user_id": self.profile_user.id}),
            {"template_key": f"g:{generator.id}"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("employee_profile", kwargs={"user_id": self.profile_user.id}),
        )
        self.assertEqual(Document.objects.count(), 0)


class EmployeeProfileSyncApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="sync-user",
            password="SyncPass123!",
            email="sync.user@satbayev.university",
            is_user=True,
            is_active=True,
        )
        self.client.force_login(self.user)

    def test_sync_fields_saves_values(self):
        response = self.client.post(
            reverse("employee_profile_sync_fields", kwargs={"user_id": self.user.id}),
            data=json.dumps(
                {
                    "satbayev_profile_url": "https://official.satbayev.university/ru/teachers/example",
                    "scopus_id": "57222517592",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload["availability"]["has_satbayev"])
        self.assertTrue(payload["availability"]["has_scopus"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.scopus_id, "57222517592")

    def test_sync_start_requires_at_least_one_field(self):
        response = self.client.post(
            reverse("employee_profile_sync_start", kwargs={"user_id": self.user.id}),
            data=json.dumps({"mode": "full"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("At least one sync field is required", response.json()["detail"])

    @patch("account.views.sync_user_profile_full_cycle_task.delay")
    def test_sync_start_queues_task(self, mock_delay):
        mock_delay.return_value = SimpleNamespace(id="sync-task-1")
        self.user.scopus_id = "57222517592"
        self.user.save(update_fields=["scopus_id"])

        response = self.client.post(
            reverse("employee_profile_sync_start", kwargs={"user_id": self.user.id}),
            data=json.dumps({"mode": "full", "force": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["task_id"], "sync-task-1")
        mock_delay.assert_called_once_with(
            user_id=self.user.id,
            initiated_by_id=self.user.id,
            force=True,
            satbayev_only=False,
        )

    @override_settings(DEBUG=False)
    def test_sync_reset_forbidden_when_debug_disabled(self):
        response = self.client.post(
            reverse("employee_profile_sync_reset", kwargs={"user_id": self.user.id}),
            data=json.dumps({"confirm": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("DEBUG=True", response.json().get("detail", ""))

    @override_settings(DEBUG=True)
    def test_sync_reset_clears_profile_fields_and_related_data(self):
        self.user.orc_id = "0000-0002-4970-3095"
        self.user.scopus_id = "56021560300"
        self.user.wos_id = "https://publons.com/researcher/AAG-7613-2019/"
        self.user.google_scholar = "https://scholar.google.com/citations?user=PoBZUCYAAAAJ"
        self.user.researchgate = "https://www.researchgate.net/profile/Test"
        self.user.satbayev_profile_url = "https://official.satbayev.university/ru/teachers/test"
        self.user.journal_links = ["https://example.org/journal"]
        self.user.journal_ids = [{"type": "doi", "value": "10.1234/test"}]
        self.user.save(
            update_fields=[
                "orc_id",
                "scopus_id",
                "wos_id",
                "google_scholar",
                "researchgate",
                "satbayev_profile_url",
                "journal_links",
                "journal_ids",
            ]
        )

        pub_type = PublicationType.objects.create(name="Reset Journal")
        language = Language.objects.create(code="kk", name="Kazakh")
        venue = Venue.objects.create(
            name="Reset Venue",
            kind="journal",
            character="scientific_journal",
        )
        publication = Publication.objects.create(
            record_id="profile-reset-1",
            pub_type=pub_type,
            title_original="Profile reset publication",
            language=language,
            year=2025,
            status="published",
            venue=venue,
            created_by=self.user,
        )
        profile_author = Author.objects.create(full_name="Sync User", user=self.user)
        coauthor = Author.objects.create(full_name="Co Author")
        PublicationAuthor.objects.create(publication=publication, author=profile_author, order=1, role="first")
        PublicationAuthor.objects.create(publication=publication, author=coauthor, order=2, role="coauthor")
        project = Project.objects.create(name="Reset Project", project_type="other")
        PublicationProject.objects.create(publication=publication, project=project)

        response = self.client.post(
            reverse("employee_profile_sync_reset", kwargs={"user_id": self.user.id}),
            data=json.dumps({"confirm": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["stats"]["publications_deleted"], 1)
        self.assertEqual(payload["stats"]["authors_deleted"], 2)
        self.assertEqual(payload["stats"]["projects_deleted"], 1)

        self.user.refresh_from_db()
        self.assertEqual(self.user.orc_id, "")
        self.assertEqual(self.user.scopus_id, "")
        self.assertEqual(self.user.wos_id, "")
        self.assertEqual(self.user.google_scholar, "")
        self.assertEqual(self.user.researchgate, "")
        self.assertEqual(self.user.satbayev_profile_url, "")
        self.assertEqual(self.user.journal_links, [])
        self.assertEqual(self.user.journal_ids, [])

        self.assertFalse(Publication.objects.filter(id=publication.id).exists())
        self.assertFalse(Author.objects.filter(id=profile_author.id).exists())
        self.assertFalse(Author.objects.filter(id=coauthor.id).exists())
        self.assertFalse(Project.objects.filter(id=project.id).exists())

    @patch("account.views.AsyncResult")
    def test_sync_status_returns_success_payload(self, mock_async_result_cls):
        mock_async = Mock()
        mock_async.state = "SUCCESS"
        mock_async.ready.return_value = True
        mock_async.successful.return_value = True
        mock_async.result = {"status": "ok", "created_total": 3}
        mock_async_result_cls.return_value = mock_async

        response = self.client.get(
            reverse(
                "employee_profile_sync_status",
                kwargs={"user_id": self.user.id, "task_id": "sync-task-1"},
            )
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["status"], "ok")

    def test_anonymous_export_returns_attachment_without_saving_document(self):
        generator = self._create_generator(
            title="Public employee template",
            access_to_all=True,
            page="employee",
        )

        response = self.client.post(
            reverse("employee_profile_export", kwargs={"user_id": self.profile_user.id}),
            {"template_key": f"g:{generator.id}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertEqual(Document.objects.count(), 0)


class SatbayevScraperTests(SimpleTestCase):
    def test_extract_teacher_profile_decodes_email_and_photo(self):
        html = """
        <html>
          <head>
            <script>
              var emailEncrypts = {"university":"111","com":"222","ru":"333"};
            </script>
          </head>
          <body>
            <div class="teacher-header">
              <div class="header-left">
                <img src="/file/2025/12/14/2e73a9/_180-180.jpg" alt="Teacher">
              </div>
              <h1>Албанбай Нуртай</h1>
              <p><span>Email: <a class="forDecrypt" href="mailto:n.albanbay@satbayev.111">n.albanbay@satbayev.111</a></span></p>
              <div class="links">
                <a href="https://orcid.org/0000-0002-3393-7380">ORCID</a>
              </div>
            </div>
          </body>
        </html>
        """
        page_url = "https://official.satbayev.university/ru/teachers/albanbay-nurtay"

        profile = extract_teacher_profile(html, page_url)

        self.assertEqual(profile["email"], "n.albanbay@satbayev.university")
        self.assertEqual(
            profile["photo_url"],
            "https://official.satbayev.university/file/2025/12/14/2e73a9/_180-180.jpg",
        )
        self.assertEqual(profile["orc_id"], "0000-0002-3393-7380")

    def test_extract_teacher_profile_parses_scopus_scholar_and_publons_links(self):
        html = """
        <html>
          <body>
            <div class="teacher-header">
              <h1>Кальпеева Жулдыз Бейшеналиевна</h1>
              <div class="links">
                <a href="https://www.scopus.com/authid/detail.uri?authorId=56021560300">Scopus</a>
                <a href="https://orcid.org/0000-0002-4970-3095">ORCID</a>
                <a href="https://scholar.google.com/citations?hl=ru&user=PoBZUCYAAAAJ">Scholar</a>
                <a href="https://publons.com/researcher/AAG-7613-2019/">WoS</a>
              </div>
            </div>
          </body>
        </html>
        """
        page_url = "https://official.satbayev.university/ru/teachers/kalpeeva-zhuldyz-beyshenalievna"

        profile = extract_teacher_profile(html, page_url)

        self.assertEqual(profile["scopus_id"], "56021560300")
        self.assertEqual(profile["orc_id"], "0000-0002-4970-3095")
        self.assertIn("scholar.google.com/citations", profile["google_scholar"])
        self.assertEqual(profile["wos_id"], "https://publons.com/researcher/AAG-7613-2019/")

    @patch("account.services.satbayev_scraper.requests.get")
    def test_load_teacher_profile_data_uses_preferred_profile_url(self, mock_get):
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.text = """
        <html>
          <body>
            <div class="teacher-header">
              <h1>Кальпеева Жулдыз Бейшеналиевна</h1>
              <div class="links">
                <a href="https://www.scopus.com/authid/detail.uri?authorId=56021560300">Scopus</a>
              </div>
            </div>
          </body>
        </html>
        """
        mock_get.return_value = mock_response
        page_url = "https://official.satbayev.university/ru/teachers/kalpeeva-zhuldyz-beyshenalievna"

        profile = load_teacher_profile_data(
            full_name="Кальпеева Жулдыз Бейшеналиевна",
            preferred_profile_url=page_url,
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile["satbayev_profile_url"], page_url)
        self.assertEqual(profile["scopus_id"], "56021560300")


class SatbayevPhotoTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="photo-user",
            password="testpass123",
            email="photo@example.com",
        )

    @patch("account.tasks.requests.get")
    def test_update_user_photo_from_satbayev_downloads_and_sets_field(self, mock_get):
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.content = b"\x89PNG\r\n\x1a\n...."
        mock_response.headers = {"Content-Type": "image/png"}
        mock_get.return_value = mock_response

        self.user.photo.save = Mock()
        changed = _update_user_photo_from_satbayev(
            user=self.user,
            photo_url="https://official.satbayev.university/file/2025/12/14/2e73a9/_180-180.png",
            force=False,
        )

        self.assertTrue(changed)
        self.user.photo.save.assert_called_once()
        args, _kwargs = self.user.photo.save.call_args
        self.assertEqual(args[0], f"satbayev_{self.user.id}.png")

    @patch("account.tasks.requests.get")
    def test_update_user_photo_skips_when_existing_file_is_present(self, mock_get):
        self.user.photo.name = "user_photos/existing.jpg"
        with patch.object(self.user.photo.storage, "exists", return_value=True):
            changed = _update_user_photo_from_satbayev(
                user=self.user,
                photo_url="https://official.satbayev.university/file/2025/12/14/2e73a9/_180-180.jpg",
                force=False,
            )

        self.assertFalse(changed)
        mock_get.assert_not_called()

    @patch("account.tasks.requests.get")
    def test_update_user_photo_redownloads_when_file_missing(self, mock_get):
        self.user.photo.name = "user_photos/missing.jpg"

        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.content = b"\x89PNG\r\n\x1a\n...."
        mock_response.headers = {"Content-Type": "image/png"}
        mock_get.return_value = mock_response

        self.user.photo.save = Mock()
        with patch.object(self.user.photo.storage, "exists", return_value=False):
            changed = _update_user_photo_from_satbayev(
                user=self.user,
                photo_url="https://official.satbayev.university/file/2025/12/14/2e73a9/_180-180.png",
                force=False,
            )

        self.assertTrue(changed)
        self.user.photo.save.assert_called_once()
        mock_get.assert_called_once()

    def test_build_update_payload_replaces_placeholder_email_without_force(self):
        self.user.email = "n.albanbay@satbayev.111"
        payload = _build_update_payload(
            user=self.user,
            profile_data={"email": "n.albanbay@satbayev.university"},
            force=False,
        )

        self.assertEqual(payload.get("email"), "n.albanbay@satbayev.university")

    @patch("account.tasks.notify_public")
    @patch("account.tasks.enrich_user_profile_from_satbayev.delay")
    def test_enqueue_satbayev_includes_placeholder_email_without_force(self, mock_delay, _mock_notify):
        user_with_placeholder = User.objects.create_user(
            username="staff-placeholder",
            password="pass123456",
            is_user=False,
            email="n.albanbay@satbayev.111",
            satbayev_profile_url="https://official.satbayev.university/ru/teachers/albanbay-nurtay",
            scopus_id="57222517592",
            orc_id="0000-0002-3393-7380",
            wos_id="wos-id",
            researchgate="https://www.researchgate.net/profile/Nurtay-Albanbay",
            google_scholar="https://scholar.google.com/citations?user=dX_HGR8AAAAJ",
            photo="user_photos/exists.jpg",
        )
        User.objects.create_user(
            username="staff-valid",
            password="pass123456",
            is_user=False,
            email="valid@satbayev.university",
            satbayev_profile_url="https://official.satbayev.university/ru/teachers/valid",
            scopus_id="1",
            orc_id="0000-0000-0000-0000",
            wos_id="wos",
            researchgate="https://www.researchgate.net/profile/Valid",
            google_scholar="https://scholar.google.com/citations?user=VALID",
            photo="user_photos/exists2.jpg",
        )

        enqueue_satbayev_enrichment(limit=50, force=False)

        called_user_ids = [call.kwargs["user_id"] for call in mock_delay.call_args_list]
        self.assertIn(user_with_placeholder.id, called_user_ids)
        self.assertEqual(len(called_user_ids), 1)
