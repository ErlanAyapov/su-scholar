(function (global) {
    function ensureContainer() {
        let container = document.getElementById("appToastContainer");
        if (container) return container;
        container = document.createElement("div");
        container.id = "appToastContainer";
        container.className = "app-toast-container";
        document.body.appendChild(container);
        return container;
    }

    function showToast(message, options) {
        const opts = options || {};
        const duration = Number.isFinite(opts.duration) ? opts.duration : 2000;
        const type = (opts.type || "info").toString();
        const container = ensureContainer();

        const toast = document.createElement("div");
        toast.className = "app-toast app-toast-" + type;
        toast.textContent = message || "";
        container.appendChild(toast);

        requestAnimationFrame(function () {
            toast.classList.add("is-visible");
        });

        setTimeout(function () {
            toast.classList.remove("is-visible");
            setTimeout(function () {
                toast.remove();
            }, 180);
        }, duration);
    }

    global.AppFeedback = global.AppFeedback || {};
    global.AppFeedback.showToast = showToast;
})(window);
