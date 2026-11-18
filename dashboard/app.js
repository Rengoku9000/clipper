"use strict";

const icons = {
  scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="m8.5 8.5 11 11m-11-4 11-11M13 11l2 2"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16m10-16v16M3 9h4m-4 6h4m10-6h4m-4 6h4"/>',
  send: '<path d="m22 2-7 20-4-9-9-4 20-7ZM22 2 11 13"/>',
  link: '<path d="m10 13 4-4m-5 6-2 2a3 3 0 0 1-4-4l4-4a3 3 0 0 1 4 0m4 0 2-2a3 3 0 0 1 4 4l-4 4a3 3 0 0 1-4 0"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  radio: '<circle cx="12" cy="12" r="2"/><path d="M7 7a7 7 0 0 0 0 10m10 0a7 7 0 0 0 0-10M4 4a11 11 0 0 0 0 16m16 0a11 11 0 0 0 0-16"/>',
  play: '<path d="m9 5 11 7-11 7V5Z"/>',
  bolt: '<path d="m13 2-9 12h7l-1 8 10-12h-7l1-8Z"/>',
  timer: '<circle cx="12" cy="13" r="8"/><path d="M12 9v5l3 2M9 2h6"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  youtube: '<rect x="2" y="5" width="20" height="14" rx="4"/><path d="m10 9 5 3-5 3V9Z"/>',
  instagram: '<rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><path d="M17.5 6.5h.01"/>',
  facebook: '<path d="M15 21v-8h3l.5-4H15V7c0-1 .5-2 2-2h2V2h-3c-3 0-5 2-5 5v2H8v4h3v8"/>',
};
const labels = {
  overview: "Overview", workflow: "Workflow", library: "Clip library",
  publishing: "Publishing", accounts: "Accounts",
};
const platforms = {
  youtube: { name: "YouTube", type: "Shorts", provider: "youtube", description: "Connect a YouTube channel to upload vertical videos." },
  instagram: { name: "Instagram", type: "Reels", provider: "meta", description: "Connect a professional account linked to a Facebook Page." },
  facebook: { name: "Facebook", type: "Reels", provider: "meta", description: "Connect a Facebook Page you can publish to." },
};
const events = {
  started: "Capture started", marked: "Highlight marked", rendering: "Rendering clip",
  ready: "Clip ready", failed: "Render failed", stopped: "Capture stopped", capture_failed: "Capture failed",
};
const number = new Intl.NumberFormat();
const dateFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const dayFormat = new Intl.DateTimeFormat(undefined, { weekday: "short" });
const view = document.querySelector("#view");
const dialog = document.querySelector("#dialog");
let state;
let stamp = "";
let refreshing = false;
let toastTimer;
let chartDays = 7;
let term = "";
let clipFilter = "all";

function escape(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}

