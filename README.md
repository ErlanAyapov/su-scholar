# SU Scholar

Платформа для учёта, анализа и обогащения публикационной активности. Проект хранит публикации, авторов, площадки публикации, идентификаторы, метрики и связи с внутренними пользователями университета.

## Publication Pipeline

В проекте реализован multi-source pipeline для разбора научных публикаций. Он не полагается на один источник и проходит полный цикл:

1. берёт исходную публикацию из локальной БД;
2. стартует с `url_publisher`, DOI URL, OA URL или репозиторного URL;
3. безопасно получает HTML/metadata;
4. извлекает intermediate payload: cleaned text, meta tags, JSON-LD, ссылки, заголовки;
5. отправляет structured input в LLM extractor с `temperature=0` и ожидает только JSON;
6. находит дополнительные допустимые источники;
7. повторяет fetch + extract для каждого полезного источника;
8. сливает payload-ы по приоритету источников;
9. нормализует данные;
10. обогащает DOI, quartile и indexing при наличии надёжного сигнала;
11. нормализует и матчится авторов с существующими `Author` и локальными `User`;
12. обновляет Django-модели и сохраняет debug artifacts.

## Что обновляет pipeline

Pipeline работает минимум со следующими сущностями:

- `Publication`
- `Venue`
- `Author`
- `PublicationAuthor`
- `PublicationIdentifier`
- `RepositoryLink`
- `IndexingDatabase`
- `VenueMetric`
- `Tag`
- `Project`

## Поддерживаемые источники

Поддерживаются следующие типы источников:

- Google Scholar
- publisher pages
- DOI landing pages
- Crossref pages / API
- OpenAlex pages / API
- Scopus-like pages
- Web of Science-like pages
- repository pages (`arXiv`, `Zenodo`, institutional repositories)
- raw HTML
- извлечённый PDF text

Источник определяется через `detect_source_type()` по URL, домену, HTML-маркерам, meta tags и характерным паттернам ссылок.

## Safe Fetching

Сетевой слой ограничен и не использует произвольные URL из HTML без проверки.

Основные ограничения:

- `requests.Session()`
- timeout-ы и redirect handling
- allowlist доменов
- запрет private IP, localhost и metadata endpoints
- дедупликация URL
- лимит числа источников на публикацию
- лимит размера HTML и cleaned text
- отказоустойчивость: ошибка одного источника не валит весь pipeline

## Приоритет источников при merge

При конфликте данных используются более надёжные источники:

1. DOI / publisher metadata / Crossref / OpenAlex
2. Scopus / Web of Science
3. publisher page visible content
4. Google Scholar
5. repository pages
6. raw HTML snippets

Правила merge:

- непустые сильные поля не затираются пустыми;
- DOI из структурированных источников приоритетнее snippet-источников;
- abstract с publisher page приоритетнее scholar snippet;
- авторы объединяются с сохранением порядка;
- идентификаторы объединяются по `(id_type, value)`;
- repository links и related URLs дедуплицируются.

## Quartile Extraction

Pipeline умеет извлекать и нормализовать:

- `publication.quartile`
- `publication.quartile_year`
- `venue_metrics` для `SJR`, `CiteScore`, `JIF`, `SNIP`

### Откуда берётся квартиль

Quartile ищется в:

- publisher page
- Scopus-like / WoS-like страницах
- structured metadata
- текстовых фрагментах intermediate payload
- enrichment-источниках, если они дают надёжный DOI/metadata match

### Нормализация квартиля

Нормализуются варианты:

- `Q1`, `Q-1`, `Q 1`, `q1`
- `Quartile 1`
- аналогично для `Q2`, `Q3`, `Q4`

### Куда сохраняется

- `Publication.quartile`
- `Publication.quartile_year`
- `VenueMetric`, если удалось выделить `sjr`, `citescore`, `jif` или `snip`

Если квартиль найден из нескольких источников и данные конфликтуют, pipeline:

