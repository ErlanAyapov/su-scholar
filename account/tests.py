from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from account.services.satbayev_scraper import extract_teacher_profile
from account.tasks import _build_update_payload, _update_user_photo_from_satbayev, enqueue_satbayev_enrichment

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