function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || ""}</svg>`;
}

function time(value) {
  return dateFormat.format(new Date(value * 1000));
}

function seconds(value) {
  return value == null ? "—" : `${Math.round(value * 10) / 10}s`;
}

function bytes(value) {
  return value < 1048576 ? `${Math.round(value / 1024)} KB` : `${(value / 1048576).toFixed(1)} MB`;
}

function chip(text, tone = "") {
  return `<span class="pill ${tone}">${escape(text)}</span>`;
}

function heading(title, description, action = "") {
  return `<div class="page-heading"><div><h1>${title}</h1><p>${description}</p></div>${action}</div>`;
}

function card(label, value, note, symbol) {
  return `<div class="panel stat"><div class="stat-top">${label}<span class="stat-icon">${icon(symbol)}</span></div><div class="stat-value">${value}</div><small>${note}</small></div>`;
}

function statRow(items) {
  return `<section class="stats" aria-label="Statistics">${items.map(item => card(...item)).join("")}</section>`;
}

function panel(title, description, html, action = "") {
  return `<section class="panel"><div class="panel-head"><div><h2>${title}</h2>${description ? `<p>${description}</p>` : ""}</div>${action}</div>${html}</section>`;
}

function chart() {
  const today = new Date();
  const bins = Array.from({ length: chartDays }, (_, i) => {
    const date = new Date(today.getFullYear(), today.getMonth(), today.getDate() - chartDays + i + 1);
    const tomorrow = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1);
    const count = state.clips.filter(clip => clip.time * 1000 >= date && clip.time * 1000 < tomorrow).length;
    return { date, count };
  });
  const yMax = Math.max(1, ...bins.map(bin => bin.count));
  const bars = bins.map(({ date, count }, i) => {
    const day = chartDays === 7 ? dayFormat.format(date) : date.getDate();
    const label = `${date.toLocaleDateString()}: ${count} clips`;
    return `<div class="chart-col" title="${escape(label)}" aria-label="${escape(label)}" tabindex="0">
      <div class="bar ${count ? "" : "zero"}" style="height:${count / yMax * 100}%"></div><small>${i === bins.length - 1 ? "Today" : day}</small></div>`;
  }).join("");
  return panel("Clip production", "Completed exports by day", `<div class="chart"><div class="chart-grid"></div>${bars}</div>
    <div class="chart-legend">Clips exported</div>`,
  `<select class="chart-period" id="period" aria-label="Chart range">
    <option value="7"${chartDays === 7 ? " selected" : ""}>Last 7 days</option>
    <option value="14"${chartDays === 14 ? " selected" : ""}>Last 14 days</option></select>`);
}

function workflowSnapshot() {
  const rendering = state.sessions.reduce((total, session) => total + session.rendering, 0);
  const values = [
    ["radio", "Capture", "Streams being monitored", state.sessions.length],
    ["bolt", "Detect", "Highlights registered", state.counts.marked || 0],
    ["film", "Render", rendering ? `${rendering} currently rendering` : "Vertical clips in the library", state.clips.length],
    ["send", "Publish", "Accepted by platform", state.posts.filter(post => post.status === "published").length],
  ];
  const rows = values.map(([symbol, label, note, value]) => `<div class="stage"><span class="stage-icon">${icon(symbol)}</span>
    <div><h3>${label}</h3><small>${note}</small></div><b>${value}</b></div>`).join("");
  return panel("Workflow stages", "From incoming footage to publication", `<div class="stage-list">${rows}</div>`,
    `<a class="text-link" href="#workflow">View workflow ${icon("arrow")}</a>`);
}

function clipName(clip) {
  return clip.channel === "Local" ? clip.name.replace(/\.mp4$/i, "").replaceAll("-", " ") : clip.channel;
}

function postLabel(status) {
  return ({
    published: ["Accepted", "good"], uploaded: ["Uploaded", "good"], working: ["Working", "purple"],
    check: ["Check platform", "warn"], failed: ["Failed", "bad"],
  })[status] || [status, ""];
}

function clipTable(clips) {
  if (!clips.length) return '<div class="empty">No clips match this view.</div>';
  const rows = clips.map(clip => {
    const id = encodeURIComponent(clip.id);
    const successes = state.posts.filter(post => post.clip === clip.id && ["published", "uploaded"].includes(post.status)).length;
    const status = successes ? chip(`${successes} destination${successes === 1 ? "" : "s"}`, "good") : chip("Ready", "purple");
    return `<tr><td><div class="clip-cell"><img src="/thumbnail?id=${id}" alt="" loading="lazy"><div><strong>${escape(clipName(clip))}</strong><small>${time(clip.time)}</small></div></div></td>
      <td class="mono">${seconds(clip.duration)}</td><td class="mono">${clip.width} × ${clip.height}</td>
      <td>${clip.cues ? chip(`${clip.cues} cues`, "good") : chip("None")}</td><td>${status}</td>
      <td><button class="secondary" data-preview="${escape(clip.id)}">${icon("play")} Preview</button></td></tr>`;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr><th>Clip</th><th>Duration</th><th>Resolution</th><th>Captions</th><th>Publishing</th><th><span class="muted">Action</span></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function overview() {
  const ready = state.clips.length;
  const connected = Object.values(state.accounts.destinations).reduce((sum, items) => sum + items.length, 0);
  return heading("Overview", "Follow your clips from live capture to publication.") + statRow([
    ["Active captures", number.format(state.sessions.length), "Running on this computer", "radio"],
    ["Clips exported", number.format(ready), `${state.clips.filter(clip => clip.cues > 0).length} with captions`, "film"],
    ["Avg. time to clip", seconds(state.average_latency), "From highlight mark to export", "timer"],
    ["Connected accounts", number.format(connected), "YouTube, Instagram and Facebook", "link"],
  ]) + `<div class="overview-grid">${chart()}${workflowSnapshot()}</div>` +
    panel("Recent clips", "", clipTable(state.clips.slice(0, 5)),
      `<a class="text-link" href="#library">Open library ${icon("arrow")}</a>`);
}

function captureCard(session) {
  return `<section class="panel capture-card"><div class="capture-top"><h3>${icon("radio")} ${escape(session.channel)}
    ${chip(session.status, session.status === "Capturing" ? "good" : "warn")}${session.rehearsal ? chip("Rehearsal") : ""}</h3>
    <div class="controls">
      <button class="primary" data-mark="${escape(session.channel)}"${session.ready ? "" : " disabled"}>${icon("bolt")} Mark highlight</button>
      <button class="secondary danger" data-stop="${escape(session.channel)}"${session.status === "Stopping" ? " disabled" : ""}>Stop</button>
    </div></div><div class="capture-grid">
      <div>Rolling buffer<b>${seconds(session.buffer)}</b></div><div>Footage captured<b>${seconds(session.captured)}</b></div>
      <div>Waiting for post-roll<b>${session.waiting}</b></div><div>Rendering<b>${session.rendering}</b></div>
    </div><p>${session.highlights} highlights · Chat detection ${session.chat ? "on" : "off"}</p>
    ${session.logs.length ? `<details data-log="${escape(session.channel)}"><summary>Capture log</summary><pre>${escape(session.logs.join("\n"))}</pre></details>` : ""}</section>`;
}

function chatTable() {
  if (!state.chat.length) return '<div class="empty">No chat windows recorded. Enable chat detection when starting a Twitch capture.</div>';
  const rows = state.chat.slice(0, 20).map(item => `<tr><td>${escape(item.channel)}</td><td>${time(item.time)}</td>
    <td class="mono">${item.data.messages}</td><td class="mono">${item.data.users}</td><td>${item.data.spike ? chip("Spike", "purple") : chip("Baseline")}</td></tr>`).join("");
  return `<div class="table-wrap"><table><thead><tr><th>Channel</th><th>Window ended</th><th>Messages</th><th>Unique chatters</th><th>Signal</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function workflow() {
  const captures = state.sessions.map(captureCard).join("") ||
    '<div class="panel empty">No active captures. Start a Twitch session to see its buffer and render queue here.</div>';
  const activity = state.activity.map(item => {
    const info = [item.channel];
    if (item.data.trigger) info.push(item.data.trigger === "spike" ? "Chat spike" : "Manual mark");
    if (item.data.latency != null) info.push(`${seconds(item.data.latency)} to export`);
    if (item.data.message) info.push(item.data.message);
    return `<div class="event"><b>${escape(events[item.kind] || item.kind)}</b><p>${escape(info.join(" · "))}</p><span>${time(item.time)}</span></div>`;
  }).join("");
  const count = key => state.counts[key] || 0;
  return heading("Workflow", "Monitor captures, chat spikes and renders in real time.") + statRow([
    ["Capture sessions", count("started"), "Started from this dashboard", "radio"],
    ["Highlights marked", count("marked"), "Manual and chat-triggered", "bolt"],
    ["Renders completed", count("ready"), "Recorded by this dashboard", "film"],
    ["Workflow failures", count("failed") + count("capture_failed"), "Capture and rendering errors", "activity"],
  ]) + `<div class="stack">${captures}${panel("Chat activity", "Latest 10-second windows", chatTable())}
    ${panel("Activity log", "Latest workflow events", activity ? `<div class="activity">${activity}</div>` : '<div class="empty">No workflow events yet.</div>')}</div>`;
}

