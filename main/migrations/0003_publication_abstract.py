from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("main", "0002_newsitem_newsmedia"),
    ]

    operations = [
        migrations.AddField(
            model_name="publication",
            name="abstract",
            field=models.TextField(blank=True),
        ),
    ]
