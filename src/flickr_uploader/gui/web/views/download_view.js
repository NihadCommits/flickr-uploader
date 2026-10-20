// The download tab: pick albums (each with its own filter: videos, photos or
// everything), pick a target folder, start; then progress, pause and log like
// the upload. The picked albums are form input on the page until the download
// starts; the target folder (a setting) and the run come from the database.

import { jobLog, runningRow } from "./progress_view.js";
import { MEDIA_LABELS, TARGET_PROBLEMS } from "./settings_view.js";
import { count, el, formatTime, icon } from "./ui.js";

export function expectedFiles(album, media) {
  if (media === "photos") return album.photo_count || 0;
  if (media === "videos") return album.video_count || 0;
  return (album.photo_count || 0) + (album.video_count || 0);
}

export function renderDownloadCard(container, job, albums, prefs, view, act) {
  const run = job.run && job.run.kind === "download" ? job.run : null;
  const otherRunning = job.running && !run;
  let parts;
  if (job.running && run) {
    parts = [runningRow(run, job, act.pauseJob, "Wird heruntergeladen", "Wird vorbereitet … (Alben werden gelesen)")];
  } else {
    const picked = [...view.download.selected.entries()];
    const byId = new Map(albums.map((album) => [album.id, album]));
    const files = picked.reduce((sum, [id, media]) => sum + (byId.has(id) ? expectedFiles(byId.get(id), media) : 0), 0);
    const target = prefs.download_target;
    const problem = prefs.download_target_problem;
    const ready = picked.length > 0 && Boolean(target) && !problem && !otherRunning;
    const lastWasPaused = run && run.status === "aborted";
    parts = [
      el(
        "div",
        { class: "upload-row" },
        el("span", { class: "upload-icon" }, icon("download", 18)),
        el(
          "div",
          { class: "grow" },
          el("div", { class: "upload-title", text: picked.length ? `${count(picked.length, "Album", "Alben")} ausgewählt · etwa ${count(files, "Datei", "Dateien")}` : "Wähle unten aus, welche Alben du herunterladen möchtest" }),
          el("div", { class: "muted small", text: target ? `Ziel: ${target} (jedes Album in einem eigenen Ordner)` : "Noch kein Zielordner gewählt" }),
          problem ? el("div", { class: "small warn-text", text: TARGET_PROBLEMS[problem] }) : null,
          otherRunning ? el("div", { class: "muted small", text: "Gerade läuft ein Upload. Der Download startet, wenn er fertig ist." }) : null,
          job.report && run ? el("div", { class: "small report", text: reportText(job.report) }) : run ? el("div", { class: "muted small", text: `Letzter Download: ${formatTime(run.finished_at || run.started_at)}` }) : null,
        ),
        target && !problem && job.report && run ? el("button", { class: "btn btn-ghost", onclick: act.openTarget }, "Im Explorer zeigen") : null,
        el("button", { class: "btn", onclick: act.chooseTarget }, "Zielordner wählen …"),
        el(
          "button",
          { class: "btn btn-primary", disabled: !ready, onclick: act.startDownload },
          icon("download", 14),
          lastWasPaused && picked.length ? "Fortsetzen" : "Herunterladen starten",
        ),
      ),
    ];
  }
  container.replaceChildren(...parts, jobLog(job, run, view));
}

export function renderDownloadAlbums(container, flickr, view, act) {
  const albums = flickr.albums;
  if (!albums.length) {
    const text = flickr.synced_at
      ? ["Auf Flickr gibt es noch keine Alben."]
      : ["Die Albenliste wurde noch nicht von Flickr geholt.", el("br"), "Das passiert beim Start automatisch, oder im Reiter „Alben“ über „Aktualisieren“."];
    container.replaceChildren(el("div", { class: "card" }, el("div", { class: "empty" }, ...text)));
    return;
  }
  const query = view.download.search.trim().toLocaleLowerCase("de-DE");
  const shown = query ? albums.filter((album) => album.title.toLocaleLowerCase("de-DE").includes(query)) : albums;
  const list = el("ul", { class: "list" }, shown.length ? shown.map((album) => albumRow(album, view, act)) : el("li", {}, el("span", { class: "muted", text: "Kein Album gefunden." })));
  const search = el("input", {
    type: "search",
    class: "search",
    placeholder: "Album suchen …",
    value: view.download.search,
    "aria-label": "Album suchen",
    oninput: (event) => {
      view.download.search = event.target.value;
      act.renderAlbums({ keepFocus: true });
    },
  });
  container.replaceChildren(
    el(
      "div",
      { class: "card" },
      el(
        "div",
        { class: "card-head" },
        "Deine Alben auf Flickr",
        el("span", { class: "muted", text: `(${albums.length})` }),
        el("span", { class: "spacer" }),
        el("span", { class: "muted small", text: flickr.synced_at ? `Stand: ${formatTime(flickr.synced_at)}` : "" }),
        view.download.selected.size ? el("button", { class: "btn btn-ghost btn-sm", text: "Auswahl leeren", onclick: act.clearSelection }) : null,
      ),
      search,
      list,
    ),
  );
}

function albumRow(album, view, act) {
  const media = view.download.selected.get(album.id);
  const picked = media !== undefined;
  return el(
    "li",
    { class: `pick${picked ? " picked" : ""}`, onclick: (event) => !event.target.closest("button, input") && act.toggle(album.id) },
    el("input", { type: "checkbox", checked: picked, "aria-label": `${album.title} herunterladen`, onchange: () => act.toggle(album.id) }),
    el(
      "div",
      { class: "grow" },
      el("div", { class: "title", text: album.title }),
      el("div", { class: "sub", text: `${count(album.photo_count, "Foto", "Fotos")} · ${count(album.video_count, "Video", "Videos")}` }),
    ),
    picked
      ? el(
          "div",
          { class: "seg", role: "radiogroup", "aria-label": "Was herunterladen?" },
          MEDIA_LABELS.map(([value, label]) =>
            el(
              "button",
              {
                class: value === media ? "active" : "",
                role: "radio",
                "aria-checked": String(value === media),
                onclick: () => act.setMedia(album.id, value),
              },
              `${label} · ${expectedFiles(album, value)}`,
            ),
          ),
        )
      : null,
  );
}

function reportText(report) {
  const parts = [];
  if (report.downloaded) parts.push(`${report.downloaded} heruntergeladen`);
  if (report.existing) parts.push(`${report.existing} waren schon da`);
  if (report.not_original) parts.push(`${report.not_original} ohne Original (größte Fassung)`);
  if (report.undated) parts.push(`${report.undated} ohne Aufnahmedatum auf Flickr`);
  if (report.failed) parts.push(`${report.failed} Fehler`);
  const states = { completed: "Fertig", aborted: "Angehalten", crashed: "Mit Fehler beendet" };
  return `${states[report.status] || "Download"}: ${parts.join(", ") || "nichts zu tun"}`;
}
