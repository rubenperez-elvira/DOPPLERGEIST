const form = document.querySelector("#login");
const error = document.querySelector("#login-error");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  const body = new FormData(form);
  const response = await fetch("/login", { method: "POST", body, credentials: "same-origin" });
  if (response.ok) {
    window.location.assign("/");
    return;
  }
  const payload = await response.json().catch(() => ({}));
  error.textContent = payload.message || "No se pudo entrar.";
});
