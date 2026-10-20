// Entry point of the frontend. The page keeps no data of its own: every view
// is rendered from what the Python API returns, and after every action the
// views are rendered again (spec, section "GUI"). `view` only remembers what
// is on screen: tab, source folder, selected row, opened branches, search.

import { renderDetails } from "./views/album_assignment.js";
import { renderAlbums } from "./views/album_overview.js";
import { renderDownloadAlbums, renderDownloadCard } from "./views/download_view.js";
import { renderTree } from "./views/folder_browser.js";
import { renderUpload } from "./views/progress_view.js";
import { renderSettings } from "./views/settings_view.js";
import { renderSteps } from "./views/steps.js";
import { el, formatTime, icon } from "./views/ui.js";

const view = {
  tab: "tab-upload",
  rootId: null,
  selectedId: null,
  open: new Set(),
  revealId: null,
  scrollToSelected: false,
  albumSearch: "",
  syncing: false,
  logOpen: false,
  // The download form: picked albums (id -> filter) and the search.
  download: { selected: new Map(), search: "" },
  accountForm: false, // the Flickr setup is open although a login exists
};
// Last answers of the API, kept only to render again without asking twice.
let downloadFlickr = { albums: [], synced_at: null };
let prefs = null;
let lastPlan = null;
let status = null;
let pollTimer = null;
let jobTimer = null;
let treeTimer = null;
let wasRunning = false;
let toastTimer = null;
let dialogAnswer = null;
const $ = (id) => document.getElementById(id);

// --- Talking to Python ---------------------------------------------------------

async function call(method, ...args) {
  try {
    const result = await window.pywebview.api[method](...args);
    if (result && result.error) {
      showDialog("Hinweis", result.error);
      return null;
    }
    return result ?? {};
  } catch (error) {
    showDialog("Etwas ist schiefgelaufen", String(error && error.message ? error.message : error));
    return null;
  }
}

async function busy(message, work) {
  document.body.classList.add("busy");
  showToast(message, { spinner: true, stay: true });
  for (const button of document.querySelectorAll(".toolbar button")) button.disabled = true;
  try {
    return await work();
  } finally {
    for (const button of document.querySelectorAll(".toolbar button")) button.disabled = false;
    document.body.classList.remove("busy");
    hideToast();
  }
}

function showToast(text, { spinner = false, stay = false } = {}) {
  clearTimeout(toastTimer);
  $("toast").replaceChildren(spinner ? el("span", { class: "spinner" }) : icon("check"), el("span", { text }));
  $("toast").hidden = false;
  if (!stay) toastTimer = setTimeout(hideToast, 4500);
}

function hideToast() {
  $("toast").hidden = true;
}

// Resolves to true for OK, false for "Abbrechen" (only shown with `cancel`).
function showDialog(title, text, { okLabel = "OK", cancel = false } = {}) {
  hideToast();
  if (dialogAnswer) dialogAnswer(false);
  $("dialog-title").textContent = title;
  $("dialog-text").textContent = text;
  $("dialog-ok").textContent = okLabel;
  $("dialog-cancel").hidden = !cancel;
  $("dialog").hidden = false;
  $("dialog-ok").focus();
  return new Promise((resolve) => (dialogAnswer = resolve));
}

function closeDialog(answer) {
  $("dialog").hidden = true;
  const resolve = dialogAnswer;
  dialogAnswer = null;
  if (resolve) resolve(answer);
}

// --- Rendering --------------------------------------------------------------------

async function renderStatus() {
  status = await call("status");
  if (!status) return;
  const login = $("login-chip");
  login.className = `chip ${status.logged_in ? "ok" : "warn"}`;
  login.replaceChildren(el("span", { class: `dot ${status.logged_in ? "green" : "yellow"}` }), status.logged_in ? "Angemeldet" : "Nicht angemeldet");
  login.title = status.message;
  $("footbar").textContent = `Datenbank: ${status.database}`;
}

