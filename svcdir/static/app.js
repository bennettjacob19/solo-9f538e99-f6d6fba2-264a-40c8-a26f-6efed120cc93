// Confirmation dialogs for destructive actions. Names are read from data
// attributes (already HTML-escaped by the template), never interpolated into
// inline JavaScript.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) {
    return;
  }
  var name = form.dataset.name || "";
  if (!window.confirm(form.dataset.confirm.replace("{name}", name))) {
    event.preventDefault();
  }
});
