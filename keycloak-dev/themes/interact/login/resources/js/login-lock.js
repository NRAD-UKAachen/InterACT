/*
 * Cosmetic "wait a minute" lock for the InterACT login page.
 *
 * After 5 failed attempts for a username in THIS browser tab, the password
 * field and the Sign In button are disabled for 60 seconds, with a countdown.
 * Keycloak itself enforces the real lockout on the server (failureFactor /
 * waitIncrementSeconds in interact-realm.json) and does not depend on this
 * script: it does not know about guesses from other machines or tabs, and it
 * cannot see successful logins. Keycloak's wait grows after repeated
 * failures (60 s up to 15 min); this always waits 60 s, so after it unlocks
 * the server may still refuse for longer.
 *
 * To switch it off, remove the `scripts=` line from ../theme.properties.
 * Keep MAX in sync with failureFactor in interact-realm.json.
 */
function interactLoginLock() {
  var MAX = 5;
  var LOCK_MS = 60 * 1000;
  var IDLE_MS = 30 * 60 * 1000;
  var KEY = "interactLoginLock";

  var user = document.getElementById("username");
  var pass = document.getElementById("password");
  var btn = document.getElementById("kc-login");
  if (!user || !pass || !btn || !document.getElementById("kc-form-login")) return;

  // sessionStorage, not localStorage: typed usernames must not outlive the tab
  // on a shared workstation. Without storage the page works as normal.
  var store;
  try {
    store = JSON.parse(window.sessionStorage.getItem(KEY) || "{}");
  } catch (e) {
    return;
  }
  var now = Date.now();
  Object.keys(store).forEach(function (k) {
    var r = store[k];
    if ((!r.until || r.until < now) && now - r.t > IDLE_MS) delete store[k];
  });

  var error = document.getElementById("input-error");
  var failed = !!error && (error.textContent || "").indexOf("Invalid username or password") !== -1;
  if (!failed) {
    // A fresh login page: earlier failures may have been followed by a good
    // login that this page cannot see, so start counting again (a running
    // lock is kept - it only lasts a minute).
    Object.keys(store).forEach(function (k) { store[k].n = 0; });
  } else {
    var nav = window.performance && performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
    var reloaded = nav && (nav.type === "reload" || nav.type === "back_forward");
    var name = (user.value || "").trim().toLowerCase();
    if (name && !reloaded) {
      var rec = store[name] || { n: 0, t: now };
      rec.n += 1;
      rec.t = now;
      if (rec.n >= MAX) rec.until = now + LOCK_MS;
      store[name] = rec;
    }
  }
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(store));
  } catch (e) {}

  var note = document.createElement("div");
  note.className = "interact-lock";
  note.setAttribute("aria-live", "polite");
  note.hidden = true;
  btn.insertAdjacentElement("beforebegin", note);

  var locked = false;
  function refresh() {
    var rec = store[(user.value || "").trim().toLowerCase()];
    var left = rec && rec.until ? Math.ceil((rec.until - Date.now()) / 1000) : 0;
    if (left > 0) {
      locked = true;
      pass.disabled = true;
      btn.disabled = true;
      var s = left % 60;
      note.textContent = "Too many wrong attempts. Please wait " + Math.floor(left / 60) + ":" + (s < 10 ? "0" : "") + s + " before trying again.";
      note.hidden = false;
    } else if (locked) {
      locked = false;
      pass.disabled = false;
      btn.disabled = false;
      note.hidden = true;
      pass.focus();
    }
  }
  user.addEventListener("input", refresh);
  refresh();
  window.setInterval(refresh, 1000);
}

// Loaded from <head> (theme.properties "scripts"), so the form does not exist
// yet when this file runs: wait for the page.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", interactLoginLock);
} else {
  interactLoginLock();
}
