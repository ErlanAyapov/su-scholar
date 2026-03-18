import json

from django.conf import settings

from main.models import Publication
from main.services.publication_pipeline import PublicationPipeline, run_publication_pipeline


class LlmParser:
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.model = model or getattr(settings, "LLM_MODEL", "gpt-oss:20b")
        self.base_url = base_url or getattr(settings, "LLM_API", "")
        self.api_key = api_key or getattr(settings, "LLM_API_KEY", "") or "ollama"

    def run(self, publication: Publication, force_refresh: bool = False) -> dict:
        pipeline = PublicationPipeline(
            force_refresh=force_refresh,
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
        )
        return pipeline.run(publication)


def main():
    publication = Publication.objects.get(id=366)
    result = run_publication_pipeline(publication.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
