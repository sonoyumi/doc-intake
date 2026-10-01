// Small helpers only; every page works without JavaScript.
document.addEventListener("submit", (event) => {
  const form = event.target;
  const button = event.submitter;
  if (button && button.classList.contains("danger") && !confirm("Reject this document?")) {
    event.preventDefault();
    return;
  }
  // a double click must not send the form twice
  if (form.dataset.sent) {
    event.preventDefault();
    return;
  }
  form.dataset.sent = "1";
  if (button && button.name) {
    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = button.name;
    hidden.value = button.value;
    form.appendChild(hidden);
  }
  setTimeout(() => form.querySelectorAll("button").forEach((b) => (b.disabled = true)), 0);
});
