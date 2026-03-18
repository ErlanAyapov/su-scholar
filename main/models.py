from django.db import models
from django.conf import settings


# ---------- РЎРїСЂР°РІРѕС‡РЅРёРєРё ----------

class PublicationType(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Language(models.Model):
    code = models.CharField(max_length=10, unique=True)   # en, ru, kk
    name = models.CharField(max_length=50)                # English, Russian, Kazakh

    def __str__(self):
        return self.name


class IndexingDatabase(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class Tag(models.Model):
    # IoT, ML, РєСЂРёРїС‚РѕРіСЂР°С„РёСЏ, ...
    name = models.CharField(max_length=80, unique=True)

    def __str__(self):
        return self.name


class DepartmentArea(models.Model):
    # РІРЅСѓС‚СЂРµРЅРЅСЏСЏ РєР»Р°СЃСЃРёС„РёРєР°С†РёСЏ РєР°С„РµРґСЂС‹
    name = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.name


# ---------- РСЃС‚РѕС‡РЅРёРє/РёР·РґР°РЅРёРµ ----------

class Venue(models.Model):
    """
    Р–СѓСЂРЅР°Р»/РєРѕРЅС„РµСЂРµРЅС†РёСЏ/РєРЅРёРіР° (РёСЃС‚РѕС‡РЅРёРє РїСѓР±Р»РёРєР°С†РёРё).
    """
    VENUE_KIND_CHOICES = [
        ("journal", "Journal"),
        ("conference", "Conference"),
        ("book", "Book/Collection"),
        ("other", "Other"),
    ]

    CHARACTER_CHOICES = [
        ("scientific_journal", "Научный журнал"),
        ("conf_proceedings", "Материалы конференции"),
        ("collection", "Сборник"),
        ("monograph", "Монография"),
        ("other", "Другое"),
    ]

    name = models.CharField(max_length=300)
    kind = models.CharField(max_length=20, choices=VENUE_KIND_CHOICES, default="journal")
    character = models.CharField(max_length=30, choices=CHARACTER_CHOICES, default="scientific_journal")
    publisher = models.CharField(max_length=200, blank=True)
    issn = models.CharField(max_length=20, blank=True)
    isbn = models.CharField(max_length=20, blank=True)

    # РґР»СЏ РєРѕРЅС„РµСЂРµРЅС†РёР№
    conference_name = models.CharField(max_length=300, blank=True)
    conference_country = models.CharField(max_length=100, blank=True)
    conference_city = models.CharField(max_length=100, blank=True)

    series = models.CharField(max_length=200, blank=True)   # Proceedings / LNCS etc.

    def __str__(self):
        return self.name


# ---------- РђРІС‚РѕСЂС‹ ----------

class Author(models.Model):
    full_name = models.CharField(max_length=200)            # "Ivanov Ivan Ivanovich"
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="linked_authors",
    )
    orcid = models.CharField(max_length=30, blank=True)
    name_normalized = models.CharField(max_length=255, blank=True, db_index=True)
    name_translit = models.CharField(max_length=255, blank=True, db_index=True)
    name_initials = models.CharField(max_length=255, blank=True, db_index=True)
    affiliations = models.TextField(blank=True)             # РјРѕР¶РЅРѕ РїРѕР·Р¶Рµ РЅРѕСЂРјР°Р»РёР·РѕРІР°С‚СЊ РІ РѕС‚РґРµР»СЊРЅСѓСЋ С‚Р°Р±Р»РёС†Сѓ
    is_department_staff = models.BooleanField(default=False)

    def __str__(self):
        return self.full_name


class Publication(models.Model):
    """
    РћСЃРЅРѕРІРЅР°СЏ СЃСѓС‰РЅРѕСЃС‚СЊ
    """
    STATUS_CHOICES = [
        ("submitted", "Submitted"),
        ("accepted", "Accepted"),
        ("published", "Published"),
    ]

    OA_TYPE_CHOICES = [
        ("", "—"),
        ("gold", "Gold"),
        ("green", "Green"),
        ("hybrid", "Hybrid"),
    ]

    # 1-7, 8
    record_id = models.CharField(max_length=50, unique=True, blank=True)  # РµСЃР»Рё РЅСѓР¶РЅРѕ СЃРІРѕР№ ID Р·Р°РїРёСЃРё
    pub_type = models.ForeignKey(PublicationType, on_delete=models.PROTECT)
    title_original = models.CharField(max_length=500)
    language = models.ForeignKey(Language, on_delete=models.PROTECT)
    year = models.PositiveIntegerField()
    publication_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="published")
    accepted_date = models.DateField(null=True, blank=True)

    # 9-21
    venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="publications")
    volume = models.CharField(max_length=30, blank=True)
    issue = models.CharField(max_length=30, blank=True)
    pages = models.CharField(max_length=50, blank=True)          # "pp. 12-19"
    article_number = models.CharField(max_length=50, blank=True) # РµСЃР»Рё РЅРµС‚ СЃС‚СЂР°РЅРёС†
    total_pages = models.PositiveIntegerField(null=True, blank=True)

    # 22-24
    doi = models.CharField(max_length=120, blank=True)
    url_publisher = models.URLField(blank=True)
    url_open_access = models.URLField(blank=True)
    abstract = models.TextField(blank=True)

    # 25-36 (С‡Р°СЃС‚СЊ РІС‹РЅРµСЃРµРј РІ РѕС‚РґРµР»СЊРЅС‹Рµ С‚Р°Р±Р»РёС†С‹, СЃРј. РЅРёР¶Рµ)
    indexing = models.ManyToManyField(IndexingDatabase, blank=True)
    quartile = models.CharField(max_length=2, blank=True)        # Q1-Q4
    quartile_year = models.PositiveIntegerField(null=True, blank=True)
    citations_count = models.PositiveIntegerField(default=0)
    citations_updated_at = models.DateField(null=True, blank=True)
    open_access = models.BooleanField(default=False)
    oa_type = models.CharField(max_length=10, choices=OA_TYPE_CHOICES, blank=True, default="")

    # 37-45
    authors = models.ManyToManyField(Author, through="PublicationAuthor", related_name="publications")
    keywords = models.TextField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True)
    area = models.ForeignKey(DepartmentArea, on_delete=models.SET_NULL, null=True, blank=True)

    # 46-49 (СЃРІСЏР¶РµРј С‡РµСЂРµР· РїСЂРѕРµРєС‚С‹)
    report_period = models.CharField(max_length=50, blank=True)

    # РєС‚Рѕ СЃРѕР·РґР°Р» Р·Р°РїРёСЃСЊ
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_publications"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def get_collaborators_str(self):
        links = list(self.publicationauthor_set.select_related("author").order_by("order", "id"))
        if len(links) <= 1:
            return ""

        # РЎРѕР°РІС‚РѕСЂС‹ С‚РµРєСѓС‰РµР№ РїСѓР±Р»РёРєР°С†РёРё: РІСЃРµ Р°РІС‚РѕСЂС‹ РїРѕСЃР»Рµ РїРµСЂРІРѕРіРѕ РїРѕ РїРѕСЂСЏРґРєСѓ.
        seen = set()
        collaborators = []
        for link in links[1:]:
            author = link.author
            if not author or not author.full_name:
                continue
            name = author.full_name.strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            collaborators.append(name)

        return ", ".join(collaborators)

    def __str__(self):
        return self.title_original


