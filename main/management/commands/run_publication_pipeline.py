import json

from django.core.management.base import BaseCommand, CommandError

from main.services.publication_pipeline import run_publication_pipeline


class Command(BaseCommand):
    help = "Run the publication parsing pipeline for a single publication"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, required=True, help="Publication ID")
        parser.add_argument("--force-refresh", action="store_true", dest="force_refresh")

    def handle(self, *args, **options):
        publication_id = options["id"]
        force_refresh = bool(options["force_refresh"])

        try:
            result = run_publication_pipeline(publication_id, force_refresh=force_refresh)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
