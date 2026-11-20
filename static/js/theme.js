(function () {
  try {
    var t = localStorage.getItem("fc-theme");
    if (t === "neon" || t === "ember" || t === "aurora") document.documentElement.setAttribute("data-theme", t);
  } catch (e) {}
})();
