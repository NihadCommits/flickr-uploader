// Files of a ticked folder that may already be on Flickr: per file "is the
// same photo" (taken over, not uploaded again) or "upload anyway".

import { el, icon } from "./ui.js";

const CONFIDENCE = { certain: "sicher", probable: "wahrscheinlich" };

export function decisionsSection(folder, matches, act) {
  if (!matches || !matches.files.length) return null;
  return el(
    "div",
    { class: "section" },
    el("p", { class: "section-title", text: `Vielleicht schon auf Flickr (${matches.files.length})` }),
    matches.open
      ? el(
          "div",
          { class: "note warn" },
          icon("warn"),
          el("span", { text: "Diese Dateien sehen aus wie Fotos, die schon auf Flickr sind. Solange du nicht entschieden hast, werden sie nicht hochgeladen." }),
        )
      : el("p", { class: "muted small", text: "Alles entschieden. Übernommene Fotos werden nicht noch einmal hochgeladen, sondern nur ins Album gelegt." }),
    matches.open
      ? el(
          "div",
          { class: "actions" },
          el("button", { class: "btn", text: "Alle sicheren übernehmen", onclick: () => act.decideFolder(folder.id, "same") }),
          el("button", { class: "btn btn-ghost", text: "Alle trotzdem hochladen", onclick: () => act.decideFolder(folder.id, "upload") }),
        )
      : null,
    el("ul", { class: "decisions" }, matches.files.map((file) => fileRow(file, act))),
  );
}

function fileRow(file, act) {
  const facts = [file.taken ? `aufgenommen ${file.taken}` : null, file.size].filter(Boolean).join(" · ");
  return el(
    "li",
    { class: `decision ${file.state}` },
    el(
      "div",
      { class: "decision-head" },
      el("div", { class: "grow" }, el("div", { class: "title", text: file.name }), facts ? el("div", { class: "sub", text: facts }) : null),
      stateChip(file),
    ),
    file.candidates.map((candidate) =>
      el(
        "div",
        { class: "candidate" },
        el(
          "div",
          { class: "candidate-info" },
          el("span", { class: `chip ${candidate.confidence === "certain" ? "ok" : "warn"}`, text: CONFIDENCE[candidate.confidence] }),
          el("span", { class: "muted small", text: candidate.reason }),
        ),
        el(
          "div",
          { class: "decision-actions" },
          el("button", { class: "btn btn-ghost btn-sm", onclick: () => act.openOnFlickr(candidate.flickr_id) }, "Auf Flickr ansehen", icon("external", 12)),
          file.state === "open"
            ? el("button", { class: "btn btn-sm", text: file.candidates.length > 1 ? "Ist dieses Foto" : "Ist dasselbe Foto", onclick: () => act.chooseSame(file.file_id, candidate.flickr_id) })
            : null,
        ),
      ),
    ),
    file.done
      ? null
      : el(
          "div",
          { class: "decision-actions" },
          file.state === "open"
            ? el("button", { class: "btn btn-ghost btn-sm", text: "Trotzdem hochladen", onclick: () => act.chooseUpload(file.file_id) })
            : el("button", { class: "btn btn-ghost btn-sm", text: "Entscheidung ändern", onclick: () => act.resetChoice(file.file_id) }),
        ),
  );
}

function stateChip(file) {
  if (file.state === "same") return el("span", { class: "chip ok", text: file.done ? "übernommen" : "wird übernommen" });
  if (file.state === "upload") return el("span", { class: `chip ${file.done ? "ok" : "info"}`, text: file.done ? "hochgeladen" : "wird hochgeladen" });
  return el("span", { class: "chip warn", text: "offen" });
}
