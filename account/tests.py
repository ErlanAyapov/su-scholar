from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

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