class PublicationAuthor(models.Model):
    """
    РЎРІСЏР·СЊ РїСѓР±Р»РёРєР°С†РёСЏ-Р°РІС‚РѕСЂ СЃ РїРѕСЂСЏРґРєРѕРј Рё СЂРѕР»СЊСЋ.
    """
    ROLE_CHOICES = [
        ("first", "First author"),
        ("corresponding", "Corresponding author"),
        ("coauthor", "Co-author"),
    ]

    publication = models.ForeignKey(Publication, on_delete=models.CASCADE)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)

    order = models.PositiveIntegerField()  # РїРѕСЂСЏРґРѕРє РєР°Рє РІ СЃС‚Р°С‚СЊРµ
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="coauthor")

    class Meta:
        unique_together = ("publication", "author")
        ordering = ["order"]

    def __str__(self):
        return f"{self.publication_id} - {self.author} ({self.order})"


# ---------- РРґРµРЅС‚РёС„РёРєР°С‚РѕСЂС‹ РІ Р±Р°Р·Р°С… ----------

class PublicationIdentifier(models.Model):
    """
    Scopus EID, WoS Accession Рё С‚.Рї.
    """
    ID_TYPE_CHOICES = [
        ("scopus_eid", "Scopus EID"),
        ("wos_accession", "WoS Accession"),
        ("rinz", "РИНЦ"),
        ("gs", "Google Scholar"),
        ("other", "Other"),
    ]

    publication = models.ForeignKey(Publication, on_delete=models.CASCADE, related_name="identifiers")
    id_type = models.CharField(max_length=30, choices=ID_TYPE_CHOICES)
    value = models.CharField(max_length=200)

    class Meta:
        unique_together = ("publication", "id_type", "value")

    def __str__(self):
        return f"{self.id_type}: {self.value}"


