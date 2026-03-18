from django.http import JsonResponse
from django.shortcuts import render
from django.views import View

from main.models import Publication
from main.utils import (
    build_main_page_context,
    build_search_page_context,
    build_publication_detail_context,
    build_search_suggestions_payload,
    handle_report_generation,
)


class HomePageView(View):
    template_name = "main/main.html"

    def get(self, request):
        context = build_search_page_context(request)
        return render(request, self.template_name, context)


class MainPageView(View):
    template_name = "main/main_empty.html"

    def get(self, request):
        context = build_main_page_context(request)
        return render(request, self.template_name, context)


class SearchPageView(View):
    template_name = "main/search_page.html"

    def get(self, request):
        context = build_main_page_context(request)
        return render(request, self.template_name, context)

    def post(self, request):
        if request.POST.get("action") == "generate_report":
            return handle_report_generation(request)
        return self.get(request)


class SearchSuggestionsView(View):
    def get(self, request):
        payload = build_search_suggestions_payload(request)
        return JsonResponse(payload)


class PublicationDetailView(View):
    template_name = "main/publication_detail.html"

    def get(self, request, pk: int):
        context = build_publication_detail_context(pk)
        return render(request, self.template_name, context)


class PublicationPipelineRunView(View):
    http_method_names = ["post"]

    def post(self, request, pk: int):
        if not Publication.objects.filter(id=pk).exists():
            return JsonResponse(
                {
                    "ok": False,
                    "status": "not_found",
                    "publication_id": pk,
                },
                status=404,
            )

        from main.tasks import run_publication_pipeline_single_task

        task = run_publication_pipeline_single_task.delay(
            publication_id=pk,
            force_refresh=True,
        )
        return JsonResponse(
            {
                "ok": True,
                "status": "queued",
                "publication_id": pk,
                "task_id": task.id,
            }
        )


class ProjectsGrantsDemoView(View):
    template_name = "main/projects_grants_demo.html"

    def get(self, request):
        return render(request, self.template_name)


class AnalyticsPageView(View):
    template_name = "main/analytics.html"

    def get(self, request):
        return render(request, self.template_name)