async function renderOverview() {
  const [overview, plan, job] = await Promise.all([call("overview"), call("upload_plan"), call("job")]);
  if (!overview || !plan || !job) return;
  renderSteps($("steps"), overview, status, plan, job.running && job.run && job.run.kind === "upload");
  await renderJob(plan);
  const chip = $("sync-chip");
  chip.hidden = false;
  if (view.syncing) chip.replaceChildren(el("span", { class: "spinner" }), "Albenliste wird geholt");
  else if (overview.albums_synced_at) chip.replaceChildren(icon("sync", 12), `${overview.flickr_albums} Alben · ${formatTime(overview.albums_synced_at)}`);
  else chip.replaceChildren(icon("sync", 12), "Albenliste noch nicht geholt");
  chip.title = "Deine Alben auf Flickr: siehe Reiter „Alben“";
  const addRoot = $("add-root");
  addRoot.className = overview.roots ? "btn" : "btn btn-primary";
}

async function renderJob(plan) {
  clearTimeout(jobTimer);
  if (plan) lastPlan = plan;
  const job = await call("job");
  if (!job || !lastPlan) return;
  const kind = job.run ? job.run.kind : null;
  const card = $("upload-card");
  card.hidden = !lastPlan.files && !(job.running && kind === "upload") && kind !== "upload";
  renderUpload(card, lastPlan, job, view, { startUpload, pauseJob });
  if (prefs) renderDownloadCard($("download-card"), job, downloadFlickr.albums, prefs, view, downloadActions);
  if (job.running) {
    wasRunning = true;
    // Follow the run: progress every second, the tree every few seconds.
    jobTimer = setTimeout(async () => {
      await renderJob(kind === "upload" ? await call("upload_plan") : null);
    }, 1000);
    if (kind === "upload" && !treeTimer) treeTimer = setTimeout(() => ((treeTimer = null), renderFolders()), 4000);
  } else if (wasRunning) {
    wasRunning = false;
    if (job.report) {
      const what = kind === "download" ? "Download" : "Upload";
      showToast(job.report.status === "aborted" ? `${what} angehalten. „Fortsetzen“ macht dort weiter.` : `${what} fertig`);
    }
    await renderOverview();
    await renderFolders();
  }
}

async function renderRoots() {
  const roots = (await call("roots")) || [];
  const select = $("root-select");
  select.replaceChildren(
    ...roots.map((root) => el("option", { value: root.id, text: root.reachable ? root.path : `${root.path} (nicht erreichbar)` })),
  );
  if (!roots.some((root) => root.id === view.rootId)) view.rootId = roots.length ? roots[0].id : null;
  if (view.rootId !== null) select.value = String(view.rootId);
  select.hidden = roots.length === 0;
  $("rescan").hidden = roots.length === 0;
}

async function renderFolders() {
  clearTimeout(pollTimer);
  if (view.rootId === null) {
    $("tree").replaceChildren(
      el("div", { class: "empty" }, el("div", {}, icon("folder", 28)), "Noch kein Ordner geöffnet.", el("br"), "Klicke oben rechts auf „Ordner öffnen …“."),
    );
    $("tree-hint").textContent = "";
    renderDetails($("details"), null, [], {}, actions);
    return;
  }
  const data = await call("tree", view.rootId);
  if (!data) return;
  if (view.revealId !== null) revealInTree(data.folders);
  renderTree($("tree"), data.folders, view, { select, tick }, data.root.path);
  $("tree-hint").textContent = "Klicke einen Ordner an, um ihn zu bearbeiten";
  const selected = data.folders.find((folder) => folder.id === view.selectedId);
  const context = {};
  if (selected && selected.album) context.flickrAlbums = ((await call("flickr_albums")) || { albums: [] }).albums;
  if (selected && selected.included) context.matches = await call("matches", selected.id);
  renderDetails($("details"), selected, data.folders, context, actions);
  // A pre-check runs in the background: look again until it is done.
  if (data.folders.some((folder) => folder.precheck_running)) {
    pollTimer = setTimeout(async () => {
      await renderFolders();
      await renderOverview();
    }, 1500);
  }
}

function revealInTree(folders) {
  const byPath = new Map(folders.map((folder) => [folder.path, folder]));
  let folder = folders.find((candidate) => candidate.id === view.revealId);
  view.selectedId = view.revealId;
  view.revealId = null;
  view.scrollToSelected = true;
  while (folder && folder.parent !== null) {
    folder = byPath.get(folder.parent);
    if (folder) view.open.add(folder.id);
  }
}