- выбирает наиболее надёжный источник;
- пишет warning в metrics/logs;
- проставляет `publication.needs_review = true` в final payload, если конфликт не удалось разрешить уверенно.

Источник метрики сохраняется в payload/debug artifacts. В текущей модели `VenueMetric` отдельного поля `source` нет, поэтому в БД записывается нормализованное значение метрики без отдельного source-column.

## Author Matching И Author -> User Linking

### Что добавлено в модель `Author`

Для устойчивого матчинга добавлены поля:

- `user -> settings.AUTH_USER_MODEL`
- `name_normalized`
- `name_translit`
- `name_initials`

Миграция: `main/migrations/0004_author_name_initials_author_name_normalized_and_more.py`

### Что делает matching

Pipeline пытается понять, является ли внешний автор внутренним сотрудником / пользователем системы. Для этого используется:

- полное имя
- нормализованное имя
- транслитерация кириллица -> латиница
- сокращённая форма с инициалами (`N Albanbay`)
- ORCID
- affiliation overlap
- уже существующие `Author`
- локальные `User`

### Поддерживаемые случаи

Pipeline учитывает, что это может быть один и тот же человек:

- `Нуртай Албанбай`
- `Nurtay Albanbay`
- `N Albanbay`
- `Албанбай Нуртай`
- `Albanbay Nurtay`

### Нормализация имён

Для каждого автора строятся:

- `raw_name`
- `name_normalized`
- `name_translit`
- `name_initials`
- `surname`
- token sets / sequence variants

Учтены кириллические и казахские буквы:

- `Ә, Ғ, Қ, Ң, Ө, Ұ, Ү, Һ, І`

### Scoring

Решение о link принимается по confidence-score:

- `>= 0.95` -> auto-link
- `0.75 - 0.95` -> link + `needs_review`
- `< 0.75` -> не линковать автоматически

На score влияют:

- ORCID exact match
- normalized exact match
- transliteration exact/variant match
- initials + surname match
- token overlap
- affiliation overlap

### Что происходит при совпадении

- `Author.user` связывается с локальным `User`
- `Author.is_department_staff = True`
- существующий `Author` переиспользуется вместо создания дубля
- если `User` найден, а `Author` нет, создаётся новый `Author` и сразу связывается с `user`

Это даёт возможность строить:

- публикации конкретного сотрудника
- совместные публикации внутренних сотрудников
- общих соавторов

Фильтрация публикаций сотрудника учитывает теперь не только текстовое совпадение имени, но и прямую связь `authors__user`.

## Структура модулей

Основная реализация находится в `main/services/publication_pipeline/`:

- `pipeline.py` — orchestration, discovery, metrics, debug artifacts
- `fetchers.py` — safe fetcher и URL validation
- `source_detector.py` — определение типа источника
- `extractors.py` — intermediate extraction из HTML/text
- `llm_client.py` — вызов LLM extractor и strict JSON parsing
- `merger.py` — merge payload-ов по приоритетам источников
- `normalizers.py` — нормализация публикации, авторов, ссылок, идентификаторов, метрик
- `validators.py` — валидация final payload
- `db_updater.py` — safe/force update Django-моделей
- `author_linking.py` — нормализация имён, транслитерация, scoring-based author/user matching
- `utils.py` — shared helpers, schema, quartile helpers, URL helpers

Совместимость со старым входом сохранена через `main/services/llm_parser.py`.

## Management Commands

### Применить миграции

```bash
python manage.py migrate
```

### Запустить pipeline для одной публикации

```bash
python manage.py run_publication_pipeline --id 366
```

### Принудительный refresh

```bash
python manage.py run_publication_pipeline --id 366 --force-refresh
```

### Бэкфилл нормализованных имён авторов

```bash
python manage.py backfill_author_normalization
```

### Бэкфилл без повторного линкования с пользователями

```bash
python manage.py backfill_author_normalization --skip-linking
```

### Перелинковка существующих авторов с пользователями

