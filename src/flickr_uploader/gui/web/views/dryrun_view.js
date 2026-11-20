// The dry run: what "Hochladen starten" would do now, per album and file.
// Nothing is sent to Flickr and nothing changes; it only reads the database.

import { count, el, icon } from "./ui.js";

const ACTIONS = {
  upload: ["wird hochgeladen", "go"],
  check: ["wird vorher abgeglichen, dann hochgeladen", "go"],
  compress: ["wird komprimiert hochgeladen", "go"],
  split: ["wird in Teilen hochgeladen", "go"],
  look_up: ["wird erst auf Flickr gesucht (Upload ohne Antwort)", "go"],
  take_over: ["altes Flickr-Foto wird übernommen", "ok"],
  done: ["schon auf Flickr", "ok"],
  wait_match: ["wartet: vielleicht schon auf Flickr, bitte entscheiden", "wait"],
  wait_album: ["wartet: das Album braucht eine Entscheidung", "wait"],
  wait_video: ["wartet: Komprimieren oder Aufteilen wählen", "wait"],
  no_album: ["liegt in keinem Album-Ordner", "wait"],
  skipped: ["übersprungen", "skip"],
  failed: ["wird nicht erneut versucht", "skip"],
};

const ALBUM_STATES = {
  exists: "Album ist auf Flickr",
  create: "Album wird neu angelegt",
  waits: "Album wartet auf deine Entscheidung",
};

export function renderDryRun(container, result, close) {
  const totals = result.totals;
  const waiting = ["wait_match", "wait_album", "wait_video", "no_album"].reduce((sum, key) => sum + (totals[key] || 0), 0);
  const summary = [
    `${count(totals.uploads, "Upload", "Uploads")} zu Flickr`,
    totals.new_albums ? count(totals.new_albums, "neues Album", "neue Alben") : null,
    totals.done ? `${totals.done} schon oben` : null,
    totals.take_over ? `${totals.take_over} übernommen` : null,
    waiting ? `${waiting} warten` : null,
    totals.skipped ? `${totals.skipped} übersprungen` : null,
  ].filter(Boolean);
  const groups = result.albums.map((album) => group(album.title, ALBUM_STATES[album.state], album.files));
  if (result.outside.length) groups.push(group("Ohne Album-Ordner", "wird nicht hochgeladen", result.outside));
  container.replaceChildren(
    el(
      "div",
      { class: "dialog wide", role: "dialog", "aria-labelledby": "dryrun-title" },
      el("h3", { id: "dryrun-title" }, "Probelauf"),
      el("p", { class: "muted", text: "So würde „Hochladen starten“ jetzt laufen. Es wurde nichts hochgeladen und nichts geändert." }),
      el("div", { class: "note" }, icon("info"), el("span", { text: summary.join(" · ") })),
      el("div", { class: "dryrun-list" }, groups.length ? groups : el("p", { class: "muted", text: "Es ist nichts angehakt." })),
      el("div", { class: "actions" }, el("button", { class: "btn btn-primary", onclick: close }, "Schließen")),
    ),
  );
}

function group(title, state, files) {
  const uploads = files.reduce((sum, file) => sum + file.uploads, 0);
  return el(
    "details",
    { class: "dryrun-group", open: files.length <= 30 },
    el("summary", {}, el("strong", { text: title }), el("span", { class: "muted small", text: ` · ${state} · ${count(files.length, "Datei", "Dateien")}, ${count(uploads, "Upload", "Uploads")}` })),
    el(
      "ul",
      { class: "dryrun-files" },
      files.map((file) => {
        const [text, kind] = ACTIONS[file.action] || [file.action, "skip"];
        return el(
          "li",
          { class: `dryrun-${kind}`, title: file.path },
          el("span", { class: "dryrun-name", text: file.name }),
          el("span", { text: file.detail ? `${text} (${file.detail})` : text }),
        );
      }),
    ),
  );
}
