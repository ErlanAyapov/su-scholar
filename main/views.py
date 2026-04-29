from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View
from django.utils.text import get_valid_filename
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.utils.decorators import method_decorator

from main.models import Publication, PublicationFile
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


@method_decorator(xframe_options_sameorigin, name="dispatch")
class PublicationPdfPreviewView(View):
    http_method_names = ["get"]

    def get(self, request, file_id: int):
        publication_file = get_object_or_404(PublicationFile, pk=file_id)
        file_field = getattr(publication_file, "file", None)
        file_name = str(getattr(file_field, "name", "") or "")
        if not file_field:
            raise Http404("PDF file not found.")

        is_pdf_by_kind = str(getattr(publication_file, "kind", "") or "").lower() == "pdf"
        is_pdf_by_name = file_name.lower().endswith(".pdf")
        if not (is_pdf_by_kind or is_pdf_by_name):
            raise Http404("The requested file is not a PDF.")

        try:
            file_field.open("rb")
        except FileNotFoundError as exc:
            raise Http404("PDF file not found.") from exc

        safe_name = get_valid_filename(file_name.split("/")[-1] or f"publication-{publication_file.id}.pdf")
        response = FileResponse(file_field, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{safe_name}"'
        return response


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
