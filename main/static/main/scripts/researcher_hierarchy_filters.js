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

    function rebuildSelect(selectElement, options, selectedValue) {
        const normalizedSelectedValue = String(selectedValue || "");
        let selectedExists = false;

        selectElement.innerHTML = "";
        const defaultOption = document.createElement("option");
        defaultOption.value = "";
        defaultOption.textContent = "Все";
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

    function filterInstitutesByUniversity() {
        const universityId = universitySelect.value;
        const currentInstituteValue = instituteSelect.value;
        const filteredInstitutes = instituteOptions.filter((optionData) => {
            return !universityId || optionData.universityId === universityId;
        });
        rebuildSelect(instituteSelect, filteredInstitutes, currentInstituteValue);
    }

    function filterDepartmentsByHierarchy() {
        const universityId = universitySelect.value;
        const instituteId = instituteSelect.value;
        const currentDepartmentValue = departmentSelect.value;
        const filteredDepartments = departmentOptions.filter((optionData) => {
            if (instituteId) {
                return optionData.instituteId === instituteId;
            }
            if (universityId) {
                return optionData.universityId === universityId;
            }
            return true;
        });
        rebuildSelect(departmentSelect, filteredDepartments, currentDepartmentValue);
    }

    universitySelect.addEventListener("change", () => {
        filterInstitutesByUniversity();
        filterDepartmentsByHierarchy();
    });

    instituteSelect.addEventListener("change", () => {
        filterDepartmentsByHierarchy();
    });

    filterInstitutesByUniversity();
    filterDepartmentsByHierarchy();
})();
