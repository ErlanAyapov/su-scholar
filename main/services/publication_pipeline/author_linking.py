import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from django.contrib.auth import get_user_model

from main.models import Author

from .utils import normalize_whitespace


User = get_user_model()

AUTO_LINK_THRESHOLD = 0.95
REVIEW_LINK_THRESHOLD = 0.75

SEPARATOR_RE = re.compile(r"[\s,;]+")

CYRILLIC_TO_LATIN = {
    "\u0430": "a",
    "\u04d9": "a",
    "\u0431": "b",
    "\u0432": "v",
    "\u0433": "g",
    "\u0493": "g",
    "\u0434": "d",
    "\u0435": "e",
    "\u0451": "e",
    "\u0436": "zh",
    "\u0437": "z",
    "\u0438": "i",
    "\u0439": "y",
    "\u043a": "k",
    "\u049b": "k",
    "\u043b": "l",
    "\u043c": "m",
    "\u043d": "n",
    "\u04a3": "n",
    "\u043e": "o",
    "\u04e9": "o",
    "\u043f": "p",
    "\u0440": "r",
    "\u0441": "s",
    "\u0442": "t",
    "\u0443": "u",
    "\u04b1": "u",
    "\u04af": "u",
    "\u0444": "f",
    "\u0445": "h",
    "\u04bb": "h",
    "\u0446": "ts",
    "\u0447": "ch",
    "\u0448": "sh",
    "\u0449": "shch",
    "\u044b": "y",
    "\u0456": "i",
    "\u044d": "e",
    "\u044e": "yu",
    "\u044f": "ya",
    "\u044c": "",
    "\u044a": "",
}


@dataclass
class NameProfile:
    raw_name: str
    normalized_name: str
    transliterated_name: str
    initials_pattern: str
    surname: str
    name_tokens: list[str]
    transliterated_tokens: list[str]
    normalized_variants: set[str]
    transliterated_variants: set[str]
    initials_variants: set[str]
    surname_variants: set[str]


def _strip_latin_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _normalize_name_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", normalize_whitespace(value or ""))
    replacements = {
        ".": " ",
        "-": " ",
        "\u2010": " ",
        "\u2011": " ",
        "\u2012": " ",
        "\u2013": " ",
        "\u2014": " ",
        "\u2019": "",
        "'": "",
        "`": "",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)

    cleaned = []
    for char in text.lower():
        if char.isalnum():
            cleaned.append(char)
        else:
            cleaned.append(" ")
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()


def transliterate_name(value: str) -> str:
    normalized = _normalize_name_text(value)
    transliterated = "".join(CYRILLIC_TO_LATIN.get(char, char) for char in normalized)
    transliterated = _strip_latin_accents(transliterated)
    return re.sub(r"\s+", " ", transliterated).strip()


def _tokenize_name(value: str) -> list[str]:
    return [token for token in SEPARATOR_RE.split(_normalize_name_text(value)) if token]


def _tokenize_transliterated(value: str) -> list[str]:
    return [token for token in SEPARATOR_RE.split(transliterate_name(value)) if token]


def _build_sequence_variants(tokens: list[str]) -> set[str]:
    if not tokens:
        return set()
    variants = {" ".join(tokens)}
    if len(tokens) > 1:
        variants.add(" ".join(reversed(tokens)))
    return {item.strip() for item in variants if item.strip()}


def _build_initials_variants(tokens: list[str]) -> set[str]:
    if len(tokens) < 2:
        return set()

    variants = set()
    for surname_index in {0, len(tokens) - 1}:
        surname = tokens[surname_index]
        given_tokens = [token for index, token in enumerate(tokens) if index != surname_index and token]
        if not surname or not given_tokens:
            continue
        initials = " ".join(token[0] for token in given_tokens)
        short_initial = given_tokens[0][0]
        variants.add(f"{short_initial} {surname}")
        variants.add(f"{surname} {short_initial}")
        variants.add(f"{initials} {surname}")
        variants.add(f"{surname} {initials}")
    return {item.strip() for item in variants if item.strip()}


