from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0008_project_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="publication",
            name="private",
            field=models.BooleanField(default=False),
        ),
    ]
