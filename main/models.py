from django.db import models
from django.conf import settings


# ---------- Справочники ----------

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
    # IoT, ML, криптография, ...
    name = models.CharField(max_length=80, unique=True)

    def __str__(self):
        return self.name


class DepartmentArea(models.Model):
    # внутренняя классификация кафедры
    name = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.name


# ---------- Источник/издание ----------

class Venue(models.Model):
    """
    Журнал/конференция/книга (источник публикации).
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

    # для конференций
    conference_name = models.CharField(max_length=300, blank=True)
    conference_country = models.CharField(max_length=100, blank=True)
    conference_city = models.CharField(max_length=100, blank=True)

    series = models.CharField(max_length=200, blank=True)   # Proceedings / LNCS etc.

    def __str__(self):
        return self.name


# ---------- Авторы ----------

class Author(models.Model):
    full_name = models.CharField(max_length=200)            # "Ivanov Ivan Ivanovich"
    orcid = models.CharField(max_length=30, blank=True)
    affiliations = models.TextField(blank=True)             # можно позже нормализовать в отдельную таблицу
    is_department_staff = models.BooleanField(default=False)

    def __str__(self):
        return self.full_name


class Publication(models.Model):
    """
    Основная сущность
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
    record_id = models.CharField(max_length=50, unique=True, blank=True)  # если нужно свой ID записи
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
    article_number = models.CharField(max_length=50, blank=True) # если нет страниц
    total_pages = models.PositiveIntegerField(null=True, blank=True)

    # 22-24
    doi = models.CharField(max_length=120, blank=True)
    url_publisher = models.URLField(blank=True)
    url_open_access = models.URLField(blank=True)

    # 25-36 (часть вынесем в отдельные таблицы, см. ниже)
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

    # 46-49 (свяжем через проекты)
    report_period = models.CharField(max_length=50, blank=True)

    # кто создал запись
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
        # Получаем всех авторов публикации
        authors = self.authors.all()

        # Получаем всех соавторов для каждого автора
        collaborators = set()
        for author in authors:
            coauthors = Author.objects.filter(publications__authors=author).exclude(id=author.id)
            collaborators.update(coauthors)

        full_authors = ", ".join([author.full_name for author in authors])
        full_collaborators = ", ".join([collab.full_name for collab in collaborators])

        return full_authors + (", " + full_collaborators if full_collaborators else "")

    def __str__(self):
        return self.title_original


class PublicationAuthor(models.Model):
    """
    Связь публикация-автор с порядком и ролью.
    """
    ROLE_CHOICES = [
        ("first", "First author"),
        ("corresponding", "Corresponding author"),
        ("coauthor", "Co-author"),
    ]

    publication = models.ForeignKey(Publication, on_delete=models.CASCADE)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)

    order = models.PositiveIntegerField()  # порядок как в статье
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="coauthor")

    class Meta:
        unique_together = ("publication", "author")
        ordering = ["order"]

    def __str__(self):
        return f"{self.publication_id} - {self.author} ({self.order})"


# ---------- Идентификаторы в базах ----------

class PublicationIdentifier(models.Model):
    """
    Scopus EID, WoS Accession и т.п.
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


# ---------- Метрики источника (SJR/CiteScore/JIF/SNIP) ----------

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


# ---------- Проекты/финансирование ----------

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


# ---------- Файлы/подтверждения ----------

class PublicationFile(models.Model):
    FILE_KIND_CHOICES = [
        ("pdf", "PDF"),
        ("acceptance", "Acceptance letter / proof"),
        ("other", "Other"),
    ]

    publication = models.ForeignKey(Publication, on_delete=models.CASCADE, related_name="files")
    kind = models.CharField(max_length=20, choices=FILE_KIND_CHOICES, default="pdf")
    file = models.FileField(upload_to="publication_files/")
    description = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"{self.publication_id} {self.kind}"


class RepositoryLink(models.Model):
    # Zenodo / arXiv / университетский репозиторий и т.п.
    publication = models.ForeignKey(Publication, on_delete=models.CASCADE, related_name="repo_links")
    url = models.URLField()
    label = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.url