def _select_initials_pattern(tokens: list[str]) -> str:
    if len(tokens) < 2:
        return tokens[0] if tokens else ""
    candidates = []
    for surname_index in {0, len(tokens) - 1}:
        surname = tokens[surname_index]
        given_tokens = [token for index, token in enumerate(tokens) if index != surname_index and token]
        if not surname or not given_tokens:
            continue
        candidates.append((len(surname), f"{given_tokens[0][0]} {surname}".strip()))
    if not candidates:
        return f"{tokens[0][0]} {tokens[-1]}".strip()
    return max(candidates, key=lambda item: item[0])[1]


def build_name_profile(value: str) -> NameProfile:
    normalized_name = _normalize_name_text(value)
    name_tokens = _tokenize_name(value)
    transliterated_name = transliterate_name(value)
    transliterated_tokens = _tokenize_transliterated(value)
    tokens_for_matching = transliterated_tokens or name_tokens
    initials_variants = _build_initials_variants(tokens_for_matching)
    surname_variants = set()
    if name_tokens:
        surname_variants.update({name_tokens[0], name_tokens[-1]})
    if transliterated_tokens:
        surname_variants.update({transliterated_tokens[0], transliterated_tokens[-1]})

    return NameProfile(
        raw_name=value or "",
        normalized_name=normalized_name,
        transliterated_name=transliterated_name,
        initials_pattern=_select_initials_pattern(tokens_for_matching),
        surname=(transliterated_tokens[-1] if transliterated_tokens else (name_tokens[-1] if name_tokens else "")),
        name_tokens=name_tokens,
        transliterated_tokens=transliterated_tokens,
        normalized_variants=_build_sequence_variants(name_tokens),
        transliterated_variants=_build_sequence_variants(transliterated_tokens),
        initials_variants=initials_variants,
        surname_variants={item for item in surname_variants if item},
    )


def build_author_identity_fields(full_name: str) -> dict[str, str]:
    profile = build_name_profile(full_name)
    return {
        "name_normalized": profile.normalized_name[:255],
        "name_translit": profile.transliterated_name[:255],
        "name_initials": profile.initials_pattern[:255],
    }


