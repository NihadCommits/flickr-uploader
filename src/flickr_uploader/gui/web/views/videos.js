// Videos of a ticked folder that Flickr would not take as they are: too large
// (compress or split) or longer than Flickr plays (upload whole or split).

import { el, formatBytes, icon } from "./ui.js";

const LABELS = { original: "Ganz hochladen", compress: "Komprimieren", split: "Aufteilen" };

export function videosSection(videos, act) {
  if (!videos || !videos.videos.length) return null;
  return el(
    "div",
    { class: "section" },
    el("p", { class: "section-title", text: `Große und lange Videos (${videos.videos.length})` }),
    videos.open
      ? el(
          "div",
          { class: "note warn" },
          icon("warn"),
          el("span", { text: "Flickr nimmt Videos nur bis 1 GB. Wähle für jedes zu große Video, ob es komprimiert oder in Teile aufgeteilt hochgeladen wird. Bis dahin wartet es." }),
        )
      : el("p", { class: "muted small", text: "Dein Original bleibt unverändert. Komprimierte Fassungen und Teile entstehen nur kurz zum Hochladen." }),
    el("ul", { class: "decisions" }, videos.videos.map((video) => videoRow(video, act))),
  );
}

function videoRow(video, act) {
  const facts = [formatBytes(video.size), video.duration ? minutes(video.duration) : null].filter(Boolean).join(" · ");
  const why = video.too_large ? "zu groß für Flickr" : "länger als 10 Minuten (Flickr spielt nur so viel ab)";
  return el(
    "li",
    { class: `decision ${video.choice ? "" : "open"}` },
    el(
      "div",
      { class: "decision-head" },
      el("strong", { text: video.name }),
      el("span", { class: "muted small", text: `${facts} · ${why}` }),
    ),
    video.locked
      ? el("p", { class: "muted small", text: `${LABELS[video.choice]}${partsText(video)} – ist schon (teilweise) auf Flickr, die Wahl bleibt so.` })
      : el(
          "div",
          { class: "actions" },
          el(
            "div",
            { class: "seg", role: "radiogroup", "aria-label": `Was mit ${video.name} passiert` },
            video.allowed.map((choice) =>
              el(
                "button",
                {
                  class: choice === video.choice ? "active" : "",
                  role: "radio",
                  "aria-checked": String(choice === video.choice),
                  onclick: () => act.chooseVideo(video.file_id, choice),
                },
                choice === "split" ? `${LABELS.split}${partsText(video)}` : LABELS[choice],
              ),
            ),
          ),
          video.own_choice
            ? el("button", { class: "btn btn-ghost btn-sm", text: "Vorgabe verwenden", onclick: () => act.chooseVideo(video.file_id, null) })
            : el("span", { class: "muted small", text: video.choice ? "Vorgabe aus den Einstellungen" : "" }),
        ),
  );
}

function partsText(video) {
  return video.parts && video.parts > 1 ? ` (${video.parts} Teile)` : "";
}

function minutes(seconds) {
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")} min`;
}