async function renderAlbumTab() {
  const [folders, flickr] = await Promise.all([call("album_folders"), call("flickr_albums")]);
  if (!folders || !flickr) return;
  renderAlbums($("albums-view"), folders, flickr, view, { reveal, syncAlbums });
}

async function loadPrefs() {
  prefs = (await call("settings")) || prefs;
}

async function refreshAll() {
  await renderStatus();
  await loadPrefs();
  await renderRoots();
  await renderOverview();
  await renderFolders();
  if (view.tab === "tab-albums") await renderAlbumTab();
  if (view.tab === "tab-download") await renderDownloadTab();
  if (view.tab === "tab-settings") await renderSettingsTab();
}

async function afterChange(message) {
  if (message) showToast(message);
  await renderOverview();
  await renderFolders();
}

// --- Actions -----------------------------------------------------------------------

function select(folderId) {
  view.selectedId = folderId;
  view.open.add(folderId); // show what is inside, e.g. the tick boxes of an album folder
  renderFolders();
}

async function tick(folderId, included) {
  if (await call("set_included", folderId, included)) {
    await afterChange(included ? "Angehakt: wird hochgeladen" : "Häkchen entfernt");
  } else await renderFolders();
}

const actions = {
  select,
  async makeAlbum(folderId, title) {
    view.open.add(folderId);
    if (await call("make_album_folder", folderId, title)) await afterChange("Als Album festgelegt. Hake jetzt darin an, was hochgeladen wird.");
    else await renderFolders();
  },
  async removeAlbum(folderId) {
    if (await call("remove_album_folder", folderId)) await afterChange("Kein Album-Ordner mehr");
  },
  async renameAlbum(albumId, title) {
    if (await call("rename_planned_album", albumId, title)) await afterChange("Albumtitel geändert");
  },
  async confirmNew(albumId) {
    if (await call("confirm_new_album", albumId)) await afterChange("Es wird ein neues Album angelegt");
  },
  async chooseSame(fileId, flickrId) {
    if (await call("choose_same", fileId, flickrId)) await afterChange("Wird übernommen statt hochgeladen");
  },
  async chooseUpload(fileId) {
    if (await call("choose_upload", fileId)) await afterChange("Wird trotzdem hochgeladen");
  },
  async resetChoice(fileId) {
    if (await call("reset_choice", fileId)) await afterChange("Entscheidung zurückgenommen");
  },
  async decideFolder(folderId, choice) {
    const result = await call("decide_folder", folderId, choice);
    if (result) await afterChange(choice === "same" ? `${result.decided} übernommen` : `${result.decided} werden trotzdem hochgeladen`);
  },
  async openOnFlickr(photoId) {
    await call("open_on_flickr", photoId);
  },
  async useExisting(folderId, albumId) {
    if (albumId === undefined) {
      showDialog("Hinweis", "Bitte ein Album aus der Liste wählen (beim Tippen erscheinen die passenden).");
      return;
    }
    if (await call("use_existing_album", folderId, albumId)) await afterChange("Vorhandenes Album wird verwendet");
  },
};

async function startUpload() {
  if (await call("start_upload")) {
    wasRunning = true;
    showToast("Upload gestartet");
    await renderOverview();
  }
}

async function pauseJob() {
  if (await call("pause_job")) {
    showToast("Hält nach der aktuellen Datei an …");
    await renderJob(null);
  }
}

// --- Download --------------------------------------------------------------------

async function renderDownloadTab() {
  const flickr = await call("flickr_albums");
  if (!flickr) return;
  downloadFlickr = flickr;
  await loadPrefs();
  // Forget picks of albums that are gone from Flickr.
  for (const id of view.download.selected.keys()) {
    if (!flickr.albums.some((album) => album.id === id)) view.download.selected.delete(id);
  }
  renderDownloadAlbums($("download-albums"), flickr, view, downloadActions);
  await renderJob(null);
}

