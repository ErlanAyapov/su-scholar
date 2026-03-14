(function () {
    const form = document.getElementById("registerForm");
    if (!form) {
        return;
    }

    const emailInput = document.getElementById("id_register_email");
    const emailHiddenInput = document.getElementById("id_email");
    const registerActionInput = document.getElementById("registerAction");
    const submitBtn = document.getElementById("registerSubmitBtn");
    const detailsBlock = document.getElementById("formDetails");
    const flowMessage = document.getElementById("registerFlowMessage");
    const nextInput = form.querySelector("input[name='next']");

    if (
        !emailInput ||
        !emailHiddenInput ||
        !registerActionInput ||
        !submitBtn ||
        !detailsBlock ||
        !flowMessage
    ) {
        return;
    }

    const statusUrl = form.dataset.emailStatusUrl || "";
    const activationUrl = form.dataset.activationUrl || "";
    const defaultLabel = submitBtn.dataset.defaultLabel || "Тіркелу";
    const activationLabel = submitBtn.dataset.activationLabel || "Аккаунты активациялау";

    let mode = detailsBlock.classList.contains("d-none") ? "locked" : "register";
    let statusTimer = null;
    let statusRequestController = null;

    const alertClasses = ["auth-alert-danger", "auth-alert-info", "auth-alert-success"];

    const isValidEmail = (email) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);

    const syncEmail = () => {
        emailHiddenInput.value = emailInput.value.trim().toLowerCase();
    };

    const setMessage = (kind, text) => {
        flowMessage.classList.remove("d-none", ...alertClasses);
        if (kind === "danger") {
            flowMessage.classList.add("auth-alert-danger");
        } else if (kind === "success") {
            flowMessage.classList.add("auth-alert-success");
        } else {
            flowMessage.classList.add("auth-alert-info");
        }
        flowMessage.textContent = text;
    };

    const clearMessage = () => {
        flowMessage.classList.add("d-none");
        flowMessage.classList.remove(...alertClasses);
        flowMessage.textContent = "";
    };

    const setMode = (nextMode) => {
        mode = nextMode;

        if (mode === "activate") {
            registerActionInput.value = "activate";
            submitBtn.textContent = activationLabel;
            detailsBlock.classList.add("d-none");
            submitBtn.disabled = !isValidEmail(emailHiddenInput.value);
            return;
        }

        if (mode === "register") {
            registerActionInput.value = "register";
            submitBtn.textContent = defaultLabel;
            detailsBlock.classList.remove("d-none");
            submitBtn.disabled = !isValidEmail(emailHiddenInput.value);
            return;
        }

        registerActionInput.value = "locked";
        submitBtn.textContent = defaultLabel;
        detailsBlock.classList.add("d-none");
        submitBtn.disabled = true;
    };

    const checkEmailStatus = async () => {
        syncEmail();
        const email = emailHiddenInput.value;

        if (!email) {
            clearMessage();
            setMode("locked");
            return;
        }

        if (!isValidEmail(email)) {
            setMode("locked");
            setMessage("danger", "Email форматы дұрыс емес.");
            return;
        }

        if (!statusUrl) {
            setMode("register");
            return;
        }

        if (statusRequestController) {
            statusRequestController.abort();
        }
        statusRequestController = new AbortController();

        submitBtn.disabled = true;

        try {
            const requestUrl = new URL(statusUrl, window.location.origin);
            requestUrl.searchParams.set("email", email);

            const response = await fetch(requestUrl.toString(), {
                method: "GET",
                signal: statusRequestController.signal,
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });

            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.detail || "Email тексеру кезінде қате шықты.");
            }

            if (data.status === "needs_activation") {
                setMode("activate");
                setMessage("info", data.detail || "Аккаунт табылды. Белсендіру хатын жібере аласыз.");
                return;
            }

            if (data.status === "already_registered") {
                setMode("locked");
                setMessage("danger", data.detail || "Бұл email бойынша аккаунт бар. Кіру бетіне өтіңіз.");
                return;
            }

            setMode("register");
            clearMessage();
        } catch (error) {
            if (error && error.name === "AbortError") {
                return;
            }
            setMode("locked");
            setMessage("danger", error.message || "Email тексеру сәтсіз аяқталды.");
        }
    };

    const queueStatusCheck = () => {
        if (statusTimer) {
            clearTimeout(statusTimer);
        }
        statusTimer = window.setTimeout(checkEmailStatus, 350);
    };

    form.addEventListener("submit", async (event) => {
        syncEmail();

        if (mode !== "activate") {
            return;
        }

        event.preventDefault();

        const email = emailHiddenInput.value;
        if (!isValidEmail(email)) {
            setMessage("danger", "Email форматы дұрыс емес.");
            submitBtn.disabled = false;
            return;
        }

        if (!activationUrl) {
            setMessage("danger", "Белсендіру сервисі қолжетімсіз.");
            submitBtn.disabled = false;
            return;
        }

        submitBtn.disabled = true;

        const csrfToken = form.querySelector("input[name='csrfmiddlewaretoken']")?.value || "";
        const nextUrl = nextInput ? nextInput.value : "";

        try {
            const response = await fetch(activationUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken,
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: JSON.stringify({
                    email,
                    next: nextUrl,
                }),
            });

            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.detail || "Хатты жіберу мүмкін болмады.");
            }

            setMessage("success", data.detail || "Белсендіру хаты жіберілді. Email-ді тексеріңіз.");
        } catch (error) {
            setMessage("danger", error.message || "Белсендіру хатын жіберу сәтсіз аяқталды.");
        } finally {
            submitBtn.disabled = false;
        }
    });

    emailInput.addEventListener("input", () => {
        syncEmail();
        queueStatusCheck();
    });

    emailInput.addEventListener("blur", () => {
        checkEmailStatus();
    });

    syncEmail();
    if (emailHiddenInput.value) {
        checkEmailStatus();
    } else {
        setMode("locked");
    }
})();
