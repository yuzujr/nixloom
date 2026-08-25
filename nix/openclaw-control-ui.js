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

  /* Re-query the composer controls fresh on every open so Lit re-renders can
   * never leave us holding detached nodes.  Items that have no backing control
   * in the current session are simply omitted — nothing is shown that cannot
   * actually run. */
  function buildItems() {
    var composer = document.querySelector(".agent-chat__input");
    if (!composer) return [];
    var items = [];

    var file = composer.querySelector(".agent-chat__file-input");
    if (file) {
      items.push({ label: "Attach file", icon: "📎", trigger: function () { file.click(); } });
    }

    var voice = composer.querySelector(
      ".agent-chat__toolbar-left .agent-chat__input-btn:nth-of-type(2)",
    );
    if (voice) {
      items.push({ label: "Voice", icon: "🎙", trigger: function () { voice.click(); } });
    }

    var settings = composer.querySelector(".chat-settings-chip");
    if (settings) {
      items.push({ label: "Session settings", icon: "⚙️", trigger: function () { settings.click(); } });
    }

    /* "Context" is the conversation context, not billing/usage: reveal the
     * app's own session context notice (tokens used / window) in place. */
    var ctxNotice = composer.querySelector(".context-notice");
    if (ctxNotice) {
      items.push({ label: "Context", icon: "◎", trigger: function () {
        composer.classList.toggle("nixloom-ctx");
      } });
    }

    return items;
  }

  function closeSheet() {
    if (BACKDROP) { BACKDROP.remove(); BACKDROP = null; }
    if (SHEET) { SHEET.remove(); SHEET = null; }
  }

  function openSheet() {
    closeSheet();
    var items = buildItems();
    if (!items.length) return;

    BACKDROP = document.createElement("div");
    BACKDROP.style.cssText =
      "position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.45);";
    BACKDROP.addEventListener("click", closeSheet);
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
      b.addEventListener("click", function () {
        closeSheet();
        item.trigger();
      });
      SHEET.appendChild(b);
    });
    document.body.appendChild(SHEET);
  }

  /* The "+" button opens the sheet instead of the file picker; the sheet's
   * "Attach file" entry drives the file input directly.  Capture-phase
   * delegation on document survives every Lit re-render. */
  document.addEventListener(
    "click",
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
})();