```bash
python manage.py relink_authors_to_users
```

## Docker

Для локального запуска в Docker добавлены:

- `Dockerfile` для Django ASGI приложения;
- `docker-compose.yml` со стеком `web + celery-worker + celery-beat + postgres + redis + onlyoffice`;
- `docker/entrypoint.sh` для ожидания зависимостей, миграций и `collectstatic`;
- `.env.docker.example` с docker-ориентированными переменными.

Быстрый старт:

```bash
docker compose up --build -d
```

Если Postgres находится на отдельном сервере:

- в `.env` укажите `DB_HOST`, `DB_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`;
- обычный `docker compose up --build -d` поднимет только приложение, `redis` и `onlyoffice`;
- сервис `db` теперь опциональный и запускается только через профиль.

Если нужен локальный Postgres внутри Docker:

```bash
docker compose --profile local-db up --build -d
```

По умолчанию сервисы публикуются так:

- Django: `http://localhost:8000`
- ONLYOFFICE Docs: `http://localhost:8080`

Важно для ONLYOFFICE:

- `DOCK_EDITOR_URL` должен быть доступен браузеру;
- `APP_PUBLIC_URL` должен быть доступен и браузеру, и контейнеру `onlyoffice`;
- для локального Docker Desktop по умолчанию используется `http://host.docker.internal:8000`;
- если запускаете на сервере или Linux-хосте, замените `APP_PUBLIC_URL` на реальный IP или домен приложения.

## Debug Artifacts

Каждый запуск pipeline сохраняет артефакты в директорию:

```text
BASE_DIR / llm_debug / publication_<id>/
```

Обычно туда попадают:

- `01_source_primary.html`
- `02_source_primary_cleaned.txt`
- `03_source_primary_links.json`
- `04_llm_primary.json`
- `05_additional_sources.json`
- `06_merged.json`
- `07_normalized.json`
- `08_author_matching.json`
- `08_final_validated.json`
- `09_metrics.json`

Содержимое может немного отличаться в зависимости от того, на каком шаге произошла ошибка.

## Метрики Pipeline

Pipeline пишет технические метрики выполнения:

- fetch time per source
- total sources fetched
- total HTML chars
- cleaned text chars
- estimated prompt tokens
- response tokens
- LLM time per source
- merge time
- normalization time
- DB update time
- total pipeline time
- warnings
- author matching summary

## Практические замечания

- Если из Google Scholar найден `url_publisher`, pipeline обязан делать второй проход по publisher page.
- Scholar часто содержит только snippet/preview, поэтому `needs_review` может быть выставлен даже при успешном разборе.
- DOI enrichment выполняется только при достаточно надёжном title/year/author match.
- Matching имён вероятностный: pipeline старается не плодить дубли, но и не склеивать агрессивно при низком confidence.
- `VenueMetric` в текущей схеме не хранит отдельный `source`; эта информация остаётся в payload/debug artifacts.
- Если в локальной БД уже есть хорошие значения, `safe_update` не должен заменять их более слабыми данными.

## Ограничения

- Google Scholar может не содержать DOI, полный abstract или publisher metadata.
- Publisher page может быть кривой, JS-heavy или частично недоступной.
- Quartile доступен не для каждой публикации.
- ORCID/affiliation есть не у всех авторов.
- Транслитерация и initials matching решают много кейсов, но не дают математической гарантии идентичности автора.
- При низком confidence pipeline оставляет warning и требует ручной review вместо агрессивного auto-link.

## Базовый сценарий эксплуатации

1. Выполнить миграции.
2. Прогнать `backfill_author_normalization` для старых авторов.
3. При необходимости прогнать `relink_authors_to_users`.
4. Запускать `run_publication_pipeline --id <publication_id>` для новых или проблемных публикаций.
5. Проверять `llm_debug/publication_<id>/` при конфликтах quartile, слабом abstract, неоднозначном matching или частичных fetch-ошибках.
