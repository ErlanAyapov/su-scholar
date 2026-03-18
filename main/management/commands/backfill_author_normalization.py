from django.core.management.base import BaseCommand

from main.models import Author
from main.services.publication_pipeline.author_linking import populate_author_identity, relink_author_instance


class Command(BaseCommand):
    help = "Backfill Author normalization fields and optionally relink authors to users"

    def add_arguments(self, parser):
        parser.add_argument("--skip-linking", action="store_true", dest="skip_linking")

    def handle(self, *args, **options):
        skip_linking = bool(options["skip_linking"])
        updated = 0
        linked = 0

        queryset = Author.objects.select_related("user").order_by("id")
        for author in queryset.iterator():
            changed = populate_author_identity(author, save=True)
            if changed:
                updated += 1

            if skip_linking:
                continue

            relink_result = relink_author_instance(author, save=True)
            if relink_result.get("matched_user_id"):
                linked += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Author normalization backfill completed: updated={updated}, linked={linked}, skip_linking={skip_linking}"
            )
        )
