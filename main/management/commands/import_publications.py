from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from main.services.publication_importer import import_publications_for_user
from main.tasks import import_publications_for_all_users_task, import_user_publications_task

User = get_user_model()


class Command(BaseCommand):
    help = "Import publications from public ORCID/OpenAlex sources."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--sync", action="store_true")
        parser.add_argument("--user-id", type=int, default=None)

    def handle(self, *args, **options):
        if options["user_id"] is not None:
            if options["sync"]:
                user = User.objects.get(pk=options["user_id"])
                result = import_publications_for_user(user=user, force=options["force"])
                self.stdout.write(self.style.SUCCESS(str(result)))
                return

            task = import_user_publications_task.delay(
                user_id=options["user_id"],
                force=options["force"],
            )
            self.stdout.write(self.style.SUCCESS(f"Queued task: {task.id}"))
            return

        if options["sync"]:
            users = User.objects.filter(is_user=False).order_by("id")[: options["limit"]]
            aggregate = {"users": 0, "works_total": 0, "created": 0, "updated": 0, "skipped": 0}
            for user in users:
                result = import_publications_for_user(user=user, force=options["force"])
                aggregate["users"] += 1
                aggregate["works_total"] += result["works_total"]
                aggregate["created"] += result["created"]
                aggregate["updated"] += result["updated"]
                aggregate["skipped"] += result["skipped"]
                self.stdout.write(f"user={user.id} -> {result}")

            self.stdout.write(self.style.SUCCESS(f"Done: {aggregate}"))
            return

        task = import_publications_for_all_users_task.delay(
            limit=options["limit"],
            force=options["force"],
        )
        self.stdout.write(self.style.SUCCESS(f"Queued task: {task.id}"))
