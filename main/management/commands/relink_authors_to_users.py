from django.core.management.base import BaseCommand

from main.models import Author
from main.services.publication_pipeline.author_linking import relink_author_instance


class Command(BaseCommand):
    help = "Re-run author to user matching for existing authors"

    def handle(self, *args, **options):
        linked = 0
        review = 0
        updated = 0

        queryset = Author.objects.select_related("user").order_by("id")
        for author in queryset.iterator():
            result = relink_author_instance(author, save=True)
            if result["updated_fields"]:
                updated += 1
            if result.get("matched_user_id"):
                linked += 1
                if not result.get("auto_link"):
                    review += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Author relinking completed: linked={linked}, review_linked={review}, updated={updated}"
            )
        )
