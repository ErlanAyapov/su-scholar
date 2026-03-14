REPORT_NAV_URLS = {
    "document_main",
    "document_detail",
    "document_edit",
    "document_file",
    "onlyoffice_config",
    "onlyoffice_callback",
    "generator_create",
    "generator_detail",
    "generator_edit",
    "generator_file",
    "generator_onlyoffice_config",
    "generator_onlyoffice_callback",
    "synonyms_export",
    "synonyms_import",
    "synonym_create",
    "synonym_detail",
}

PUBLICATIONS_NAV_URLS = {
    "main_search",
    "publication_detail",
}

PROJECTS_NAV_URLS = {
    "projects_grants_demo",
}

ACCOUNT_NAV_URLS = {
    "account_page",
    "account_login",
    "account_register",
    "account_activate",
    "account_set_password",
    "account_logout",
    "employee_profile",
    "employee_profile_sync",
    "employees",
    "employees_load",
    "login",
    "register",
}


def layout_navigation(request):
    resolver_match = getattr(request, "resolver_match", None)
    url_name = getattr(resolver_match, "url_name", "") or ""
    search_tab = (request.GET.get("tab") or "").strip().lower()
    researchers_search_active = url_name == "advanced_search" and search_tab == "researchers"
    publications_search_active = url_name == "advanced_search" and search_tab != "researchers"

    active = {
        "home": url_name == "main",
        "researchers": researchers_search_active,
        "projects": url_name in PROJECTS_NAV_URLS,
        "publications": url_name in PUBLICATIONS_NAV_URLS or publications_search_active,
        "analytics": url_name in REPORT_NAV_URLS,
        "account": url_name in ACCOUNT_NAV_URLS,
    }

    return {
        "layout_nav": {
            "url_name": url_name,
            "search_tab": search_tab,
            "active": {
                **active,
                # Backward-compatible aliases for older templates.
                "reports": active["analytics"],
                "documents": active["publications"],
            },
        }
    }
