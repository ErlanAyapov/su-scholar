(function () {
    const form = document.getElementById("profileExportForm");
    if (!form) {
        return;
    }

    const templateKeyInput = document.getElementById("profileExportTemplateKey");
    const reportTitleInput = document.getElementById("profileExportReportTitle");
    if (!templateKeyInput || !reportTitleInput) {
        return;
    }

    const FILTER_KEYS = [
        "search",
        "type",
        "lang",
        "status",
        "oa",
        "y_min",
        "y_max",
        "db",
        "quartile",
        "area",
        "venue",
        "tags",
    ];

    function clearGeneratedFilters() {
        form.querySelectorAll("input[data-export-filter='1']").forEach((input) => input.remove());
    }

    function appendFilterField(name, value) {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = name;
        input.value = value;
        input.dataset.exportFilter = "1";
        form.appendChild(input);
    }

    function appendFiltersFromUrl() {
        clearGeneratedFilters();
        const params = new URLSearchParams(window.location.search);

        FILTER_KEYS.forEach((key) => {
            const values = params.getAll(key).filter((value) => value !== "");
            values.forEach((value) => appendFilterField(key, value));
        });
    }

    function buildTitle(templateTitle) {
        const cleanedTitle = (templateTitle || "").trim();
        const datePart = new Date().toISOString().slice(0, 10);
        if (!cleanedTitle) {
            return "";
        }
        return cleanedTitle + " - " + datePart;
    }

    function submitProfileExportReport(templateKey, templateTitle) {
        const cleanedKey = (templateKey || "").trim();
        if (!cleanedKey) {
            return;
        }
        templateKeyInput.value = cleanedKey;
        reportTitleInput.value = buildTitle(templateTitle);
        appendFiltersFromUrl();
        form.submit();
    }

    window.submitProfileExportReport = submitProfileExportReport;

    document.querySelectorAll(".js-profile-export-template").forEach((link) => {
        link.addEventListener("click", (event) => {
            event.preventDefault();
            submitProfileExportReport(link.dataset.templateKey, link.dataset.templateTitle);
        });
    });
})();
