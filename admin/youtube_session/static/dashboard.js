const $ = (selector) => document.querySelector(selector);
let csrf = "";
const login = $("#loginPanel"), board = $("#dashboard"), error = $("#loginError");
const show = (message) => { $("#statusMessage").textContent = message; };

async function api(path, method = "GET", data) {
  const opts = { method, credentials:"same-origin", headers:{} };
  if (data !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(data); }
  if (method !== "GET") opts.headers["X-CSRF-Token"] = csrf;
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.text()).slice(0,120));
  return res.json();
}

async function refresh() {
  let status, events;
  try {
    [status, events] = await Promise.all([api("/api/status"), api("/api/events")]);
  } catch (e) {
    show("Status tidak tersedia. Sila login semula atau semak rangkaian.");
    return;
  }
  const s = status.status || "WORKER_OFFLINE";
  const pretty = {
    CONNECTED:"Disambungkan", DISCONNECTED:"Belum disambungkan", UNVERIFIED:"Perlu pengesahan",
    RECONNECT_REQUIRED:"Login semula diperlukan", WORKER_OFFLINE:"Worker belum aktif",
    WORKER_NOT_CONFIGURED:"Browser belum tersedia",
  };
  const ready = status.worker === "RUNNING" && status.can_open_browser !== false;
  const blocked = !ready;
  const reason = status.blocker === "CHROMIUM_SANDBOX_UNAVAILABLE"
    ? "Hosting Musix belum menyokong Chromium bersandbox. Login Google dan simpan cookies belum diaktifkan."
    : "Browser worker belum dapat dihubungi. Sila semak status konfigurasi worker.";
  $("#workerExplanation").textContent = blocked ? reason : "Browser worker bersambung. Sahkan sesi Google sebelum digunakan.";
  $("#statusMessage").textContent = blocked
    ? "Belum siap: " + reason + " Website dan bot Telegram tetap berfungsi."
    : "Worker telah bersambung. Buka browser untuk login Google melalui halaman sebenar.";
  $("#controlStatus").textContent = blocked ? "Belum aktif" : "Sedia";
  $("#googleLoginStatus").textContent = blocked ? "Belum tersedia" : "Perlu pengesahan";
  $("#cookieStorageStatus").textContent = status.profile_present ? "Profil wujud" : "Belum tersedia";
  $("#accountStatus").textContent = pretty[s] || s;
  $("#sessionPill").textContent = pretty[s] || s;
  $("#accountDesc").textContent = s === "CONNECTED" ? "Sesi YouTube telah disahkan" : "Sesi Google belum disahkan";
  $("#accountName").textContent = s === "CONNECTED" ? "Sesi Google disahkan" : "Akaun Google belum disambungkan";
  $("#workerStatus").textContent = status.worker === "RUNNING" ? "Berjalan" : "Belum aktif";
  $("#health").textContent = status.worker === "RUNNING" ? "Bersambung" : "Belum tersedia";
  $("#profileStatus").textContent = status.profile_present ? "Profil wujud" : "Tiada profil";
  $("#lastChecked").textContent = status.last_checked ? new Date(status.last_checked * 1000).toLocaleString() : "Belum pernah";
  $("#verify").disabled = blocked;
  $("#connect").disabled = blocked;
  $("#revoke").disabled = blocked;
  $("#connect").title = blocked ? "Browser worker belum selamat untuk digunakan" : "Buka browser";
  $("#activityList").replaceChildren();
  for (const event of events.events || []) {
    const row = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = event.action + " · " + event.outcome;
    const time = document.createElement("small");
    time.textContent = new Date(event.time * 1000).toLocaleString();
    row.append(title, time);
    $("#activityList").append(row);
  }
  if (!(events.events || []).length) $("#activityList").textContent = "Belum ada aktiviti";
}

async function init() {
  try {
    const identity = await api("/api/me");
    csrf = identity.csrf;
    login.hidden = true;
    board.hidden = false;
    $("#logout").hidden = false;
    await refresh();
  } catch {
    login.hidden = false;
    board.hidden = true;
    $("#logout").hidden = true;
  }
}

$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  try {
    const response = await fetch("/login", {
      method:"POST",
      credentials:"same-origin",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        code:$("#loginCode").value,
      }),
    });
    if (!response.ok) throw new Error("Kod tidak sah atau sudah tamat tempoh. Hantar /sessionlogin semula.");
    const json = await response.json();
    csrf = json.csrf;
    $("#loginCode").value = "";
    await init();
  } catch(e) { error.textContent = e.message; }
});

$("#connect").addEventListener("click", async () => {
  try {
    show("Creating one-time remote browser ticket...");
    const ticket = await api("/api/browser-ticket", "POST", {});
    // Same-origin browser control page; no Google password ever enters dashboard.
    // Keep control tickets out of the browser address bar and normal HTTP access logs.
    sessionStorage.setItem("musix_browser_ticket", ticket.ticket);
    location.assign(ticket.url);
  } catch(e) { show("Gagal buka browser: " + e.message); }
});
$("#verify").addEventListener("click", async () => {
  try {
    show("Checking actual YouTube signed-in UI...");
    const result = await api("/api/verify", "POST", {});
    show(result.status === "CONNECTED" ? "Session verified. Profile remains on worker's vault." : "Not verified. Check the real Google sign-in page.");
    await refresh();
  } catch(e) { show("Verification failed: " + e.message); }
});
$("#lock").addEventListener("click", async () => {
  try { await api("/api/lock", "POST", {}); show("Remote control locked."); }
  catch(e) { show(e.message); }
});
$("#revoke").addEventListener("click", async () => {
  if (!confirm("Padam SEMUA data browser profile YouTube worker? Ini tak boleh diundo.")) return;
  const input = prompt("Taip REVOKE untuk sahkan pemadaman profile:");
  if (input !== "REVOKE") return;
  try {
    const result = await api("/api/revoke", "POST", {confirmation:"REVOKE"});
    show(result.status === "DISCONNECTED" ? "Worker profile revoked. Google cookies locally deleted." : "Revoke not confirmed: worker may be unavailable.");
    await refresh();
  } catch(e) { show("Revoke failed: " + e.message); }
});
$("#refresh").addEventListener("click", refresh);
$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", "POST", {}); } finally { location.assign("/admin/youtube-session"); }
});
init();