# ---------- РњРµС‚СЂРёРєРё РёСЃС‚РѕС‡РЅРёРєР° (SJR/CiteScore/JIF/SNIP) ----------

class VenueMetric(models.Model):
    METRIC_CHOICES = [
        ("sjr", "SJR"),
        ("citescore", "CiteScore"),
        ("jif", "JIF (Impact Factor)"),
        ("snip", "SNIP"),
    ]

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="metrics")
    metric = models.CharField(max_length=20, choices=METRIC_CHOICES)
    year = models.PositiveIntegerField()
    value = models.DecimalField(max_digits=10, decimal_places=4)

    class Meta:
        unique_together = ("venue", "metric", "year")

    def __str__(self):
        return f"{self.venue} {self.metric} {self.year} = {self.value}"


# ---------- РџСЂРѕРµРєС‚С‹/С„РёРЅР°РЅСЃРёСЂРѕРІР°РЅРёРµ ----------

class Project(models.Model):
    PROJECT_TYPE_CHOICES = [
        ("gf", "ГФ"),
        ("pcf", "ПЦФ"),
        ("contract", "хоздоговор"),
        ("other", "другое"),
    ]

    name = models.CharField(max_length=300)
    project_type = models.CharField(max_length=20, choices=PROJECT_TYPE_CHOICES, default="other")
    contract_number = models.CharField(max_length=100, blank=True)
    funding_source = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return self.name


class PublicationProject(models.Model):
    publication = models.ForeignKey(Publication, on_delete=models.CASCADE)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("publication", "project")


# ---------- Р¤Р°Р№Р»С‹/РїРѕРґС‚РІРµСЂР¶РґРµРЅРёСЏ ----------

class PublicationFile(models.Model):
    FILE_KIND_CHOICES = [
        ("pdf", "PDF"),
        ("acceptance", "Acceptance letter / proof"),
        ("other", "Other"),
    ]

    publication = models.ForeignKey(Publication, on_delete=models.CASCADE, related_name="files")
    kind = models.CharField(max_length=20, choices=FILE_KIND_CHOICES, default="pdf")
    source_url = models.URLField(blank=True, db_index=True, max_length=500)
    file = models.FileField(upload_to="publication_files/")
    description = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"{self.publication_id} {self.kind}"


class RepositoryLink(models.Model):
    # Zenodo / arXiv / СѓРЅРёРІРµСЂСЃРёС‚РµС‚СЃРєРёР№ СЂРµРїРѕР·РёС‚РѕСЂРёР№ Рё С‚.Рї.
    publication = models.ForeignKey(Publication, on_delete=models.CASCADE, related_name="repo_links")
    url = models.URLField()
    label = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.url


# News and announcements related to publications, projects, etc.
class NewsItem(models.Model):
    title = models.CharField(max_length=300)
    content = models.TextField()
    publication_date = models.DateField()
    related_publications = models.ManyToManyField(Publication, blank=True)
    related_projects = models.ManyToManyField(Project, blank=True)

    def __str__(self):
        return self.title
    
class NewsMedia(models.Model):
    MEDIA_TYPE_CHOICES = [
        ("image", "Image"),
        ("video", "Video"),
        ("other", "Other"),
    ]

    news_item = models.ForeignKey(NewsItem, on_delete=models.CASCADE, related_name="media")
    file = models.FileField(upload_to="news_media/")
    description = models.CharField(max_length=200, blank=True)
    media_type = models.CharField(max_length=50, choices=MEDIA_TYPE_CHOICES, default="image")  # image, video, etc.

    def __str__(self):
        return f"{self.news_item_id} media"



