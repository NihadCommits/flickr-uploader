// The details of the selected folder: make it an album folder, change the
// planned title, pick an album that is on Flickr under another name, settle a
// yellow light, or see which album the folder belongs to.

import { decisionsSection } from "./decisions.js";
import { LIGHT_LABELS, count, dot, el, formatBytes, icon } from "./ui.js";

export function renderDetails(container, folder, folders, context, act) {
  if (!folder) {
    container.replaceChildren(
      el("div", { class: "empty" }, el("div", {}, icon("folder", 28)), "Wähle links einen Ordner aus,", el("br"), "um ihn hier zu bearbeiten."),
    );
    return;
  }
  const sections = [
    el(
      "div",
      { class: "section" },
      el("h2", { class: "folder-title", text: folder.name }),
      el("p", { class: "path-line", text: folder.path || "Ausgangsordner" }),
    ),
    folder.album ? albumSection(folder, context, act) : folder.in_album ? memberSection(folder, folders, act) : assignSection(folder, act),
    folder.in_album ? uploadSection(folder) : null,
    folder.included ? decisionsSection(folder, context.matches, act) : null,
    contentSection(folder),
  ];
  container.replaceChildren(...sections.filter(Boolean));
}

function contentSection(folder) {
  return el(
    "div",
    { class: "section" },
    el("p", { class: "section-title", text: "Inhalt" }),
    el(
      "ul",
      { class: "facts" },
      fact("Direkt im Ordner", `${count(folder.files, "Datei", "Dateien")} · ${formatBytes(folder.bytes)}`),
      folder.skipped ? fact("Davon übersprungen", `${folder.skipped} (RAW, zu groß oder nicht unterstützt)`) : null,
      fact("Mit Unterordnern", `${count(folder.total_files, "Datei", "Dateien")} · ${formatBytes(folder.total_bytes)}`),
    ),
  );
}

function fact(label, value) {
  return el("li", {}, el("span", { class: "label", text: label }), el("span", { text: value }));
}

function assignSection(folder, act) {
  const input = el("input", { type: "text", value: folder.path ? folder.name : "", "aria-label": "Albumtitel" });
  return el(
    "div",
    { class: "section" },
    el("p", { class: "section-title", text: "Album" }),
    el("p", { class: "muted", text: "Soll dieser Ordner ein Album auf Flickr werden? Danach hakst du darin an, welche Ordner hochgeladen werden." }),
    el("label", { class: "field-label", text: "Albumtitel (vorgeschlagen: Ordnername)" }),
    el("div", { class: "field" }, input, el("button", { class: "btn btn-primary", text: "Als Album festlegen", onclick: () => act.makeAlbum(folder.id, input.value) })),
  );
}

function albumSection(folder, context, act) {
  const album = folder.album;
  const planned = album.state === "planned";
  const kind = { green: "ok", blue: "", yellow: "warn" }[album.light];
  const title = el("input", { type: "text", value: album.title, disabled: !planned, "aria-label": "Albumtitel" });
  return el(
    "div",
    { class: "section" },
    el("p", { class: "section-title", text: "Album" }),
    el("div", { class: `note ${kind}` }, dot(album.light, LIGHT_LABELS[album.light]), el("span", { text: album.light_text })),
    el("label", { class: "field-label", text: planned ? "Titel des neuen Albums" : "Album auf Flickr" }),
    el("div", { class: "field" }, title, planned ? el("button", { class: "btn", text: "Ändern", onclick: () => act.renameAlbum(album.id, title.value) }) : null),
    album.state === "exists" && album.photo_count !== null
      ? el("p", { class: "muted small", text: `Auf Flickr: ${count(album.photo_count, "Foto", "Fotos")}, ${count(album.video_count, "Video", "Videos")}` })
      : null,
    existingPicker(folder, context, act),
    el(
      "div",
      { class: "actions" },
      album.light === "yellow" && planned ? el("button", { class: "btn", text: "Trotzdem neues Album anlegen", onclick: () => act.confirmNew(album.id) }) : null,
      el("button", { class: "btn btn-ghost", text: "Kein Album-Ordner mehr", onclick: () => act.removeAlbum(folder.id) }),
    ),
  );
}

function existingPicker(folder, context, act) {
  const albums = context.flickrAlbums || [];
  if (!albums.length) return null;
  // Flickr allows duplicate titles; those get their id to tell them apart.
  const seen = new Map();
  for (const album of albums) seen.set(album.title, (seen.get(album.title) || 0) + 1);
  const byLabel = new Map(albums.map((album) => [seen.get(album.title) > 1 ? `${album.title} · ${album.id}` : album.title, album.id]));
  const listId = `flickr-albums-${folder.id}`;
  const input = el("input", { type: "text", list: listId, placeholder: "Albumtitel eintippen …", "aria-label": "Vorhandenes Album" });
  return el(
    "div",
    {},
    el("label", { class: "field-label", text: "Oder ein vorhandenes Flickr-Album verwenden (wenn es dort anders heißt)" }),
    el("datalist", { id: listId }, [...byLabel.keys()].map((label) => el("option", { value: label }))),
    el(
      "div",
      { class: "field" },
      input,
      el("button", {
        class: "btn",
        text: "Verwenden",
        onclick: () => act.useExisting(folder.id, byLabel.get(input.value)),
      }),
    ),
  );
}

function memberSection(folder, folders, act) {
  const albumFolder = folders.find((other) => other.album && other.album.id === folder.in_album);
  return el(
    "div",
    { class: "section" },
    el("p", { class: "section-title", text: "Album" }),
    el(
      "p",
      {},
      "Gehört zum Album ",
      el("strong", { text: albumFolder ? albumFolder.album.title : "?" }),
      albumFolder ? el("span", { class: "muted", text: ` (Album-Ordner „${albumFolder.name}“)` }) : null,
    ),
    albumFolder ? el("div", { class: "actions" }, el("button", { class: "btn btn-sm", text: "Zum Album-Ordner", onclick: () => act.select(albumFolder.id) })) : null,
  );
}

function uploadSection(folder) {
  const lines = [];
  if (!folder.included) {
    lines.push(el("p", { class: "muted", text: "Nicht angehakt: Die Dateien direkt in diesem Ordner werden nicht hochgeladen. Setze links das Häkchen, um sie mitzunehmen." }));
  } else {
    lines.push(el("div", { class: "note ok" }, icon("check"), el("span", { text: "Angehakt: Die Dateien direkt in diesem Ordner werden hochgeladen." })));
    if (folder.precheck_running) lines.push(el("p", { class: "muted", text: "Abgleich mit deinen Flickr-Fotos läuft …" }));
    else if (!folder.prechecked) lines.push(el("p", { class: "muted", text: "Noch nicht mit deinen Flickr-Fotos abgeglichen. Das passiert spätestens beim Hochladen." }));
    if (folder.open_matches) {
      lines.push(
        el(
          "div",
          { class: "note warn" },
          icon("warn"),
          el("span", { text: `${folder.open_matches} Datei(en) sind vielleicht schon auf Flickr. Sie werden erst hochgeladen, wenn du unten entschieden hast.` }),
        ),
      );
    }
    const upload = folder.upload;
    if (upload.verified || upload.failed) {
      lines.push(el("p", { class: "muted", text: `Hochgeladen: ${upload.verified} von ${upload.files}${upload.failed ? `, ${upload.failed} mit Fehler` : ""}` }));
    }
  }
  return el("div", { class: "section" }, el("p", { class: "section-title", text: "Hochladen" }), el("div", { class: "step-notes" }, lines));
}