def enrich_author_payload(author_payload: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(author_payload)
    full_name = (enriched.get("full_name") or enriched.get("raw_name") or "").strip()
    profile = build_name_profile(full_name)
    enriched["full_name"] = full_name
    enriched["name_normalized"] = profile.normalized_name
    enriched["name_translit"] = profile.transliterated_name
    enriched["name_initials"] = profile.initials_pattern
    enriched["surname"] = profile.surname
    enriched["name_tokens"] = profile.transliterated_tokens or profile.name_tokens
    return enriched


def _build_affiliation_tokens(value: str) -> set[str]:
    return {token for token in _tokenize_transliterated(value) if len(token) > 2}


def _get_user_full_name(user: User) -> str:
    parts = [user.last_name or "", user.first_name or "", user.father_name or ""]
    return normalize_whitespace(" ".join(part for part in parts if part))


def _build_user_affiliation_text(user: User) -> str:
    parts = []
    if getattr(user, "department", None):
        parts.append(user.department.name or "")
        institute = getattr(user.department, "institute", None)
        if institute:
            parts.append(institute.name or "")
            university = getattr(institute, "university", None)
            if university:
                parts.append(university.name or "")
    return normalize_whitespace(" ".join(part for part in parts if part))


def _name_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _score_profiles(
    left: NameProfile,
    right: NameProfile,
    *,
    left_affiliation: str = "",
    right_affiliation: str = "",
    orcid_match: bool = False,
) -> tuple[float, str]:
    if orcid_match:
        return 1.0, "orcid_exact"

    if left.normalized_name and left.normalized_name == right.normalized_name:
        return 0.99, "normalized_exact"
    if left.transliterated_name and left.transliterated_name == right.transliterated_name:
        return 0.97, "translit_exact"
    if left.normalized_variants & right.normalized_variants:
        return 0.96, "normalized_variant"
    if left.transliterated_variants & right.transliterated_variants:
        return 0.95, "translit_variant"
    if left.initials_variants & right.initials_variants:
        return 0.88, "initials_surname_exact"

    surname_similarity = max(
        [
            _name_similarity(candidate_left, candidate_right)
            for candidate_left in left.surname_variants
            for candidate_right in right.surname_variants
        ]
        or [0.0]
    )
    left_tokens = set(left.transliterated_tokens or left.name_tokens)
    right_tokens = set(right.transliterated_tokens or right.name_tokens)
    token_overlap = 0.0
    if left_tokens and right_tokens:
        token_overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    best_score = 0.0
    best_method = ""
    if surname_similarity >= 0.92 and token_overlap >= 0.34:
        best_score = 0.86
        best_method = "surname_token_overlap"

    translit_similarity = max(
        (
            _name_similarity(candidate_left, candidate_right)
            for candidate_left in left.transliterated_variants
            for candidate_right in right.transliterated_variants
        ),
        default=0.0,
    )
    if translit_similarity >= 0.93:
        best_score = max(best_score, 0.84)
        best_method = best_method or "translit_similarity"

    initials_similarity = max(
        (
            _name_similarity(candidate_left, candidate_right)
            for candidate_left in left.initials_variants
            for candidate_right in right.initials_variants
        ),
        default=0.0,
    )
    if initials_similarity >= 0.92 and surname_similarity >= 0.92:
        best_score = max(best_score, 0.87)
        best_method = best_method or "initials_surname_similarity"

    if left_affiliation and right_affiliation:
        affiliation_overlap = _build_affiliation_tokens(left_affiliation) & _build_affiliation_tokens(right_affiliation)
        if affiliation_overlap and best_score > 0.0:
            best_score = min(1.0, best_score + 0.05)
            best_method = f"{best_method}+affiliation" if best_method else "affiliation_overlap"

    return best_score, best_method


def match_author_to_existing_author(author_payload: dict[str, Any], authors: list[Author] | None = None) -> dict[str, Any] | None:
    author_payload = enrich_author_payload(author_payload)
    profile = build_name_profile(author_payload.get("full_name", ""))
    author_orcid = (author_payload.get("orcid") or "").strip()
    authors = authors or list(Author.objects.select_related("user").all())

    best_match = None
    best_score = 0.0
    best_method = ""
    for candidate in authors:
        candidate_profile = build_name_profile(candidate.full_name)
        candidate_orcid = (candidate.orcid or "").strip()
        score, method = _score_profiles(
            profile,
            candidate_profile,
            left_affiliation=author_payload.get("affiliations", ""),
            right_affiliation=candidate.affiliations or "",
            orcid_match=bool(author_orcid and candidate_orcid and author_orcid.lower() == candidate_orcid.lower()),
        )
        if score > best_score:
            best_match = candidate
            best_score = score
            best_method = method

    if not best_match or best_score < REVIEW_LINK_THRESHOLD:
        return None

    return {
        "matched_author_id": best_match.id,
        "matched_user_id": best_match.user_id,
        "match_confidence": round(best_score, 3),
        "match_method": best_method,
        "auto_link": best_score >= AUTO_LINK_THRESHOLD,
    }


def match_author_to_user(author_payload: dict[str, Any], users: list[User] | None = None) -> dict[str, Any] | None:
    author_payload = enrich_author_payload(author_payload)
    profile = build_name_profile(author_payload.get("full_name", ""))
    author_orcid = (author_payload.get("orcid") or "").strip()
    users = users or list(User.objects.select_related("department__institute__university").all())

    best_match = None
    best_score = 0.0
    best_method = ""
    for user in users:
        user_full_name = _get_user_full_name(user)
        if not user_full_name:
            continue
        candidate_profile = build_name_profile(user_full_name)
        score, method = _score_profiles(
            profile,
            candidate_profile,
            left_affiliation=author_payload.get("affiliations", ""),
            right_affiliation=_build_user_affiliation_text(user),
            orcid_match=bool(author_orcid and user.orc_id and author_orcid.lower() == user.orc_id.lower()),
        )
        if score > best_score:
            best_match = user
            best_score = score
            best_method = method

    if not best_match or best_score < REVIEW_LINK_THRESHOLD:
        return None

    return {
        "matched_user_id": best_match.id,
        "match_confidence": round(best_score, 3),
        "match_method": best_method,
        "auto_link": best_score >= AUTO_LINK_THRESHOLD,
    }


def build_author_payload_from_instance(author: Author) -> dict[str, Any]:
    return {
        "full_name": author.full_name,
        "raw_name": author.full_name,
        "orcid": author.orcid,
        "affiliations": author.affiliations,
        "is_department_staff": author.is_department_staff,
        "matched_author_id": author.id,
        "matched_user_id": author.user_id,
    }


def link_authors_to_users(
    authors_payload: list[dict[str, Any]],
    *,
    users: list[User] | None = None,
    authors: list[Author] | None = None,
    return_logs: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    linked_authors = []
    logs = []
    users = users or list(User.objects.select_related("department__institute__university").all())
    authors = authors or list(Author.objects.select_related("user").all())

    for author_payload in authors_payload or []:
        enriched = enrich_author_payload(author_payload)
        existing_author_match = match_author_to_existing_author(enriched, authors=authors)
        user_match = match_author_to_user(enriched, users=users)

        final_match = existing_author_match or user_match
        if existing_author_match and user_match and user_match["match_confidence"] > existing_author_match["match_confidence"]:
            final_match = user_match

        if final_match:
            enriched.update(final_match)
            if final_match.get("matched_user_id"):
                enriched["is_department_staff"] = True
            if final_match.get("match_confidence", 0.0) < AUTO_LINK_THRESHOLD:
                enriched["needs_review"] = True

        logs.append(
            {
                "raw_name": enriched.get("raw_name") or enriched.get("full_name"),
                "full_name": enriched.get("full_name"),
                "name_normalized": enriched.get("name_normalized", ""),
                "name_translit": enriched.get("name_translit", ""),
                "name_initials": enriched.get("name_initials", ""),
                "matched_author_id": enriched.get("matched_author_id"),
                "matched_user_id": enriched.get("matched_user_id"),
                "match_confidence": enriched.get("match_confidence"),
                "match_method": enriched.get("match_method", ""),
                "auto_link": bool(final_match and final_match.get("auto_link")),
                "needs_review": bool(enriched.get("needs_review")),
            }
        )
        linked_authors.append(enriched)

    if return_logs:
        return linked_authors, logs
    return linked_authors


def populate_author_identity(author: Author, *, save: bool = True) -> list[str]:
    changed = []
    for field_name, value in build_author_identity_fields(author.full_name).items():
        if getattr(author, field_name) != value:
            setattr(author, field_name, value)
            changed.append(field_name)
    if author.user_id and not author.is_department_staff:
        author.is_department_staff = True
        changed.append("is_department_staff")
    if changed and save:
        author.save(update_fields=changed)
    return changed


def relink_author_instance(author: Author, *, save: bool = True) -> dict[str, Any]:
    payload = build_author_payload_from_instance(author)
    user_match = match_author_to_user(payload)
    changes = populate_author_identity(author, save=False)

    if user_match and user_match.get("matched_user_id") and author.user_id != user_match["matched_user_id"]:
        author.user_id = user_match["matched_user_id"]
        changes.append("user")
    if user_match and author.user_id and not author.is_department_staff:
        author.is_department_staff = True
        changes.append("is_department_staff")

    if save and changes:
        author.save(update_fields=sorted(set(changes)))

    return {
        "author_id": author.id,
        "matched_user_id": user_match.get("matched_user_id") if user_match else None,
        "match_confidence": user_match.get("match_confidence") if user_match else None,
        "match_method": user_match.get("match_method") if user_match else "",
        "auto_link": bool(user_match and user_match.get("auto_link")),
        "updated_fields": sorted(set(changes)),
    }
