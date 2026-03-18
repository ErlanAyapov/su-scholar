"use strict";

(function initResearcherHierarchyFilters() {
    const universitySelect = document.getElementById("researcherUniversitySelect");
    const instituteSelect = document.getElementById("researcherInstituteSelect");
    const departmentSelect = document.getElementById("researcherDepartmentSelect");

    if (!universitySelect || !instituteSelect || !departmentSelect) {
        return;
    }

    const instituteOptions = Array.from(instituteSelect.options)
        .filter((option) => option.value)
        .map((option) => ({
            value: option.value,
            label: option.textContent.trim(),
            universityId: option.dataset.universityId || "",
        }));

    const departmentOptions = Array.from(departmentSelect.options)
        .filter((option) => option.value)
        .map((option) => ({
            value: option.value,
            label: option.textContent.trim(),
            instituteId: option.dataset.instituteId || "",
            universityId: option.dataset.universityId || "",
        }));

    function rebuildSelect(selectElement, options, selectedValue, emptyLabel) {
        const normalizedSelectedValue = String(selectedValue || "");
        let selectedExists = false;

        selectElement.innerHTML = "";
        const defaultOption = document.createElement("option");
        defaultOption.value = "";
        defaultOption.textContent = emptyLabel || "Все";
        selectElement.appendChild(defaultOption);

        options.forEach((optionData) => {
            const option = document.createElement("option");
            option.value = optionData.value;
            option.textContent = optionData.label;

            if (normalizedSelectedValue && optionData.value === normalizedSelectedValue) {
                option.selected = true;
                selectedExists = true;
            }
            selectElement.appendChild(option);
        });

        if (!selectedExists) {
            selectElement.value = "";
        }
    }

    function refreshHierarchy() {
        const universityId = universitySelect.value;
        const previousInstitute = instituteSelect.value;
        const previousDepartment = departmentSelect.value;

        if (!universityId) {
            rebuildSelect(instituteSelect, [], "", "Сначала выберите университет");
            rebuildSelect(departmentSelect, [], "", "Сначала выберите институт");
            instituteSelect.disabled = true;
            departmentSelect.disabled = true;
            return;
        }

        const filteredInstitutes = instituteOptions.filter(
            (optionData) => optionData.universityId === universityId
        );
        rebuildSelect(instituteSelect, filteredInstitutes, previousInstitute, "Все");
        instituteSelect.disabled = false;

        const instituteId = instituteSelect.value;
        if (!instituteId) {
            rebuildSelect(departmentSelect, [], "", "Сначала выберите институт");
            departmentSelect.disabled = true;
            return;
        }

        const filteredDepartments = departmentOptions.filter(
            (optionData) => optionData.instituteId === instituteId
        );
        rebuildSelect(departmentSelect, filteredDepartments, previousDepartment, "Все");
        departmentSelect.disabled = false;
    }

    universitySelect.addEventListener("change", () => {
        refreshHierarchy();
    });

    instituteSelect.addEventListener("change", () => {
        refreshHierarchy();
    });

    refreshHierarchy();
})();
