// The upload card: what a run would do, the progress of a running one (with a
// pause button), the summary of the last one, and its log. The progress row
// and the log are shared with the download card.

import { count, el, formatTime, icon } from "./ui.js";

export function renderUpload(container, plan, job, view, act) {
  const run = job.run && job.run.kind === "upload" ? job.run : null;
  const parts =
    job.running && run
      ? [runningRow(run, job, act.pauseJob, "Wird hochgeladen", "Wird vorbereitet … (Albenliste und Abgleich)")]
      : idle(plan, run, job, act);
  container.replaceChildren(...parts, jobLog(job, run, view));
}

export function runningRow(run, job, onPause, doing, preparing) {
  const total = run.items_total;
  const done = run.items_done;
  const latest = job.events.length ? job.events[job.events.length - 1].message : "";
  return el(
    "div",
    { class: "upload-row" },
    el("span", { class: "upload-icon" }, el("span", { class: "spinner" })),
    el(
      "div",
      { class: "grow" },
      el("div", { class: "upload-title", text: total ? `${doing} … ${done} von ${total}` : preparing }),
      el("div", { class: `bar${total ? "" : " indeterminate"}` }, el("span", { style: total ? `width: ${Math.round((done / total) * 100)}%` : null })),
      el("div", { class: "muted small latest", text: latest }),
    ),
    job.pausing
      ? el("button", { class: "btn", disabled: true, text: "Hält nach der aktuellen Datei an …" })
      : el("button", { class: "btn", onclick: onPause }, icon("pause", 14), "Pausieren"),
  );
}

export function jobLog(job, run, view) {
  if (!run || !job.events.length) return el("span");
  const list = el(
    "ol",
    { class: "log" },
    job.events.map((event) =>
      el(
        "li",
        { class: `log-${event.level}` },
        el("span", { class: "log-time", text: new Date(event.ts).toLocaleTimeString("de-DE") }),
        el("span", { text: event.message }),
      ),
    ),
  );
  const details = el(
    "details",
    {
      class: "log-box",
      open: view.logOpen,
      ontoggle: (event) => {
        view.logOpen = event.target.open;
        if (view.logOpen) list.scrollTop = list.scrollHeight;
      },
    },
    el("summary", { text: `Protokoll (${formatTime(run.started_at)})` }),
    list,
  );
  queueMicrotask(() => (list.scrollTop = list.scrollHeight));
  return details;
}

function idle(plan, run, job, act) {
  const otherRunning = job.running && !run;
  const lastWasPaused = run && run.status === "aborted";
  const head = plan.ready
    ? `${count(plan.ready, "Datei", "Dateien")} bereit zum Hochladen`
    : plan.files
      ? "Gerade nichts bereit zum Hochladen"
      : "Noch keine Ordner angehakt";
  const details = [];
  if (plan.decide) details.push(`${plan.decide} warten auf deine Entscheidung`);
  if (plan.album) details.push(`${plan.album} in Alben, die eine Entscheidung brauchen`);
  if (plan.done) details.push(`${plan.done} schon auf Flickr`);
  if (plan.failed) details.push(`${plan.failed} mit Fehler`);
  if (plan.skipped) details.push(`${plan.skipped} übersprungen (RAW, zu groß, nicht unterstützt)`);
  if (plan.unchecked) details.push(`${plan.unchecked} werden vorher noch abgeglichen`);
  return [
    el(
      "div",
      { class: "upload-row" },
      el("span", { class: "upload-icon" }, icon("upload", 18)),
      el(
        "div",
        { class: "grow" },
        el("div", { class: "upload-title", text: head }),
        details.length ? el("div", { class: "muted small", text: details.join(" · ") }) : null,
        otherRunning ? el("div", { class: "muted small", text: "Gerade läuft ein Download. Hochladen geht, sobald er fertig ist." }) : null,
        job.report && run ? el("div", { class: "small report", text: reportText(job.report) }) : run ? el("div", { class: "muted small", text: runText(run) }) : null,
      ),
      el(
        "button",
        { class: "btn btn-primary", disabled: !plan.ready || otherRunning, onclick: act.startUpload },
        icon("upload", 14),
        lastWasPaused && plan.ready ? "Fortsetzen" : "Hochladen starten",
      ),
    ),
  ];
}

function runText(run) {
  const states = { completed: "abgeschlossen", aborted: "angehalten", crashed: "mit Fehler beendet", running: "läuft" };
  return `Letzter Lauf ${states[run.status] || run.status} (${formatTime(run.finished_at || run.started_at)})`;
}

function reportText(report) {
  const parts = [];
  if (report.uploaded) parts.push(`${report.uploaded} hochgeladen`);
  if (report.adopted) parts.push(`${report.adopted} übernommen`);
  if (report.already_done) parts.push(`${report.already_done} waren schon oben`);
  if (report.albums_created) parts.push(`${count(report.albums_created, "Album", "Alben")} angelegt`);
  if (report.waiting) parts.push(`${report.waiting} warten`);
  if (report.failed) parts.push(`${report.failed} Fehler`);
  if (report.unclear) parts.push(`${report.unclear} unklar (nächster Lauf prüft nach)`);
  const states = { completed: "Fertig", aborted: "Angehalten", crashed: "Mit Fehler beendet" };
  return `${states[report.status] || "Lauf"}: ${parts.join(", ") || "nichts zu tun"}`;
}