const downloadActions = {
  pauseJob,
  toggle(albumId) {
    if (view.download.selected.has(albumId)) view.download.selected.delete(albumId);
    else view.download.selected.set(albumId, prefs ? prefs.download_media : "all");
    downloadActions.renderAlbums();
  },
  setMedia(albumId, media) {
    view.download.selected.set(albumId, media);
    downloadActions.renderAlbums();
  },
  clearSelection() {
    view.download.selected.clear();
    downloadActions.renderAlbums();
  },
  renderAlbums({ keepFocus = false } = {}) {
    const search = document.activeElement && document.activeElement.type === "search" ? document.activeElement.selectionStart : null;
    renderDownloadAlbums($("download-albums"), downloadFlickr, view, downloadActions);
    if (keepFocus) {
      const input = $("download-albums").querySelector('input[type="search"]');
      input.focus();
      if (search !== null) input.setSelectionRange(search, search);
    }
    renderJob(null);
  },
  async chooseTarget() {
    if (await chooseTarget()) await renderJob(null);
  },
  async startDownload() {
    const choices = [...view.download.selected.entries()].map(([album_id, media]) => ({ album_id, media }));
    if (await call("start_download", choices)) {
      wasRunning = true;
      showToast("Download gestartet");
      await renderJob(null);
    }
  },
  async openTarget() {
    await call("open_folder", prefs.download_target);
  },
};

async function chooseTarget() {
  const result = await call("choose_download_target");
  if (!result || !result.path) return false;
  await loadPrefs();
  showToast("Zielordner gemerkt");
  return true;
}

// --- Settings ------------------------------------------------------------------------

async function renderSettingsTab() {
  const [values, info, account] = await Promise.all([call("settings"), call("info"), call("account")]);
  if (!values || !info || !account) return;
  prefs = values;
  renderSettings($("settings-view"), { settings: values, info, account, formOpen: view.accountForm }, settingsActions);
}

async function openSettings() {
  switchTab("tab-settings");
  await renderSettingsTab();
}

const settingsActions = {
  openForm() {
    view.accountForm = true;
    renderSettingsTab();
  },
  async closeForm() {
    await call("cancel_login");
    view.accountForm = false;
    await renderSettingsTab();
  },
  async openFlickrPage(page) {
    await call("open_flickr_page", page);
  },
  // The inputs are read here and emptied once Python has the values; on an
  // error they stay, so a typo can be fixed.
  async startLogin(keyInput, secretInput) {
    const result = await busy("Flickr prüft Key und Secret …", () => call("start_login", keyInput.value, secretInput.value));
    if (!result) return;
    keyInput.value = "";
    secretInput.value = "";
    showToast("Flickr ist im Browser geöffnet");
    await renderSettingsTab();
  },
  async reopenLoginPage() {
    await call("reopen_login_page");
  },
  async finishLogin(codeInput) {
    const result = await busy("Anmeldung wird abgeschlossen …", () => call("finish_login", codeInput.value));
    if (!result) return;
    view.accountForm = false;
    showToast(`Angemeldet als ${result.username}`);
    await renderStatus();
    await renderSettingsTab();
    syncAlbums({ quiet: true }); // a new login: read its albums, the traffic lights depend on them
  },
  async logout() {
    const sure = await showDialog(
      "Abmelden?",
      "Key, Secret und Anmeldung werden aus dem Windows-Anmeldeinformationsspeicher entfernt. Deine Ordner, Alben und Einstellungen in der App bleiben.",
      { okLabel: "Abmelden", cancel: true },
    );
    if (!sure || !(await call("logout"))) return;
    await renderStatus();
    await renderOverview();
    await renderSettingsTab();
    const revoke = await showDialog("Abgemeldet", "Möchtest du der App den Zugriff auch bei Flickr entziehen? Das geht auf der Flickr-Seite mit deinen erlaubten Apps.", {
      okLabel: "Flickr-Seite öffnen",
      cancel: true,
    });
    if (revoke) await call("open_flickr_page", "authorized_apps");
  },
  async setSetting(key, value) {
    if (await call("set_setting", key, value)) showToast("Gespeichert");
    await renderSettingsTab();
  },
  async chooseTarget() {
    await chooseTarget();
    await renderSettingsTab();
  },
  async openFolder(path) {
    await call("open_folder", path);
  },
  async backupNow() {
    const result = await busy("Backup wird angelegt …", () => call("backup_now"));
    if (result) showToast("Backup angelegt und geprüft");
    await renderSettingsTab();
  },
};