function filteredClips() {
  return clipTable(state.clips.filter(clip => {
    const match = [clip.name, clip.channel, clip.id].some(value => value.toLowerCase().includes(term.toLowerCase()));
    return match && (clipFilter === "all" || Boolean(clip.cues) === (clipFilter === "captioned"));
  }));
}

function library() {
  const totalSeconds = state.clips.reduce((sum, clip) => sum + clip.duration, 0);
  const totalSize = state.clips.reduce((sum, clip) => sum + clip.size, 0);
  const filters = `<div class="filters"><input id="search" type="search" value="${escape(term)}" aria-label="Search clips" placeholder="Search clips">
    <select id="clip-filter" aria-label="Filter by captions"><option value="all">All clips</option>
      <option value="captioned"${clipFilter === "captioned" ? " selected" : ""}>Captioned</option>
      <option value="uncaptioned"${clipFilter === "uncaptioned" ? " selected" : ""}>Without captions</option></select></div>`;
  return heading("Clip library", "Preview and inspect the clips saved on this computer.", filters) +
    panel("Saved clips", `${state.clips.length} files · ${seconds(totalSeconds)} total footage · ${bytes(totalSize)}`,
      `<div id="clip-results">${filteredClips()}</div>`);
}

function safeLink(url) {
  try {
    const link = new URL(url);
    const allowed = ["www.youtube.com", "www.facebook.com", "www.instagram.com"];
    return link.protocol === "https:" && allowed.includes(link.hostname) ? link.href : "";
  } catch {
    return "";
  }
}

