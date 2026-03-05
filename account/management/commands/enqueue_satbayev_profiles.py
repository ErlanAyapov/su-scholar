from django.core.management.base import BaseCommand

from account.tasks import enqueue_satbayev_enrichment


class Command(BaseCommand):
    help = "Queue Celery tasks to enrich inactive users from Satbayev teacher pages."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        result = enqueue_satbayev_enrichment.delay(
            limit=options["limit"],
            force=options["force"],
        )
        self.stdout.write(self.style.SUCCESS(f"Task queued: {result.id}"))
