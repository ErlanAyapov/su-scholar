import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from account.services.satbayev_scraper import extract_teacher_profile
from account.tasks import _build_update_payload, _update_user_photo_from_satbayev, enqueue_satbayev_enrichment
from document.models import Document, DocumentGenerator
from main.models import Language, Publication, PublicationType, Venue

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
