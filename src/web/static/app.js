/*
 * TrialBridge web UI behaviours (06-04-PLAN.md Task 2; extended by
 * 06-06-PLAN.md Task 2 with the login page's Show/Hide key toggle and
 * busy-label-on-submit; extended by 07-01-PLAN.md Task 1 with the confirm
 * dialog's [data-open-dialog]/[data-close-dialog] delegation; extended by
 * 07-03-PLAN.md Task 2 with the finalize dialog's busy/Esc/close/focus
 * interaction states; extended by 08.2-07-PLAN.md Task 3 with the criteria
 * disclosure opener for stepper step 2 and the #criteria hash; extended by
 * quick task 260929-hk6 (08.2-REVIEW.md WR-04) with the login button's
 * restore on a bfcache pageshow; extended by 09-05-PLAN.md with the scorecard's
 * Download PDF progressive enhancement -- fetch + blob with a "Preparing PDF…"
 * busy state -- section (9)).
 *
 * Vanilla JS, no dependencies, one IIFE. Every listener is registered at
 * the document level (event delegation) so htmx-swapped content works
 * without rebinding. Every text write uses `textContent` -- never an
 * HTML-string element-content assignment of any kind.
 */
(function () {
  "use strict";

  var COLLAPSED_GLYPH = "▸"; // ▸
  var EXPANDED_GLYPH = "▾"; // ▾
  var NCT_ID_PATTERN = /^NCT\d{8}$/;
  var COPY_RESET_DELAY_MS = 2000;
  var FINALIZE_FORM_ID = "finalize-form";
  var BUSY_ATTR = "data-busy";
  var LOGIN_ACTION_SUFFIX = "/web/login";
  var PDF_FAILED_MESSAGE = "PDF could not be generated.";
  var PDF_FALLBACK_FILENAME = "trialbridge-scorecard.pdf";
  var PDF_FILENAME_PATTERN = /filename="([A-Za-z0-9._-]+)"/;
  var PDF_CONTENT_TYPE = "application/pdf";
  var OBJECT_URL_REVOKE_DELAY_MS = 1000;

  // -- (1) Expand/collapse: button.expand-affordance and its containing row --

  function swapGlyph(button, expanded) {
    var text = button.textContent;
    var glyph = expanded ? EXPANDED_GLYPH : COLLAPSED_GLYPH;
    if (text.indexOf(COLLAPSED_GLYPH) === 0) {
      button.textContent = glyph + text.slice(COLLAPSED_GLYPH.length);
    } else if (text.indexOf(EXPANDED_GLYPH) === 0) {
      button.textContent = glyph + text.slice(EXPANDED_GLYPH.length);
    }
  }

  function toggleExpand(button) {
    var wasExpanded = button.getAttribute("aria-expanded") === "true";
    var nowExpanded = !wasExpanded;
    button.setAttribute("aria-expanded", String(nowExpanded));

    var targetId = button.getAttribute("aria-controls");
    var target = targetId ? document.getElementById(targetId) : null;
    if (target) {
      target.hidden = !nowExpanded;
    }

    swapGlyph(button, nowExpanded);
  }

  // -- (2) Copy link: [data-copy-link] --

  function showFallbackInput(button) {
    var container = button.parentElement;
    if (!container) {
      return;
    }
    var fallback = container.querySelector(".copy-link-fallback");
    if (!fallback) {
      return;
    }
    fallback.hidden = false;
    fallback.select();
  }

  function showCopiedLabel(button) {
    var originalText = button.textContent;
    button.textContent = "Link copied";
    window.setTimeout(function () {
      button.textContent = originalText;
    }, COPY_RESET_DELAY_MS);
  }

  function handleCopyLink(button) {
    var link = button.getAttribute("data-copy-link");
    if (!link) {
      return;
    }
    var absoluteLink = window.location.origin + link;

    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(absoluteLink).then(
        function () {
          showCopiedLabel(button);
        },
        function () {
          showFallbackInput(button);
        }
      );
    } else {
      showFallbackInput(button);
    }
  }

  // -- (4) Show/Hide key toggle: [data-toggle-key] (06-06-PLAN.md Task 2) --

  function toggleKeyVisibility(button) {
    var targetId = button.getAttribute("aria-controls");
    var input = targetId ? document.getElementById(targetId) : null;
    if (!input) {
      return;
    }
    var wasPressed = button.getAttribute("aria-pressed") === "true";
    var nowPressed = !wasPressed;
    input.type = nowPressed ? "text" : "password";
    button.setAttribute("aria-pressed", String(nowPressed));
    button.textContent = nowPressed ? "Hide" : "Show";
  }

  // -- (6) Confirm dialog open/close: [data-open-dialog] / [data-close-dialog] --
  // -- (07-01-PLAN.md Task 1) --

  function openDialog(button) {
    var targetId = button.getAttribute("data-open-dialog");
    var dialog = targetId ? document.getElementById(targetId) : null;
    if (dialog && typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  }

  function closeDialog(button) {
    var targetId = button.getAttribute("data-close-dialog");
    var dialog = targetId ? document.getElementById(targetId) : null;
    if (dialog && typeof dialog.close === "function") {
      dialog.close();
    }
  }

  // -- (7) Finalize dialog interaction states: busy, Esc/Cancel-while-busy, --
  // -- close-on-response, focus management (07-03-PLAN.md Task 2) --

  function setDialogBusy(dialog, busy) {
    if (!dialog) {
      return;
    }
    if (busy) {
      dialog.setAttribute(BUSY_ATTR, "true");
    } else {
      dialog.removeAttribute(BUSY_ATTR);
    }

    var buttons = dialog.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      btn.disabled = busy;
      if (btn.type === "submit") {
        if (busy) {
          btn.setAttribute("data-idle-label", btn.textContent);
          var busyLabel = btn.getAttribute("data-busy-label");
          if (busyLabel) {
            btn.textContent = busyLabel;
          }
        } else {
          var idleLabel = btn.getAttribute("data-idle-label");
          if (idleLabel) {
            btn.textContent = idleLabel;
          }
        }
      }
    }
  }

  function openerFor(dialog) {
    return document.querySelector('[data-open-dialog="' + dialog.id + '"]');
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    var elt = event.detail && event.detail.elt;
    if (!elt || elt.id !== FINALIZE_FORM_ID) {
      return;
    }
    var dialog = elt.closest("dialog");
    setDialogBusy(dialog, true);
  });

  function clearFinalizeBusyOnResponse(event) {
    var elt = event.detail && event.detail.elt;
    if (!elt || elt.id !== FINALIZE_FORM_ID) {
      return;
    }
    var dialog = elt.closest("dialog");
    if (!dialog || !document.contains(dialog)) {
      // Success / already-finalized path: #screening-panel (the dialog's
      // ancestor) was swapped out entirely -- nothing left here to reset.
      return;
    }
    setDialogBusy(dialog, false);
    if (dialog.open) {
      dialog.close();
    }
    var opener = openerFor(dialog);
    if (opener && document.contains(opener)) {
      opener.focus();
    }
  }

  document.addEventListener("htmx:afterRequest", clearFinalizeBusyOnResponse);
  // htmx:afterRequest does not fire on a network-layer failure -- htmx fires
  // htmx:sendError (connection failure) or htmx:timeout instead. Without
  // these, a dropped connection during finalize would leave every dialog
  // button permanently disabled with "Finalizing..." stuck.
  document.addEventListener("htmx:sendError", clearFinalizeBusyOnResponse);
  document.addEventListener("htmx:timeout", clearFinalizeBusyOnResponse);

  document.addEventListener("htmx:afterSettle", function () {
    var focusTarget = document.querySelector("[data-focus-on-swap]");
    if (focusTarget) {
      focusTarget.removeAttribute("data-focus-on-swap");
      focusTarget.focus();
    }
  });

  // Dialog "cancel" (Esc) and "close" events do not bubble, so both
  // listeners are registered on the capture phase at the document level.
  document.addEventListener(
    "cancel",
    function (event) {
      var target = event.target;
      if (target && target.tagName === "DIALOG" && target.hasAttribute(BUSY_ATTR)) {
        event.preventDefault();
      }
    },
    true
  );

  document.addEventListener(
    "keydown",
    function (event) {
      if (event.key !== "Escape") {
        return;
      }
      var target = event.target;
      var dialog = target && typeof target.closest === "function" ? target.closest("dialog") : null;
      if (dialog && dialog.hasAttribute(BUSY_ATTR)) {
        event.preventDefault();
      }
    },
    true
  );

  document.addEventListener(
    "close",
    function (event) {
      var dialog = event.target;
      if (!dialog || dialog.tagName !== "DIALOG" || dialog.hasAttribute(BUSY_ATTR)) {
        return;
      }
      var opener = openerFor(dialog);
      if (opener && document.contains(opener)) {
        opener.focus();
      }
    },
    true
  );

  // -- (8) Criteria disclosure (08.2-07-PLAN.md Task 3, plan.md D1) --
  // Opens details#criteria (step 3's collapsed criteria section). It only
  // toggles the `open` property; nothing from the URL is written to the page.

  function openCriteria() {
    var section = document.getElementById("criteria");
    if (section && section.tagName === "DETAILS") {
      section.open = true;
    }
  }

  function openCriteriaForHash() {
    if (window.location.hash === "#criteria") {
      openCriteria();
    }
  }

  document.addEventListener("DOMContentLoaded", openCriteriaForHash);
  window.addEventListener("hashchange", openCriteriaForHash);

  // -- (9) Download PDF (09-05-PLAN.md; EXPERIENCE.md PDF generating / PDF failed) --
  // The anchor a[data-download-pdf] is a plain link that works with JavaScript off.
  // Here it is upgraded: fetch the export, and only a 200 application/pdf answer is
  // saved (through a temporary object-URL link), so an error page never lands on disk
  // as a .pdf. The control is aria-disabled and reads its data-busy-label meanwhile.

  var pdfIdleLabel = null;

  function setPdfBusy(anchor, busy) {
    if (busy) {
      pdfIdleLabel = anchor.textContent;
      var busyLabel = anchor.getAttribute("data-busy-label");
      if (busyLabel) {
        anchor.textContent = busyLabel;
      }
      anchor.setAttribute("aria-disabled", "true");
    } else {
      if (pdfIdleLabel !== null) {
        anchor.textContent = pdfIdleLabel;
        pdfIdleLabel = null;
      }
      anchor.removeAttribute("aria-disabled");
    }
  }

  function clearPdfFeedback() {
    var region = document.getElementById("pdf-feedback");
    if (!region) {
      return;
    }
    while (region.firstChild) {
      region.removeChild(region.firstChild);
    }
  }

  function pdfFilename(response) {
    var disposition = response.headers.get("Content-Disposition") || "";
    var match = PDF_FILENAME_PATTERN.exec(disposition);
    return match ? match[1] : PDF_FALLBACK_FILENAME;
  }

  function saveBlob(blob, filename) {
    var objectUrl = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.setAttribute("download", filename);
    link.setAttribute("href", objectUrl);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(function () {
      URL.revokeObjectURL(objectUrl);
    }, OBJECT_URL_REVOKE_DELAY_MS);
  }

  function isPdfResponse(response) {
    var contentType = response.headers.get("Content-Type") || "";
    return response.ok && contentType.indexOf(PDF_CONTENT_TYPE) === 0;
  }

  function showPdfFailure(anchor) {
    var region = document.getElementById("pdf-feedback");
    if (!region) {
      return;
    }
    clearPdfFeedback();
    var box = document.createElement("div");
    box.className = "inline-error";
    box.setAttribute("role", "alert");
    box.appendChild(document.createTextNode(PDF_FAILED_MESSAGE + " "));
    var retry = document.createElement("a");
    retry.className = "link";
    retry.setAttribute("data-pdf-retry", "");
    retry.setAttribute("href", anchor.getAttribute("href") || "");
    retry.textContent = "Retry";
    box.appendChild(retry);
    region.appendChild(box);
  }

  // An ended session answers the export with a redirect to the sign-in page, which fetch
  // follows. Move the page there -- but only to a same-origin address.
  function isSameOriginRedirect(response) {
    if (!response.redirected) {
      return false;
    }
    try {
      return new URL(response.url).origin === window.location.origin;
    } catch (error) {
      return false;
    }
  }

  function startPdfDownload(anchor) {
    if (anchor.getAttribute("aria-disabled") === "true") {
      return;
    }
    setPdfBusy(anchor, true);
    fetch(anchor.href, { credentials: "same-origin" })
      .then(function (response) {
        if (!isPdfResponse(response)) {
          if (isSameOriginRedirect(response)) {
            // Leave the control busy while the page goes away.
            window.location.assign(response.url);
            return null;
          }
          setPdfBusy(anchor, false);
          showPdfFailure(anchor);
          return null;
        }
        var filename = pdfFilename(response);
        return response.blob().then(function (blob) {
          saveBlob(blob, filename);
          clearPdfFeedback();
          setPdfBusy(anchor, false);
        });
      })
      .catch(function () {
        setPdfBusy(anchor, false);
        showPdfFailure(anchor);
      });
  }

  // -- Document-level click delegation --

  document.addEventListener("click", function (event) {
    var pdfAnchor = event.target.closest("a[data-download-pdf]");
    if (pdfAnchor) {
      // A modified click (new tab, new window, save link) keeps the browser's own behaviour.
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
        return;
      }
      event.preventDefault();
      startPdfDownload(pdfAnchor);
      return;
    }

    var retryLink = event.target.closest("[data-pdf-retry]");
    if (retryLink) {
      event.preventDefault();
      var pdfAnchorForRetry = document.querySelector("a[data-download-pdf]");
      if (pdfAnchorForRetry) {
        startPdfDownload(pdfAnchorForRetry);
      }
      return;
    }

    var openDialogButton = event.target.closest("[data-open-dialog]");
    if (openDialogButton) {
      openDialog(openDialogButton);
      return;
    }

    var closeDialogButton = event.target.closest("[data-close-dialog]");
    if (closeDialogButton) {
      closeDialog(closeDialogButton);
      return;
    }

    var expandButton = event.target.closest(".expand-affordance");
    if (expandButton) {
      toggleExpand(expandButton);
      return;
    }

    if (event.target.closest('a[href="#criteria"]')) {
      openCriteria();
    }

    var copyButton = event.target.closest("[data-copy-link]");
    if (copyButton) {
      handleCopyLink(copyButton);
      return;
    }

    var toggleKeyButton = event.target.closest("[data-toggle-key]");
    if (toggleKeyButton) {
      toggleKeyVisibility(toggleKeyButton);
      return;
    }

    var row = event.target.closest("tr.candidate-row");
    if (row) {
      var rowExpandButton = row.querySelector(".expand-affordance");
      if (rowExpandButton) {
        toggleExpand(rowExpandButton);
      }
    }
  });

  // -- (3) Client-side NCT ID trim+format pre-check (convenience only; the --
  // -- server re-validates with trials.NCT_ID_PATTERN on every request). --

  function showNctIdError(input, message) {
    var errorId = (input.id || "nct_id") + "-error";
    var errorElement = document.getElementById(errorId);
    if (!errorElement) {
      errorElement = document.createElement("div");
      errorElement.id = errorId;
      errorElement.className = "inline-error";
      errorElement.setAttribute("role", "alert");
      input.insertAdjacentElement("afterend", errorElement);
    }
    errorElement.textContent = message;
    input.setAttribute("aria-describedby", errorId);
  }

  // -- (5) Login busy label on submit (06-06-PLAN.md Task 2) --

  function isLoginForm(form) {
    var actionAttr = form.getAttribute("action") || "";
    return actionAttr.slice(-LOGIN_ACTION_SUFFIX.length) === LOGIN_ACTION_SUFFIX;
  }

  function activateBusyLabel(form) {
    if (!isLoginForm(form)) {
      return;
    }
    var submitButton = form.querySelector('button[type="submit"]');
    var busyLabel = submitButton ? submitButton.getAttribute("data-busy-label") : null;
    if (submitButton && busyLabel) {
      submitButton.setAttribute("data-idle-label", submitButton.textContent);
      submitButton.disabled = true;
      submitButton.textContent = busyLabel;
    }
  }

  // pageshow fires on window. `persisted` is true only when the page comes back from the
  // back/forward cache, which keeps the DOM exactly as submit left it (busy button and all);
  // a normal load's pageshow is ignored. Only a login button that activateBusyLabel put into
  // the busy state (it saved data-idle-label) is restored, so a server-rendered disabled
  // (locked out) button stays disabled. The finalize dialog has its own recovery paths and
  // is not touched here.
  function restoreLoginButtonOnBfcache(event) {
    if (!event.persisted) {
      return;
    }
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) {
      var form = forms[i];
      if (!isLoginForm(form)) {
        continue;
      }
      var submitButton = form.querySelector('button[type="submit"]');
      var idleLabel = submitButton ? submitButton.getAttribute("data-idle-label") : null;
      if (idleLabel === null) {
        continue;
      }
      submitButton.disabled = false;
      submitButton.textContent = idleLabel;
      submitButton.removeAttribute("data-idle-label");
    }
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || typeof form.querySelector !== "function") {
      return;
    }

    var input = form.querySelector('input[name="nct_id"]:not([type="hidden"])');
    if (input) {
      var trimmed = input.value.trim();
      input.value = trimmed;

      if (!NCT_ID_PATTERN.test(trimmed)) {
        event.preventDefault();
        showNctIdError(input, "Malformed NCT ID format: expected NCT followed by 8 digits.");
        return;
      }
    }

    activateBusyLabel(form);
  });

  window.addEventListener("pageshow", restoreLoginButtonOnBfcache);
})();
