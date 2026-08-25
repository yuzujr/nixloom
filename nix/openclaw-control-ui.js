/* NixLoom's Control UI adaptation layer.
 *
 * Loaded as an external same-origin script (the gateway CSP permits
 * script-src 'self'), so it runs regardless of inline-script policies and is
 * easy to maintain.  It adapts runtime behavior that CSS cannot express:
 *
 *  - message timestamps on mobile (today → HH:MM, older → M/D HH:MM),
 *  - the "+" composer action sheet that re-triggers the app's own controls
 *    (attach / voice / settings / context) without hiding any functionality.
 *
 * Everything is defensive: a missing selector no-ops rather than throws, and a
 * MutationObserver keeps the timestamp formatting correct across streaming
 * re-renders.  The "+" sheet lives outside the Lit-managed subtree, is rebuilt
 * from fresh selectors on every open, and closes before triggering an action,
 * so re-renders and popovers cannot collide with it.
 */
(function () {
  "use strict";
  var MOBILE_LIMIT = 768;
  if (window.innerWidth > MOBILE_LIMIT) return;

  /* ---------------- Message timestamps: today → HH:MM ---------------- */
  function pad(n) {
    return n < 10 ? "0" + n : "" + n;
  }

  function fmtTimestamp(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var t = pad(d.getHours()) + ":" + pad(d.getMinutes());
    return d.toDateString() === new Date().toDateString()
      ? t
      : (d.getMonth() + 1) + "/" + d.getDate() + " " + t;
  }

  function updateTimestamps() {
    var nodes = document.querySelectorAll(".chat-group-timestamp");
    for (var i = 0; i < nodes.length; i++) {
      var dt = nodes[i].getAttribute("datetime");
      if (!dt) continue;
      var v = fmtTimestamp(dt);
      if (v) nodes[i].textContent = v;
    }
  }

  updateTimestamps();
  if (window.MutationObserver) {
    var stampTimer = null;
    new MutationObserver(function () {
      clearTimeout(stampTimer);
      stampTimer = setTimeout(updateTimestamps, 100);
    }).observe(document.body, { childList: true, subtree: true });
  }

  /* ---------------- "+" composer action sheet ---------------- */
  var SHEET = null;
  var BACKDROP = null;

  /* "Context" reveal is tracked in a flag and re-applied after re-renders:
   * Lit owns the composer's className, so any state update rebuilds it without
   * our class.  A MutationObserver re-adds it shortly after (same pattern as
   * the timestamp formatting), so the class survives until toggled off. */
  var ctxMode = false;

  function applyCtxClass() {
    var composer = document.querySelector(".agent-chat__input");
    if (composer) composer.classList.toggle("nixloom-ctx", ctxMode);
  }

  var ctxApplyTimer = null;
  new MutationObserver(function () {
    if (!ctxMode) return;
    clearTimeout(ctxApplyTimer);
    ctxApplyTimer = setTimeout(applyCtxClass, 50);
  }).observe(document.body, { childList: true, subtree: true });

  /* Items carry selectors, not element references, and are re-queried at
   * trigger time: Lit re-renders the composer constantly, so a captured node
   * would go stale.  Items that have no backing control in the current session
   * are omitted entirely — nothing is shown that cannot actually run. */
  function buildItems() {
    var composer = document.querySelector(".agent-chat__input");
    if (!composer) return [];
    var items = [];

    if (composer.querySelector(".agent-chat__file-input")) {
      items.push({ label: "Attach file", icon: "📎", sel: ".agent-chat__file-input" });
    }
    if (composer.querySelector(".agent-chat__toolbar-left .agent-chat__input-btn:nth-of-type(2)")) {
      items.push({ label: "Voice", icon: "🎙", sel: ".agent-chat__toolbar-left .agent-chat__input-btn:nth-of-type(2)" });
    }
    if (composer.querySelector(".chat-settings-chip")) {
      items.push({ label: "Session settings", icon: "⚙️", sel: ".chat-settings-chip" });
    }
    /* "Context" is the conversation context, not billing/usage: reveal the
     * app's own session context notice (tokens used / window) in place. */
    if (composer.querySelector(".context-notice")) {
      items.push({ label: "Context", icon: "◎", special: "context" });
    }

    return items;
  }

  var openedAt = 0;

  function closeSheet() {
    /* The tap that opened the sheet keeps producing trailing events (pointerup,
     * a synthetic click, a re-render) for a few ms; those must not instantly
     * tear the sheet back down.  Ignore closes inside a short window after
     * opening — the user can still dismiss by tapping the backdrop later. */
    if (Date.now() - openedAt < 250) return;
    if (BACKDROP) { BACKDROP.remove(); BACKDROP = null; }
    if (SHEET) { SHEET.remove(); SHEET = null; }
  }

  function openSheet() {
    closeSheet();
    openedAt = Date.now();
    var items = buildItems();
    if (!items.length) return;

    BACKDROP = document.createElement("div");
    BACKDROP.style.cssText =
      "position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.45);";
    /* Close on pointerdown, not click: the tap that opened the sheet generates
     * a synthetic click that would otherwise land on the freshly-added
     * backdrop and instantly close it again (the ADDED→REMOVED symptom). */
    BACKDROP.addEventListener("pointerdown", closeSheet);
    document.body.appendChild(BACKDROP);

    SHEET = document.createElement("div");
    SHEET.id = "nixloom-composer-more";
    SHEET.setAttribute("role", "menu");
    SHEET.style.cssText =
      "position:fixed;left:0;right:0;bottom:0;z-index:9999;box-sizing:border-box;" +
      "background:var(--card,#161b22);border-top:1px solid var(--border,#2a3038);" +
      "border-radius:16px 16px 0 0;padding:6px 0 max(10px,env(safe-area-inset-bottom));" +
      "box-shadow:0 -8px 40px rgba(0,0,0,.35);";
    items.forEach(function (item) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = item.icon + "  " + item.label;
      b.style.cssText =
        "display:flex;width:100%;align-items:center;gap:10px;padding:13px 20px;" +
        "font:inherit;font-size:15px;color:var(--text);background:none;border:none;" +
        "text-align:left;cursor:pointer;";
      b.addEventListener("click", function (e) {
        /* The tap that opened the sheet covers the "+" button, so its trailing
         * synthetic click can land on a sheet item and trigger it
         * unintentionally.  Ignore item clicks inside a short window after
         * opening — a real user takes longer than that to pick an item. */
        if (Date.now() - openedAt < 250) return;
        /* Stop the tap from reaching the app: a bubble-phase document handler
         * would otherwise trigger a Lit state update that re-renders the
         * composer and wipes the class we are about to toggle. */
        if (e) { e.preventDefault(); e.stopPropagation(); }
        closeSheet();
        if (item.special === "context") {
          ctxMode = !ctxMode;
          applyCtxClass();
        } else {
          var el = document.querySelector(item.sel);
          if (el) el.click();
        }
      });
      SHEET.appendChild(b);
    });
    document.body.appendChild(SHEET);
  }

  /* The "+" button opens the sheet instead of the file picker; the sheet's
   * "Attach file" entry drives the file input directly.  Wire it on
   * pointerdown, not click: on a touch screen the app's composer focus step
   * swallows the synthetic click on the first tap (the event flow shows
   * pointerdown → focusin → mouseup with no click), which made "+" require
   * two taps.  pointerdown fires before that focus, and preventDefault stops
   * the focus/file-picker from ever happening.  Capture-phase delegation on
   * document survives every Lit re-render. */
  document.addEventListener(
    "pointerdown",
    function (e) {
      var target = e.target.closest
        ? e.target.closest(".agent-chat__toolbar-left .agent-chat__input-btn")
        : null;
      if (target) {
        e.preventDefault();
        e.stopPropagation();
        openSheet();
      }
    },
    true,
  );

  /* The opening tap's synthetic click still lands on the button even though
   * pointerdown was prevented; swallow it so the app's own file-picker
   * handler never fires for a "+" that now means "open the sheet". */
  document.addEventListener(
    "click",
    function (e) {
      var target = e.target.closest
        ? e.target.closest(".agent-chat__toolbar-left .agent-chat__input-btn")
        : null;
      if (target) {
        e.preventDefault();
        e.stopPropagation();
      }
    },
    true,
  );
})();
