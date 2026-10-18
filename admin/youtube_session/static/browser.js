import RFB from "/novnc/core/rfb.js";

const query = new URLSearchParams(location.search);
const ticket = query.get("ticket") || "";
// Remove ticket from address bar/referrer as soon as module runs.
history.replaceState({}, "", "/browser");
const notice = document.getElementById("browserNotice");
const banner = document.getElementById("remoteStatus");
let csrf = "";
let rfb;

async function api(path, method = "GET") {
  const headers = method === "GET" ? {} : {"X-CSRF-Token": csrf};
  const result = await fetch(path, {method, credentials:"same-origin", headers});
  if (!result.ok) throw new Error(await result.text());
  return result.json();
}

async function boot() {
  if (!ticket) { banner.textContent = "Missing browser ticket. Return to dashboard."; return; }
  const identity = await api("/api/me");
  csrf = identity.csrf;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const addr = scheme + "//" + location.host + "/browser/ws?ticket=" + encodeURIComponent(ticket);
  // noVNC connects only to our authenticated same-origin WS gateway.
  rfb = new RFB(document.getElementById("screen"), addr, {shared:false});
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.addEventListener("connect", () => { banner.textContent = ""; banner.hidden = true; notice.textContent = "Browser connected. Check real Google URL before signing in."; });
  rfb.addEventListener("disconnect", () => {
    banner.hidden = false;
    banner.textContent = "Browser control closed or expired. Return to dashboard for a new ticket.";
    notice.textContent = "Remote control is locked. Your browser profile remains on the worker.";
  });
}

document.getElementById("lockBrowser").addEventListener("click", async () => {
  try { await api("/api/lock", "POST"); } catch {}
  if (rfb) rfb.disconnect();
  location.assign("/admin");
});
document.getElementById("verifyBrowser").addEventListener("click", async () => {
  try {
    const result = await api("/api/verify", "POST");
    notice.textContent = result.status === "CONNECTED"
      ? "✓ YouTube signed-in interface verified. Press Lock & return."
      : "Session unverified. Complete login inside the browser and try again.";
  } catch {
    notice.textContent = "Verification unavailable. Try again or return to dashboard.";
  }
});
boot().catch(() => { banner.textContent = "Browser authorization failed. Return to dashboard."; });