async function reveal(rootId, folderId) {
  view.rootId = rootId;
  view.revealId = folderId;
  switchTab("tab-upload");
  await renderRoots();
  await renderFolders();
}

async function syncAlbums({ quiet = false } = {}) {
  if (view.syncing) return;
  view.syncing = true;
  await renderOverview();
  if (view.tab === "tab-albums") await renderAlbumTab();
  let result = null;
  try {
    result = quiet ? await window.pywebview.api.refresh_albums().catch(() => null) : await call("refresh_albums");
  } finally {
    view.syncing = false;
  }
  if (result && !result.error) showToast(syncMessage(result));
  else if (result && result.error && quiet) showToast(`Albenliste nicht geholt: ${result.error}`);
  await renderOverview();
  await renderFolders();
  if (view.tab === "tab-albums") await renderAlbumTab();
  if (view.tab === "tab-download") await renderDownloadTab();
}

function syncMessage(result) {
  const parts = [];
  if (result.lights.green) parts.push(`${result.lights.green} vorhanden`);
  if (result.lights.blue) parts.push(`${result.lights.blue} neu`);
  if (result.lights.yellow) parts.push(`${result.lights.yellow} mit Entscheidung nötig`);
  const folders = parts.length ? ` Deine Album-Ordner: ${parts.join(", ")}.` : "";
  return `Albenliste aktualisiert: ${result.albums} Alben auf Flickr.${folders}`;
}

async function addRoot() {
  const result = await busy("Ordner wird eingelesen …", () => call("add_root"));
  if (result && !result.cancelled) {
    view.rootId = result.root_id;
    view.selectedId = null;
    await refreshAll();
    showToast(scanMessage(result.report));
  }
}

async function rescan() {
  const result = await busy("Ordner wird neu eingelesen …", () => call("scan", view.rootId));
  if (result) {
    await afterChange(scanMessage(result.report));
  }
}

function scanMessage(report) {
  const parts = [`${report.files} Dateien in ${report.folders} Ordnern`];
  if (report.new_files) parts.push(`${report.new_files} neu`);
  if (report.changed_files) parts.push(`${report.changed_files} geändert`);
  if (report.missing_files) parts.push(`${report.missing_files} verschwunden`);
  if (report.unreadable) parts.push(`${report.unreadable} Ordner nicht lesbar`);
  return `Eingelesen: ${parts.join(", ")}`;
}

// --- Start -------------------------------------------------------------------------

function switchTab(id) {
  view.tab = id;
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const active = tab.getAttribute("aria-controls") === id;
    tab.setAttribute("aria-selected", String(active));
    $(tab.getAttribute("aria-controls")).hidden = !active;
  }
}

function start() {
  $("brand-mark").append(icon("updown", 18));
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    tab.addEventListener("click", async () => {
      switchTab(tab.getAttribute("aria-controls"));
      if (view.tab === "tab-albums") await renderAlbumTab();
      if (view.tab === "tab-download") await renderDownloadTab();
      if (view.tab === "tab-settings") await renderSettingsTab();
    });
  }
  $("dialog-ok").addEventListener("click", () => closeDialog(true));
  $("dialog-cancel").addEventListener("click", () => closeDialog(false));
  $("login-chip").addEventListener("click", openSettings);
  $("add-root").addEventListener("click", addRoot);
  $("rescan").addEventListener("click", rescan);
  $("root-select").addEventListener("change", (event) => {
    view.rootId = Number(event.target.value);
    view.selectedId = null;
    renderFolders();
  });
  refreshAll().then(() => {
    // Keep the traffic lights right without anyone having to think of it.
    if (status && status.logged_in && prefs && prefs.sync_albums_at_start) syncAlbums({ quiet: true });
  });
}

if (window.pywebview && window.pywebview.api) start();
else window.addEventListener("pywebviewready", start, { once: true });
