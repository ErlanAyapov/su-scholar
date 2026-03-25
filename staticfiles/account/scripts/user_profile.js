(function () {
  "use strict";

  const editModalElement = document.getElementById("userProfileEditModal");
  if (!editModalElement || typeof bootstrap === "undefined") {
    return;
  }

  const fileInput = document.getElementById("id_photo");
  const cropButton = document.getElementById("profilePhotoCropButton");
  const previewImage = document.getElementById("profileEditPhotoPreview");
  const previewFallback = document.getElementById("profileEditPhotoFallback");
  const sizeHint = document.getElementById("profilePhotoSizeHint");
  const cropModalElement = document.getElementById("profilePhotoCropModal");
  const cropImage = document.getElementById("profileCropImage");
  const applyCropButton = document.getElementById("profilePhotoApplyCropButton");
  const clearCheckbox = document.querySelector('input[name="photo-clear"]');

  if (
    !fileInput
    || !cropButton
    || !previewImage
    || !previewFallback
    || !sizeHint
    || !cropModalElement
    || !cropImage
    || !applyCropButton
  ) {
    return;
  }

  const editModal = new bootstrap.Modal(editModalElement);
  const cropModal = new bootstrap.Modal(cropModalElement);
  const cropperIsAvailable = typeof Cropper !== "undefined";

  let cropper = null;
  let cropSourceFile = null;
  let cropSourceUrl = null;

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) {
      return "0 KB";
    }
    const kb = bytes / 1024;
    if (kb < 1024) {
      return `${kb.toFixed(1)} KB`;
    }
    return `${(kb / 1024).toFixed(2)} MB`;
  }

  function buildJpegName(originalName) {
    const safeName = String(originalName || "profile-photo").replace(/\.[^.]+$/, "").trim();
    return `${safeName || "profile-photo"}.jpg`;
  }

  function showFallbackPreview() {
    previewImage.classList.add("d-none");
    previewFallback.classList.remove("d-none");
    previewImage.removeAttribute("src");
  }

  function showImagePreviewFromFile(file) {
    const reader = new FileReader();
    reader.onload = function onLoad(event) {
      const result = event.target && event.target.result;
      if (!result) {
        showFallbackPreview();
        return;
      }
      previewImage.src = result;
      previewImage.classList.remove("d-none");
      previewFallback.classList.add("d-none");
    };
    reader.readAsDataURL(file);
  }

  function revokeCropSourceUrl() {
    if (cropSourceUrl) {
      URL.revokeObjectURL(cropSourceUrl);
      cropSourceUrl = null;
    }
  }

  function assignFileToInput(file) {
    if (typeof DataTransfer === "undefined") {
      return false;
    }

    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    fileInput.files = dataTransfer.files;
    return true;
  }

  function setSizeHint(text) {
    sizeHint.textContent = text;
  }

  function openCropModalForFile(file) {
    if (!file || !file.type || !file.type.startsWith("image/")) {
      return;
    }
    cropSourceFile = file;
    revokeCropSourceUrl();
    cropSourceUrl = URL.createObjectURL(file);
    cropImage.src = cropSourceUrl;
    cropModal.show();
  }

  if (editModalElement.dataset.openOnLoad === "1") {
    editModal.show();
  }

  if (!cropperIsAvailable) {
    cropButton.disabled = true;
    setSizeHint("Редактор фото недоступен. Изображение будет сохранено и сжато на сервере.");
  }

  fileInput.addEventListener("change", function handlePhotoSelection() {
    const selectedFile = fileInput.files && fileInput.files[0];
    if (!selectedFile) {
      cropButton.disabled = true;
      setSizeHint("Фото не выбрано.");
      if (clearCheckbox && clearCheckbox.checked) {
        showFallbackPreview();
      }
      return;
    }

    if (!selectedFile.type || !selectedFile.type.startsWith("image/")) {
      fileInput.value = "";
      cropButton.disabled = true;
      setSizeHint("Выберите файл изображения (JPG, PNG, WEBP).");
      return;
    }

    if (clearCheckbox) {
      clearCheckbox.checked = false;
    }

    showImagePreviewFromFile(selectedFile);
    cropButton.disabled = !cropperIsAvailable;
    setSizeHint(`Выбрано фото: ${formatBytes(selectedFile.size)}.`);

    if (cropperIsAvailable) {
      openCropModalForFile(selectedFile);
    }
  });

  cropButton.addEventListener("click", function openCropEditor() {
    const selectedFile = fileInput.files && fileInput.files[0];
    if (!selectedFile || !cropperIsAvailable) {
      return;
    }
    openCropModalForFile(selectedFile);
  });

  if (clearCheckbox) {
    clearCheckbox.addEventListener("change", function handlePhotoClearToggle() {
      if (!clearCheckbox.checked) {
        return;
      }
      fileInput.value = "";
      cropButton.disabled = true;
      setSizeHint("Текущее фото будет удалено после сохранения формы.");
      showFallbackPreview();
    });
  }

  cropModalElement.addEventListener("shown.bs.modal", function initCropper() {
    if (!cropImage.getAttribute("src") || !cropperIsAvailable) {
      return;
    }

    cropper = new Cropper(cropImage, {
      aspectRatio: 1,
      viewMode: 1,
      dragMode: "move",
      autoCropArea: 1,
      responsive: true,
      background: false,
      guides: true,
      center: true,
      highlight: false,
      toggleDragModeOnDblclick: false,
      minCropBoxWidth: 120,
      minCropBoxHeight: 120,
    });
  });

  cropModalElement.addEventListener("hidden.bs.modal", function cleanupCropper() {
    if (cropper) {
      cropper.destroy();
      cropper = null;
    }
    cropImage.removeAttribute("src");
    revokeCropSourceUrl();
  });

  applyCropButton.addEventListener("click", function applyCrop() {
    if (!cropper) {
      return;
    }

    const sourceSize = cropSourceFile ? cropSourceFile.size : 0;
    const canvas = cropper.getCroppedCanvas({
      width: 960,
      height: 960,
      imageSmoothingEnabled: true,
      imageSmoothingQuality: "high",
      fillColor: "#ffffff",
    });

    if (!canvas) {
      return;
    }

    applyCropButton.disabled = true;
    canvas.toBlob(
      function afterBlob(blob) {
        applyCropButton.disabled = false;
        if (!blob) {
          setSizeHint("Не удалось обработать изображение. Попробуйте другой файл.");
          return;
        }

        const croppedFile = new File([blob], buildJpegName(cropSourceFile && cropSourceFile.name), {
          type: "image/jpeg",
          lastModified: Date.now(),
        });

        if (!assignFileToInput(croppedFile)) {
          setSizeHint("Обрезка выполнена, но браузер не позволил автоматически обновить файл.");
          cropModal.hide();
          return;
        }

        showImagePreviewFromFile(croppedFile);
        const compressionPercent = sourceSize > 0
          ? Math.max(0, Math.round((1 - (croppedFile.size / sourceSize)) * 100))
          : 0;
        if (compressionPercent > 0) {
          setSizeHint(`Сжатие: ${formatBytes(sourceSize)} -> ${formatBytes(croppedFile.size)} (${compressionPercent}%).`);
        } else {
          setSizeHint(`Размер после обработки: ${formatBytes(croppedFile.size)}.`);
        }

        cropModal.hide();
      },
      "image/jpeg",
      0.84
    );
  });
})();
