// The step guide above the tree: the four steps, what is done, what comes
// next, and anything that needs the user's eye. Built from overview() only.

import { el, icon } from "./ui.js";

const STEPS = ["Ordner öffnen", "Album-Ordner festlegen", "Ordner anhaken", "Hochladen"];

const NEXT = [
  "Öffne den Ordner, in dem deine Fotos liegen, z. B. „Bilder“. Das Tool liest ihn nur, es ändert nichts daran.",
  "Klicke im Baum auf einen Ordner, der ein Album werden soll (z. B. eine Reise), und lege ihn rechts als Album fest.",
  "Hake im Album-Ordner an, welche Ordner hochgeladen werden, z. B. „Jpegs“, aber nicht „RAWs“. Nur die Dateien direkt darin zählen.",
  "Starte oben das Hochladen. Du kannst jederzeit pausieren; beim Fortsetzen geht es genau dort weiter.",
];

export function renderSteps(container, overview, status, plan, uploading = false) {
  const uploaded = Boolean(plan && plan.files && !plan.ready && plan.done);
  const done = [overview.roots > 0, overview.album_folders > 0, overview.included > 0, uploaded];
  const next = done.findIndex((isDone) => !isDone);
  const current = next === -1 ? 3 : next;
  const items = [];
  STEPS.forEach((title, index) => {
    if (index) items.push(el("li", { class: `step-line${done[index - 1] ? " done" : ""}`, "aria-hidden": "true" }));
    const state = done[index] ? "done" : index === current ? "current" : "";
    items.push(
      el(
        "li",
        { class: `step ${state}` },
        el("span", { class: "step-num" }, done[index] ? icon("check", 14) : String(index + 1)),
        el("span", { text: title }),
      ),
    );
  });

  const notes = [];
  if (status && !status.logged_in) {
    notes.push(note("warn", "warn", status.message));
  }
  if (uploaded) notes.push(note("ok", "check", "Alles Angehakte ist auf Flickr. Neue Dateien in diesen Ordnern erkennt „Neu scannen“."));
  else if (uploading) notes.push(note("", "info", "Der Upload läuft. Du kannst ihn oben jederzeit pausieren; beim Fortsetzen geht es genau dort weiter."));
  else notes.push(note("", "info", `Als Nächstes: ${NEXT[current]}`));
  if (overview.lights.yellow) {
    notes.push(note("warn", "warn", `${overview.lights.yellow} Album-Ordner brauchen eine Entscheidung: Auf Flickr gibt es ein Album mit sehr ähnlichem Titel. Wähle den Album-Ordner aus, um zu entscheiden.`));
  }
  if (overview.open_matches) {
    notes.push(note("warn", "warn", `${overview.open_matches} Datei(en) in angehakten Ordnern sind vielleicht schon auf Flickr. Wähle den Ordner aus, um zu entscheiden (sie werden bis dahin nicht hochgeladen).`));
  }
  container.replaceChildren(el("ol", { class: "step-list" }, items), el("div", { class: "step-notes" }, notes));
}

function note(kind, iconName, text) {
  return el("div", { class: `note ${kind}` }, icon(iconName), el("span", { text }));
}
