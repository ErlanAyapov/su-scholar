from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from document.models import DocumentGenerator
from document.templatetags.base_tags import get_document_generators

User = get_user_model()


class DocumentGeneratorTemplateTagTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="testpass123")
        self.other_user = User.objects.create_user(username="other", password="testpass123")

        self.public_global = self._create_generator(
            title="public-global",
            owner=self.owner,
            access_to_all=True,
            page=None,
        )
        self.public_main = self._create_generator(
            title="public-main",
            owner=self.owner,
            access_to_all=True,
            page="main_search",
        )
        self.private_owner_main = self._create_generator(
            title="private-owner-main",
            owner=self.owner,
            access_to_all=False,
            page="main_search",
        )
        self.private_other_main = self._create_generator(
            title="private-other-main",
            owner=self.other_user,
            access_to_all=False,
            page="main_search",
        )
        self.private_owner_documents = self._create_generator(
            title="private-owner-documents",
            owner=self.owner,
            access_to_all=False,
            page="document_main",
        )

    def _create_generator(self, *, title, owner, access_to_all, page):
        return DocumentGenerator.objects.create(
            title=title,
            content="template",
            file="synthetic_documents/template.docx",
            file_type="docx",
            user=owner,
            access_to_all=access_to_all,
            page=page,
        )

    def test_tag_returns_public_and_owned_generators_for_requested_page(self):
        generators = get_document_generators(self.owner, "main_search")
        ids = set(generators.values_list("id", flat=True))

        self.assertIn(self.public_global.id, ids)
        self.assertIn(self.public_main.id, ids)
        self.assertIn(self.private_owner_main.id, ids)
        self.assertNotIn(self.private_other_main.id, ids)
        self.assertNotIn(self.private_owner_documents.id, ids)

    def test_tag_returns_only_public_generators_for_anonymous_user(self):
        generators = get_document_generators(AnonymousUser(), "main_search")
        ids = set(generators.values_list("id", flat=True))

        self.assertIn(self.public_global.id, ids)
        self.assertIn(self.public_main.id, ids)
        self.assertNotIn(self.private_owner_main.id, ids)
        self.assertNotIn(self.private_other_main.id, ids)

    def test_tag_returns_all_accessible_generators_when_page_empty(self):
        generators = get_document_generators(self.owner, "")
        ids = set(generators.values_list("id", flat=True))

        self.assertIn(self.public_global.id, ids)
        self.assertIn(self.public_main.id, ids)
        self.assertIn(self.private_owner_main.id, ids)
        self.assertIn(self.private_owner_documents.id, ids)
        self.assertNotIn(self.private_other_main.id, ids)
