const setupToken = new URLSearchParams(location.hash.slice(1)).get("setup");
if (setupToken) {
  history.replaceState(null, "", "/");
  document.querySelector("#signin-title").textContent = "Your workspace.";
  document.querySelector("#signin-description").textContent =
    "Choose a password. This account is just for you.";
  document.querySelector("#signin-submit").textContent = "Create password";
  document.querySelector("#password").autocomplete = "new-password";
  document.querySelector("#password").minLength = 12;
}
document.querySelector("#signin-form").onsubmit = async (event) => {
  event.preventDefault();
  const button = document.querySelector("#signin-submit");
  button.disabled = true;
  try {
    const response = await fetch(setupToken ? "/cloud/setup" : "/cloud/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        password: document.querySelector("#password").value,
        token: setupToken,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "Could not sign in");
    location.replace("/");
  } catch (error) {
    document.querySelector("#signin-error").textContent = error.message;
    button.disabled = false;
  }
};
