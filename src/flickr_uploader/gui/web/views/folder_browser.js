// The folder tree: one row per folder with its tick box (only inside album
// folders), name, file count and what is going on with it.

import { LIGHT_LABELS, count, dot, el, formatBytes, icon } from "./ui.js";

export function renderTree(container, folders, view, on, rootPath) {
  const children = new Map();
  let root = null;
  for (const folder of folders) {
    if (folder.parent === null) root = folder;
    else {
      if (!children.has(folder.parent)) children.set(folder.parent, []);
      children.get(folder.parent).push(folder);
    }
  }
  if (!root) {
    container.replaceChildren(el("div", { class: "empty", text: "Dieser Ordner ist leer." }));
    return;
  }

  function node(folder) {
    const kids = children.get(folder.path) || [];
    const row = folderRow(folder, view, on, rootPath, kids.length > 0);
    if (!kids.length) return el("li", {}, row);
    return el(
      "li",
      {},
      el(
        "details",
        {
          open: folder.parent === null || view.open.has(folder.id),
          ontoggle: (event) => {
            if (event.target.open) view.open.add(folder.id);
            else view.open.delete(folder.id);
          },
        },
        el("summary", {}, row),
        el("ul", {}, kids.map(node)),
      ),
    );
  }

  container.replaceChildren(el("ul", { class: "tree" }, node(root)));
  const selected = container.querySelector(".row.selected");
  if (selected && view.scrollToSelected) {
    selected.scrollIntoView({ block: "center" });
    view.scrollToSelected = false;
  }
}

function folderRow(folder, view, on, rootPath, hasChildren) {
  const tick = folder.in_album
    ? el("input", {
        type: "checkbox",
        checked: folder.included,
        title: "Hochladen: die Dateien direkt in diesem Ordner",
        "aria-label": `${folder.name} hochladen`,
        onclick: (event) => event.stopPropagation(),
        onchange: (event) => on.tick(folder.id, event.target.checked),
      })
    : null;
  return el(
    "div",
    {
      class: `row${folder.album ? " album" : ""}${folder.id === view.selectedId ? " selected" : ""}`,
      onclick: (event) => {
        // The chevron opens and closes; a click anywhere else selects (and opens).
        if (event.target.closest(".chevron")) return;
        event.preventDefault();
        on.select(folder.id);
      },
    },
    el("span", { class: `chevron${hasChildren ? "" : " none"}` }, icon("chevron", 14)),
    el("span", { class: "tick" }, tick),
    el("span", { class: "folder-icon" }, icon("folder")),
    el("span", { class: "row-name", text: folder.name, title: folder.parent === null ? rootPath : folder.path }),
    folder.files ? el("span", { class: "row-meta", text: `${count(folder.files, "Datei", "Dateien")} · ${formatBytes(folder.bytes)}` }) : null,
    folder.album ? albumChip(folder.album) : null,
    folder.included ? statusChips(folder) : null,
  );
}

function albumChip(album) {
  const kind = { green: "ok", blue: "info", yellow: "warn" }[album.light];
  return el("span", { class: `chip ${kind}`, title: album.light_text }, dot(album.light, LIGHT_LABELS[album.light]), album.title);
}

function statusChips(folder) {
  const chips = [];
  if (folder.precheck_running) chips.push(el("span", { class: "chip" }, el("span", { class: "spinner" }), "Abgleich läuft"));
  else if (!folder.prechecked) chips.push(el("span", { class: "chip", text: "nicht abgeglichen" }));
  if (folder.open_matches) chips.push(el("span", { class: "chip warn", text: `${folder.open_matches} vielleicht schon auf Flickr` }));
  const upload = folder.upload;
  if (upload.state === "done") chips.push(el("span", { class: "chip ok", text: "hochgeladen" }));
  else if (upload.state === "partial") chips.push(el("span", { class: "chip info", text: `${upload.verified} von ${upload.files} hochgeladen` }));
  else if (upload.state === "error") chips.push(el("span", { class: "chip error", text: `${upload.failed} Fehler` }));
  return chips;
}
