import re

from django.contrib import admin, messages

from main.models import (
    Author,
    DepartmentArea,
    IndexingDatabase,
    Language,
    Project,
    Publication,
    PublicationAuthor,
    PublicationFile,
    PublicationIdentifier,
    PublicationProject,
    PublicationType,
    RepositoryLink,
    Tag,
    Venue,
    VenueMetric,
    NewsItem,
    NewsMedia,
)

try:
    from lingua import Language as LinguaLanguage
    from lingua import LanguageDetectorBuilder
except ImportError:  # pragma: no cover
    LinguaLanguage = None
    LanguageDetectorBuilder = None

LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
KAZAKH_SPECIFIC_RE = re.compile(r"[\u04d8\u04d9\u0492\u0493\u049a\u049b\u04a2\u04a3\u04e8\u04e9\u04b0\u04b1\u04ae\u04af\u0406\u0456\u04ba\u04bb]")

LINGUA_LANG_CODE_MAP = {
    "ENGLISH": "en",
    "RUSSIAN": "ru",
    "KAZAKH": "kk",
}


def _build_lingua_detector():
    if not LanguageDetectorBuilder or not LinguaLanguage:
        return None
    try:
        return LanguageDetectorBuilder.from_languages(
            LinguaLanguage.ENGLISH,
            LinguaLanguage.RUSSIAN,
            LinguaLanguage.KAZAKH,
        ).build()
    except Exception:  # noqa: BLE001
        return None


_LINGUA_DETECTOR = _build_lingua_detector()


def _detect_by_lingua(text: str) -> str:
    if not _LINGUA_DETECTOR or not text:
        return ""
    try:
        detected = _LINGUA_DETECTOR.detect_language_of(text)
    except Exception:  # noqa: BLE001
        return ""
    if detected is None:
        return ""
    return LINGUA_LANG_CODE_MAP.get(getattr(detected, "name", ""), "")


def _detect_by_script(text: str) -> str:
    latin_count = len(LATIN_RE.findall(text))
    cyrillic_count = len(CYRILLIC_RE.findall(text))
    if latin_count == 0 and cyrillic_count == 0:
        return ""

    if latin_count > cyrillic_count:
        return "en"
    if cyrillic_count > latin_count:
        if KAZAKH_SPECIFIC_RE.search(text):
            return "kk"
        return "ru"

    if KAZAKH_SPECIFIC_RE.search(text):
        return "kk"
    return "en" if latin_count else "ru"


def _detect_title_language_code(title: str) -> str:
    text = (title or "").strip()
    if not text:
        return ""

    lingua_code = _detect_by_lingua(text)
    if lingua_code:
        return lingua_code

    return _detect_by_script(text)


@admin.register(Publication)
class PublicationAdmin(admin.ModelAdmin):
    list_display = ("id", "title_original", "language", "year", "pub_type", "created_by")
    list_filter = ("language", "year", "pub_type", "status", "open_access")
    search_fields = ("title_original", "doi", "record_id", "authors__full_name")
    ordering = ("-year", "-id")
    actions = ("action_autodetect_language_by_title",)

    @admin.action(description="Auto-detect language from title for Unknown language rows")
    def action_autodetect_language_by_title(self, request, queryset):
        language_map = {
            "en": Language.objects.get_or_create(code="en", defaults={"name": "English"})[0],
            "ru": Language.objects.get_or_create(code="ru", defaults={"name": "Russian"})[0],
            "kk": Language.objects.get_or_create(code="kk", defaults={"name": "Kazakh"})[0],
        }

        updated = 0
        skipped = 0
        untouched = 0
        to_update = []
        for publication in queryset.select_related("language"):
            current_code = (publication.language.code if publication.language_id else "").strip().lower()
            if current_code and current_code not in {"und", "unknown"}:
                untouched += 1
                continue

            detected_code = _detect_title_language_code(publication.title_original or "")
            target_language = language_map.get(detected_code)
            if not target_language:
                skipped += 1
                continue

            if publication.language_id == target_language.id:
                skipped += 1
                continue

            publication.language = target_language
            to_update.append(publication)
            updated += 1

        if to_update:
            Publication.objects.bulk_update(to_update, ["language"])

        detector_name = "lingua" if _LINGUA_DETECTOR else "script-fallback"
        self.message_user(
            request,
            (
                f"Language autodetect finished ({detector_name}): "
                f"updated={updated}, skipped={skipped}, untouched={untouched}."
            ),
            level=messages.SUCCESS,
        )


@admin.register(PublicationType)
class PublicationTypeAdmin(admin.ModelAdmin):
    search_fields = ("name",)
    list_display = ("id", "name")


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name")
    search_fields = ("code", "name")


@admin.register(IndexingDatabase)
class IndexingDatabaseAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)


@admin.register(DepartmentArea)
class DepartmentAreaAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "kind", "publisher")
    list_filter = ("kind", "character")
    search_fields = ("name", "publisher", "issn", "isbn")


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    list_display = ("id", "full_name", "orcid", "is_department_staff")
    list_filter = ("is_department_staff",)
    search_fields = ("full_name", "orcid")


@admin.register(PublicationAuthor)
class PublicationAuthorAdmin(admin.ModelAdmin):
    list_display = ("id", "publication", "author", "order", "role")
    list_filter = ("role",)
    search_fields = ("publication__title_original", "author__full_name")


@admin.register(PublicationIdentifier)
class PublicationIdentifierAdmin(admin.ModelAdmin):
    list_display = ("id", "publication", "id_type", "value")
    list_filter = ("id_type",)
    search_fields = ("value", "publication__title_original")


@admin.register(VenueMetric)
class VenueMetricAdmin(admin.ModelAdmin):
    list_display = ("id", "venue", "metric", "year", "value")
    list_filter = ("metric", "year")
    search_fields = ("venue__name",)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "project_type", "contract_number")
    list_filter = ("project_type",)
    search_fields = ("name", "contract_number")


@admin.register(PublicationProject)
class PublicationProjectAdmin(admin.ModelAdmin):
    list_display = ("id", "publication", "project")
    search_fields = ("publication__title_original", "project__name")


@admin.register(PublicationFile)
class PublicationFileAdmin(admin.ModelAdmin):
    list_display = ("id", "publication", "kind", "description")
    list_filter = ("kind",)
    search_fields = ("publication__title_original", "description")


@admin.register(RepositoryLink)
class RepositoryLinkAdmin(admin.ModelAdmin):
    list_display = ("id", "publication", "url", "label")
    search_fields = ("publication__title_original", "url", "label")

admin.site.register(NewsItem)
admin.site.register(NewsMedia)
