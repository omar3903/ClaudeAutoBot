/* The theme, picked before first paint. A plain script loaded from <head>, not a module, so the page
   is never drawn in the wrong theme first; a file, not inline script, because the app's
   Content-Security-Policy only runs the dashboard's own script files. */
// your saved choice, else the OS setting
(function () {
  var t = null;
  try { t = localStorage.getItem("atb-theme"); } catch (e) { }
  if (t !== "light" && t !== "dark")
    t = window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = t;
})();
