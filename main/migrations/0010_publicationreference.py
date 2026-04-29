from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0009_publication_private"),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicationReference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order", models.PositiveIntegerField(db_index=True, default=1)),
                ("raw_text", models.TextField(blank=True)),
                ("note", models.CharField(blank=True, max_length=255)),
                (
                    "publication",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reference_entries", to="main.publication"),
                ),
                (
                    "referenced_publication",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="cited_by_entries",
                        to="main.publication",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "id"],
            },
        ),
    ]
