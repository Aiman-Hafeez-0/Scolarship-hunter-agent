// Small helpers for the dashboard (plain JavaScript, no framework). Every page works without
// this file; it only adds comfort: theme toggle, "/" for search, dialogs, toasts, and so on.
(function () {
  "use strict";

  var THEME_KEY = "theme";

  // ---------- Theme (light by default; "dark" saved in localStorage) ----------

  function savedTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }

  function applyTheme(theme) {
    var dark = theme === "dark";
    document.documentElement.classList.toggle("dark", dark);
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-pressed", dark ? "true" : "false");
      button.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
      button.setAttribute("title", dark ? "Switch to light mode" : "Switch to dark mode");
    });
  }

  function toggleTheme() {
    var next = document.documentElement.classList.contains("dark") ? "light" : "dark";
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* private mode: not saved */ }
    applyTheme(next);
  }

  // ---------- Opportunities: table or card view (saved in localStorage) ----------

  var VIEW_KEY = "opportunities-view";

  function applyView(view) {
    var box = document.getElementById("opps");
    if (!box) return;
    view = view === "cards" ? "cards" : "table";
    box.setAttribute("data-view", view);
    document.querySelectorAll("[data-view-toggle]").forEach(function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-view-toggle") === view ? "true" : "false");
    });
  }

  function savedView() {
    try { return localStorage.getItem(VIEW_KEY); } catch (e) { return null; }
  }

  // ---------- Toasts ----------

  var TOAST_MS = 6000;

  function armToast(toast) {
    if (toast.dataset.armed) return;
    toast.dataset.armed = "1";
    if (toast.dataset.autoclose === "true") {
      setTimeout(function () { toast.remove(); }, TOAST_MS);
    }
  }

  function armToasts(root) {
    (root || document).querySelectorAll(".toast").forEach(armToast);
    if (root && root.classList && root.classList.contains("toast")) armToast(root);
  }

  // A toast made here (for errors that never reached the server, or came back as an error).
  function showErrorToast(message) {
    var region = document.getElementById("toasts");
    if (!region) return;
    var template = document.getElementById("error-toast-template");
    var toast = template.content.firstElementChild.cloneNode(true);
    toast.querySelector("[data-toast-text]").textContent = message;
    region.prepend(toast);
    armToast(toast);
  }

  // ---------- Dialogs (slide-over, drawer, confirm) ----------

  function openDialog(id) {
    var dialog = document.getElementById(id);
    if (!dialog || typeof dialog.showModal !== "function") return false;
    if (dialog.open) dialog.close(); // opened without JS (the "open" attribute): reopen as modal
    dialog.showModal();
    return true;
  }

  // ---------- Events ----------

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element)) return;

    var opener = target.closest("[data-open-dialog]");
    if (opener && openDialog(opener.getAttribute("data-open-dialog"))) {
      event.preventDefault();
      return;
    }
    var closer = target.closest("[data-close-dialog]");
    if (closer) {
      var dialog = closer.closest("dialog");
      if (dialog) dialog.close();
      return;
    }
    // A click on the dark backdrop around a dialog closes it.
    if (target instanceof HTMLDialogElement && target.open) {
      var box = target.getBoundingClientRect();
      var inside = event.clientX >= box.left && event.clientX <= box.right &&
                   event.clientY >= box.top && event.clientY <= box.bottom;
      if (!inside) target.close();
      return;
    }
    if (target.closest("[data-theme-toggle]")) {
      toggleTheme();
      return;
    }
    var viewButton = target.closest("[data-view-toggle]");
    if (viewButton) {
      var view = viewButton.getAttribute("data-view-toggle");
      try { localStorage.setItem(VIEW_KEY, view); } catch (e) { /* not saved */ }
      applyView(view);
      return;
    }
    var dismiss = target.closest("[data-dismiss-toast]");
    if (dismiss) {
      var toast = dismiss.closest(".toast");
      if (toast) toast.remove();
      return;
    }
    var eye = target.closest("[data-toggle-password]");
    if (eye) {
      var input = document.getElementById(eye.getAttribute("data-toggle-password"));
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.setAttribute("aria-pressed", show ? "true" : "false");
      eye.setAttribute("aria-label", show ? "Hide password" : "Show password");
      eye.querySelector("[data-eye-open]").classList.toggle("hidden", show);
      eye.querySelector("[data-eye-closed]").classList.toggle("hidden", !show);
      return;
    }
    // Clickable table rows: the row opens its link (the link inside stays for keyboards).
    var rowLink = target.closest("tr[data-href]");
    if (rowLink && !target.closest("a, button, input, select, textarea, label")) {
      window.location.href = rowLink.getAttribute("data-href");
      return;
    }
    // Close open menus when clicking anywhere else.
    document.querySelectorAll("details[data-menu][open]").forEach(function (menu) {
      if (!menu.contains(target)) menu.removeAttribute("open");
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      document.querySelectorAll("details[data-menu][open]").forEach(function (menu) {
        menu.removeAttribute("open");
        var summary = menu.querySelector("summary");
        if (summary) summary.focus();
      });
      return;
    }
    // "/" jumps to the search box, unless you are typing somewhere.
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey) {
      var active = document.activeElement;
      var typing = active && (active.isContentEditable ||
        /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName));
      var search = Array.prototype.find.call(
        document.querySelectorAll("[data-global-search]"),
        function (input) { return input.offsetParent !== null; } // the one on screen
      );
      if (!typing && search) {
        event.preventDefault();
        search.focus();
        search.select();
      }
    }
  });

  // Forms that take a while (login, add source): spinner on the button, no double submit.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-loading-form")) return;
    var button = event.submitter || form.querySelector("button[type=submit], button:not([type])");
    if (!button) return;
    setTimeout(function () { // after the browser has collected the form values
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.querySelectorAll("[data-idle]").forEach(function (el) { el.classList.add("hidden"); });
      button.querySelectorAll("[data-busy]").forEach(function (el) {
        el.classList.remove("hidden");
        el.classList.add("inline-flex");
      });
    }, 0);
  });

  // ---------- HTMX ----------

  // Buttons marked data-confirm-dialog="<id>" first ask in that dialog.
  document.addEventListener("htmx:confirm", function (event) {
    var source = event.detail.elt;
    var holder = source && source.closest("[data-confirm-dialog]");
    if (!holder) return;
    var dialog = document.getElementById(holder.getAttribute("data-confirm-dialog"));
    if (!dialog || typeof dialog.showModal !== "function") return; // fall back: just send
    event.preventDefault();
    var yes = dialog.querySelector("[data-confirm-yes]");
    yes.onclick = function () {
      dialog.close();
      event.detail.issueRequest(true);
    };
    dialog.showModal();
  });

  document.addEventListener("htmx:load", function (event) { armToasts(event.target); });

  document.addEventListener("htmx:responseError", function (event) {
    var status = event.detail.xhr.status;
    showErrorToast(status === 503 ? "The database could not be reached. Try again in a minute."
      : status === 403 ? "This page has expired. Reload it and try again."
      : status === 404 ? "That item no longer exists. Reload the page."
      : "Something went wrong (error " + status + ").");
  });
  document.addEventListener("htmx:sendError", function () {
    showErrorToast("No connection to the dashboard server.");
  });
  document.addEventListener("htmx:timeout", function () {
    showErrorToast("The server took too long to answer.");
  });

  // ---------- Start ----------

  function start() {
    applyTheme(savedTheme());
    applyView(savedView());
    armToasts(document);
    document.querySelectorAll("dialog[data-open-on-load]").forEach(function (dialog) {
      openDialog(dialog.id);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