function publishing() {
  const rows = state.posts.map(post => {
    const href = safeLink(post.url);
    return `<tr><td>${escape(post.clip.split("/").at(-1))}</td><td>${escape(platforms[post.platform]?.name || post.platform)}</td>
      <td>${escape(post.name)}</td><td>${chip(...postLabel(post.status))}</td>
      <td><span title="${escape(post.remote || "")}">${escape(post.detail)}</span></td><td>${time(post.updated)}</td>
      <td>${href ? `<a class="text-link" href="${escape(href)}" target="_blank" rel="noopener">Open post ${icon("arrow")}</a>` : "—"}</td></tr>`;
  }).join("");
  const count = status => state.posts.filter(post => post.status === status).length;
  return heading("Publishing", "Track each upload separately by platform and destination.") + statRow([
    ["Accepted posts", count("published"), "Public requests accepted", "send"],
    ["Private / unlisted", count("uploaded"), "YouTube uploads", "film"],
    ["In progress", count("working"), "Queued, uploading or processing", "timer"],
    ["Needs attention", count("check") + count("failed"), "Failed or awaiting verification", "activity"],
  ]) + panel("Upload history", "", rows ? `<div class="table-wrap"><table><thead><tr><th>Clip</th><th>Platform</th>
    <th>Account</th><th>Status</th><th>Details</th><th>Updated</th><th>Post</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<div class="empty">No uploads yet. Open a clip preview to choose a publishing destination.</div>',
    '<a class="text-link" href="#library">Open clip library</a>');
}

function accounts() {
  const metaLinked = ["instagram", "facebook"].some(key => state.accounts.destinations[key]?.length);
  const cards = Object.entries(platforms).map(([key, entry]) => {
    const linked = state.accounts.destinations[key] || [];
    const configured = state.accounts.configured[entry.provider];
    const badge = linked.length ? chip("Connected", "good") : configured ? chip("Not connected") : chip("Setup needed", "warn");
    return `<section class="panel account-card"><div class="account-top"><span class="platform-icon ${key}">${icon(key)}</span>
        <div><h3>${entry.name}</h3><p>${entry.type}</p></div>${badge}</div>
      ${linked.length ? `<ul>${linked.map(account => `<li>${escape(account.name)}</li>`).join("")}</ul>` : `<p>${entry.description}</p>`}
      <div class="card-actions">
        <button class="${linked.length ? "secondary" : "primary"}" ${configured ? `data-connect="${entry.provider}"` : `data-setup="${entry.provider}"`}>${icon("link")} ${linked.length ? "Reconnect" : configured ? `Connect ${entry.name}` : entry.provider === "meta" ? "Set up Meta" : "Setup instructions"}</button>
        ${configured && entry.provider === "meta" && !metaLinked ?
          '<button class="secondary" data-setup="meta">Setup</button>' : ""}
        ${linked.length ? `<button class="secondary" data-disconnect="${key}">Disconnect</button>` : ""}
      </div></section>`;
  }).join("");
  return heading("Publishing accounts", "Authorize the accounts where your clips can be published.") +
    (state.accounts.notice ? `<div class="panel empty" style="margin-bottom:14px" role="status">${escape(state.accounts.notice)}</div>` : "") +
    `<div class="cards">${cards}</div><p style="margin-top:16px">Facebook Login can connect both Meta destinations. Connections stay active until the dashboard server restarts.</p>`;
}

function pageName() {
  return Object.hasOwn(labels, location.hash.slice(1)) ? location.hash.slice(1) : "overview";
}

function render() {
  const page = pageName();
  document.querySelector("#breadcrumb").textContent = labels[page];
  document.querySelectorAll("nav a").forEach(link => {
    if (link.dataset.page === page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (!state) return;
  const open = [...view.querySelectorAll("details[open]")].map(item => item.dataset.log);
  view.innerHTML = ({ overview, workflow, library, publishing, accounts })[page]();
  view.querySelectorAll("details[data-log]").forEach(item => { item.open = open.includes(item.dataset.log); });
  document.querySelector("#start-capture").disabled = false;
}

function notify(message, error = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 5500);
}

async function refresh(force = false) {
  if (refreshing) return;
  refreshing = true;
  const status = document.querySelector("#server-status");
  try {
    const response = await fetch("/api/overview", { cache: "no-store" });
    if (!response.ok) throw new Error("Dashboard unavailable");
    const fresh = await response.json();
    const signature = JSON.stringify(fresh);
    const preserveFocus = view.contains(document.activeElement) && document.activeElement !== document.body;
    if (force || !state || (signature !== stamp && !preserveFocus)) {
      state = fresh;
      stamp = signature;
      render();
    } else {
      state = fresh;
    }
    status.innerHTML = '<span class="led"></span>Engine online';
    status.classList.remove("offline");
  } catch {
    status.innerHTML = '<span class="led"></span>Engine offline';
    status.classList.add("offline");
  } finally {
    refreshing = false;
  }
}

async function action(route, data) {
  const response = await fetch(`/api/${route}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  return result;
}

function showDialog(title, body) {
  dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${escape(title)}</h2>
    <button class="secondary" aria-label="Close dialog" data-close>${icon("close")}</button></div>${body}`;
  if (!dialog.open) dialog.showModal();
}

function startDialog() {
  const presets = (state.clip_presets || []).map(preset =>
    `<option value="${escape(preset.id)}"${preset.default ? " selected" : ""}>
    ${escape(preset.label)} · ${preset.before + preset.after}s</option>`).join("");
  showDialog("Start capture", `<form id="capture-form"><label>Twitch channel<input name="channel" required
    pattern="[a-zA-Z0-9_]{1,25}" placeholder="Channel login" autocomplete="off" autofocus></label>
    ${state.top_caption_enabled ? '<label>Top caption<input name="top_caption" maxlength="56" placeholder="Defaults to the channel name"></label>' : ""}
    ${presets ? `<label>Clip length<select name="clip_preset">${presets}</select></label>` : ""}
    <label class="check"><input type="checkbox" name="chat"${state.chat_configured ? "" : " disabled"}>
      <span>Detect chat spikes${state.chat_configured ? "" : "<br><small>Twitch OAuth is required for automatic detection.</small>"}</span></label>
    ${state.rehearsal ? '<label class="check"><input type="checkbox" name="rehearsal"><span>Use the local rehearsal recording<br><small>Rehearsal clips are 8 seconds.</small></span></label>' : ""}
    <p class="dialog-error" role="alert" hidden></p><div class="dialog-foot"><button type="button" class="secondary" data-close>Cancel</button><button class="primary">Start capture</button></div></form>`);
}

function preview(id) {
  const clip = state.clips.find(item => item.id === id);
  if (!clip) return;
  const encoded = encodeURIComponent(id);
  showDialog(clipName(clip), `<video class="preview" controls playsinline preload="metadata" src="/clip?id=${encoded}"></video>
    <p class="preview-meta">${seconds(clip.duration)} · ${clip.width} × ${clip.height} · ${bytes(clip.size)} · ${clip.cues} caption cues</p>
    <div class="dialog-foot">${clip.cues ? `<a class="button secondary" href="/captions?id=${encoded}">Download captions</a>` : ""}
      <button class="primary" data-publish="${escape(id)}">${icon("send")} Publish clip</button></div>`);
}

function publishDialog(id) {
  const clip = state.clips.find(item => item.id === id);
  if (!clip) return;
  const options = Object.entries(platforms).flatMap(([key, platform]) =>
    (state.accounts.destinations[key] || []).map(account => ({
      value: `${key}:${account.id}`, label: `${platform.name} · ${account.name}`,
    })));
  if (!options.length) {
    showDialog("Connect a publishing account", '<p>Connect YouTube, Instagram or a Facebook Page to publish this clip.</p><div class="dialog-foot"><a class="button primary" href="#accounts" data-close>Go to accounts</a></div>');
    return;
  }
  showDialog("Publish clip", `<form id="publish-form" data-clip="${escape(id)}">
    <label>Destination<select name="destination">${options.map(item => `<option value="${escape(item.value)}">${escape(item.label)}</option>`).join("")}</select></label>
    <label>Title<input name="title" maxlength="100" required value="${escape(clipName(clip))}"></label>
    <label>Description<textarea name="description" maxlength="2000"></textarea></label>
    <label id="visibility">YouTube visibility<select name="visibility"><option value="private">Private</option>
      <option value="unlisted">Unlisted</option><option value="public">Public</option></select></label>
    <label id="audience">YouTube audience<select name="audience" required><option value="" selected disabled>Select audience</option>
      <option value="no">No, this video isn’t made for kids</option><option value="yes">Yes, this video is made for kids</option></select></label>
    <p id="meta-public" hidden>Publishing to Instagram or Facebook creates a public Reel.</p>
    <label class="check"><input name="confirmed" type="checkbox" required><span>I have permission to publish this clip and have reviewed the destination.</span></label>
    <p class="dialog-error" role="alert" hidden></p><div class="dialog-foot">
    <button type="button" data-preview="${escape(id)}" class="secondary">Back</button><button class="primary">Submit upload</button></div></form>`);
  setVisibility(options[0].value);
}

function setVisibility(destination) {
  const youtube = destination.startsWith("youtube:");
  const visibility = dialog.querySelector("#visibility");
  visibility.hidden = !youtube;
  visibility.style.display = youtube ? "" : "none";
  const audience = dialog.querySelector("#audience");
  audience.style.display = youtube ? "" : "none";
  audience.querySelector("select").disabled = !youtube;
  dialog.querySelector("#meta-public").hidden = youtube;
}

function setup(provider) {
  if (provider === "meta") {
    const app = state.accounts.meta_app || {};
    showDialog("Set up Meta", `<form id="meta-setup-form" autocomplete="off">
      <p>Use the App Secret from Meta for Developers → App settings → Basic. The secret is kept in memory for this dashboard session.</p>
      <label>Meta App ID<input name="identifier" inputmode="numeric" required value="${escape(app.identifier)}"></label>
      <label>Configuration ID<input name="config_id" inputmode="numeric" required value="${escape(app.config_id)}"></label>
      <label>App Secret<input name="secret" type="password" autocomplete="new-password" required></label>
      <p>OAuth callback <span class="mono">${escape(state.accounts.redirects.meta)}</span></p>
      <p class="dialog-error" role="alert" hidden></p>
      <div class="dialog-foot"><button type="button" class="secondary" data-close>Cancel</button><button class="primary">Save setup</button></div>
    </form>`);
    return;
  }
  showDialog("Set up YouTube", `<div class="setup">
    <p>Enable YouTube Data API v3 and create a Desktop OAuth client in Google Cloud.</p>
    <p>Set these environment variables before starting the dashboard.</p>
    <code>CLIPPER_GOOGLE_CLIENT_ID<br>CLIPPER_GOOGLE_CLIENT_SECRET</code>
    <p>OAuth callback</p><code>${escape(state.accounts.redirects.youtube)}</code></div>`);
}

document.querySelectorAll("[data-icon]").forEach(element => { element.innerHTML = icon(element.dataset.icon); });
document.querySelector(".skip").addEventListener("click", event => {
  event.preventDefault();
  document.querySelector("#main").focus();
});
document.querySelector("#start-capture").disabled = true;
document.querySelector("#start-capture").addEventListener("click", startDialog);
window.addEventListener("hashchange", () => { render(); document.querySelector("#main").focus(); });
document.addEventListener("click", async event => {
  const button = event.target.closest("button, [data-close]");
  if (!button || !state) return;
  const data = button.dataset;
  try {
    if (Object.hasOwn(data, "close")) dialog.close();
    if (data.preview) preview(data.preview);
    if (data.publish) publishDialog(data.publish);
    if (data.setup) setup(data.setup);
    if (data.connect) {
      button.disabled = true;
      const result = await action("connect", { provider: data.connect });
      location.assign(result.url);
    }
    if (data.disconnect) {
      await action("disconnect", { platform: data.disconnect });
      await refresh(true);
    }
    if (data.mark || data.stop) {
      const channel = data.mark || data.stop;
      await action(data.mark ? "mark" : "stop", { channel });
      notify(data.mark ? `Highlight marked for ${channel}.` : `Stopping ${channel}.`);
      await refresh(true);
    }
  } catch (error) {
    button.disabled = false;
    notify(error.message, true);
  }
});
document.addEventListener("input", event => {
  if (event.target.id === "search") {
    term = event.target.value;
    document.querySelector("#clip-results").innerHTML = filteredClips();
  }
});
document.addEventListener("change", event => {
  if (event.target.id === "period") {
    chartDays = Number(event.target.value);
    render();
    document.querySelector("#period").focus();
  }
  if (event.target.id === "clip-filter") {
    clipFilter = event.target.value;
    document.querySelector("#clip-results").innerHTML = filteredClips();
  }
  if (event.target.name === "destination") setVisibility(event.target.value);
});
dialog.addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target;
  const data = Object.fromEntries(new FormData(form));
  const button = event.submitter;
  const errorBox = form.querySelector(".dialog-error");
  errorBox.hidden = true;
  if (button) button.disabled = true;
  try {
    if (form.id === "capture-form") {
      await action("start", { channel: data.channel, chat: data.chat === "on",
        rehearsal: data.rehearsal === "on", top_caption: data.top_caption || "",
        clip_preset: data.clip_preset || "highlight" });
      location.hash = "workflow";
      notify(`Starting ${data.channel}.`);
    } else if (form.id === "meta-setup-form") {
      await action("configure-meta", data);
      form.reset();
      notify("Meta setup saved for this session. Connect Instagram to continue.");
    } else {
      const [platform, account] = data.destination.split(":");
      const made_for_kids = data.audience ? data.audience === "yes" : null;
      await action("publish", { ...data, platform, account, made_for_kids, clip: form.dataset.clip, confirmed: data.confirmed === "on" });
      location.hash = "publishing";
      notify("Upload queued.");
    }
    dialog.close();
    await refresh(true);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    if (button) button.disabled = false;
  }
});

render();
refresh(true);
setInterval(refresh, 2500);
