// The albums tab: the user's album folders with their traffic lights, and the
// albums on Flickr as last read, in their order there (searchable, with the
// folders that use them). "Bearbeiten" renames and reorders them; new names
// and the order are form input of the page until "Speichern".

import { LIGHT_LABELS, count, dot, el, formatTime, icon } from "./ui.js";

export function renderAlbums(container, folders, flickr, view, act) {
  container.replaceChildren(el("div", { class: "albums-grid" }, folderCard(folders, act), flickrCard(flickr, view, act)));
}

function folderCard(folders, act) {
  const body = folders.length
    ? el(
        "ul",
        { class: "list" },
        folders.map((row) =>
          el(
            "li",
            {},
            dot(row.album.light, LIGHT_LABELS[row.album.light]),
            el("div", { class: "grow" }, el("div", { class: "title", text: row.album.title }), el("div", { class: "sub", text: row.path })),
            el("span", { class: `chip ${{ green: "ok", blue: "info", yellow: "warn" }[row.album.light]}`, text: stateText(row.album) }),
            el("button", { class: "btn btn-ghost btn-sm", onclick: () => act.reveal(row.root_id, row.folder_id) }, "Im Baum zeigen", icon("arrow", 14)),
          ),
        ),
      )
    : el("div", { class: "empty" }, "Noch keine Album-Ordner.", el("br"), "Lege im Reiter „Hochladen“ einen Ordner als Album fest.");
  return el(
    "div",
    { class: "card" },
    el("div", { class: "card-head" }, "Deine Album-Ordner", el("span", { class: "muted", text: `(${folders.length})` })),
    body,
  );
}

function stateText(album) {
  if (album.state === "exists" && album.photo_count !== null) {
    return `${count(album.photo_count, "Foto", "Fotos")} · ${count(album.video_count, "Video", "Videos")}`;
  }
  return LIGHT_LABELS[album.light];
}

const COLLATOR = new Intl.Collator("de-DE", { numeric: true, sensitivity: "base" });

export const SORTS = [
  ["flickr", "Wie auf Flickr"],
  ["title_asc", "Name A → Z"],
  ["title_desc", "Name Z → A"],
  ["taken_desc", "Aufnahmedatum, neueste zuerst"],
  ["taken_asc", "Aufnahmedatum, älteste zuerst"],
];

// The order the list would have on Flickr after saving: pinned albums first
// (in their current order), the rest sorted, by the new names where given.
export function editedOrder(albums, edit) {
  const title = (album) => (edit.drafts.get(album.id) ?? album.title).trim();
  const byPosition = (a, b) => (a.position ?? 1e9) - (b.position ?? 1e9);
  const byTaken = (direction) => (a, b) => {
    if (!a.taken || !b.taken) return (a.taken ? 0 : 1) - (b.taken ? 0 : 1) || byPosition(a, b);
    return direction * a.taken.localeCompare(b.taken);
  };
  const compare = {
    flickr: byPosition,
    title_asc: (a, b) => COLLATOR.compare(title(a), title(b)),
    title_desc: (a, b) => COLLATOR.compare(title(b), title(a)),
    taken_desc: byTaken(-1),
    taken_asc: byTaken(1),
  }[edit.sort];
  const pinned = albums.filter((album) => edit.pinned.has(album.id)).sort(byPosition);
  const rest = albums.filter((album) => !edit.pinned.has(album.id)).sort(compare);
  return [...pinned, ...rest];
}

// What "Speichern" would send: changed names, and the order if it differs.
export function editedChanges(albums, edit) {
  const renames = albums
    .filter((album) => edit.drafts.has(album.id))
    .map((album) => ({ album_id: album.id, title: edit.drafts.get(album.id).trim(), old: album.title }))
    .filter((change) => change.title && change.title !== change.old);
  const now = [...albums].sort((a, b) => (a.position ?? 1e9) - (b.position ?? 1e9)).map((album) => album.id);
  const next = editedOrder(albums, edit).map((album) => album.id);
  const reordered = next.some((id, index) => id !== now[index]);
  return { renames, order: reordered ? next : null };
}

function flickrCard(flickr, view, act) {
  const edit = view.albumEdit;
  const search = el("input", {
    type: "search",
    class: "search",
    placeholder: "Album suchen …",
    value: view.albumSearch,
    "aria-label": "Album suchen",
    oninput: (event) => {
      view.albumSearch = event.target.value;
      list.replaceChildren(...albumRows(flickr.albums, view, act));
    },
  });
  const list = el("ul", { class: `list${edit ? " editing" : ""}` }, albumRows(flickr.albums, view, act));
  const head = edit
    ? [el("span", { class: "chip info", text: "Bearbeiten" })]
    : [
        el("span", { class: "muted small", text: flickr.synced_at ? `Stand: ${formatTime(flickr.synced_at)}` : "noch nicht geholt" }),
        el("button", { class: "btn btn-sm", onclick: act.syncAlbums, disabled: view.syncing }, icon("sync", 14), view.syncing ? "Wird geholt …" : "Aktualisieren"),
        flickr.albums.length ? el("button", { class: "btn btn-sm", onclick: act.startEdit }, "Bearbeiten") : null,
      ];
  return el(
    "div",
    { class: "card" },
    el("div", { class: "card-head" }, "Deine Alben auf Flickr", el("span", { class: "muted", text: `(${flickr.albums.length})` }), el("span", { class: "spacer" }), head),
    edit
      ? el(
          "div",
          { class: "edit-bar" },
          el("label", { class: "muted small", text: "Sortieren:" }),
          el(
            "select",
            { "aria-label": "Sortieren", onchange: (event) => act.setSort(event.target.value) },
            SORTS.map(([value, label]) => el("option", { value, selected: value === edit.sort }, label)),
          ),
          el("span", { class: "spacer" }),
          el("button", { class: "btn", onclick: act.cancelEdit }, "Abbrechen"),
          el("button", { class: "btn btn-primary btn-save", id: "albums-save", onclick: act.saveEdit }, saveLabel(flickr.albums, edit)),
          el("p", { class: "edit-hint muted small", text: "Neue Namen und die Reihenfolge werden erst mit „Speichern“ auf Flickr übernommen. Bis dahin bleiben deine Eingaben erhalten, auch beim Umsortieren." }),
        )
      : null,
    flickr.albums.length ? search : null,
    flickr.albums.length ? list : el("div", { class: "empty", text: "Die Albenliste wurde noch nicht von Flickr geholt." }),
  );
}

export function saveLabel(albums, edit) {
  const { renames, order } = editedChanges(albums, edit);
  const parts = [];
  if (renames.length) parts.push(count(renames.length, "Name", "Namen"));
  if (order) parts.push("Reihenfolge");
  return parts.length ? `Speichern (${parts.join(", ")})` : "Speichern";
}

function albumRows(albums, view, act) {
  const edit = view.albumEdit;
  const sorted = edit ? editedOrder(albums, edit) : albums;
  const wanted = (view.albumSearch || "").trim().toLocaleLowerCase("de-DE");
  const matches = (album) =>
    album.title.toLocaleLowerCase("de-DE").includes(wanted) || (edit?.drafts.get(album.id) ?? "").toLocaleLowerCase("de-DE").includes(wanted);
  const shown = wanted ? sorted.filter(matches) : sorted;
  if (!shown.length) return [el("li", {}, el("span", { class: "muted", text: "Kein Album gefunden." }))];
  return shown.map((album) => (edit ? editRow(album, sorted.indexOf(album) + 1, edit, act) : viewRow(album)));
}

function viewRow(album) {
  return el(
    "li",
    {},
    el(
      "div",
      { class: "grow" },
      el("div", { class: "title", text: album.title }),
      el("div", { class: "sub", text: `${count(album.photo_count, "Foto", "Fotos")} · ${count(album.video_count, "Video", "Videos")}` }),
    ),
    album.used_by ? el("span", { class: "chip ok", text: album.used_by === 1 ? "von 1 Ordner verwendet" : `von ${album.used_by} Ordnern verwendet` }) : null,
  );
}

function editRow(album, place, edit, act) {
  const draft = edit.drafts.get(album.id);
  const changed = draft !== undefined && draft.trim() !== "" && draft.trim() !== album.title;
  const open = draft !== undefined;
  const pinned = edit.pinned.has(album.id);
  const date = album.taken ? album.taken.slice(0, 10) : null;
  const facts = [changed ? `vorher: ${album.title}` : null, date ? `ab ${date}` : null, `${count(album.photo_count, "Foto", "Fotos")} · ${count(album.video_count, "Video", "Videos")}`];
  return el(
    "li",
    { class: changed ? "renamed" : "" },
    el("span", { class: "place", text: String(place) }),
    el(
      "div",
      { class: "grow" },
      open
        ? el("input", {
            type: "text",
            class: "rename",
            value: draft,
            "aria-label": `Neuer Name für ${album.title}`,
            "data-album": String(album.id),
            oninput: (event) => act.setDraft(album.id, event.target.value),
            onkeydown: (event) => event.key === "Escape" && act.dropDraft(album.id),
          })
        : el("div", { class: "title", text: album.title }),
      el("div", { class: "sub", text: facts.filter(Boolean).join(" · ") }),
    ),
    open
      ? el("button", { class: "btn btn-ghost btn-sm", title: "Neuen Namen verwerfen", onclick: () => act.dropDraft(album.id) }, "Zurück")
      : el("button", { class: "btn btn-sm", onclick: () => act.openRename(album.id, album.title) }, "Umbenennen"),
    el(
      "button",
      { class: `btn btn-sm pin${pinned ? " on" : ""}`, title: "Beim Sortieren oben halten", "aria-pressed": String(pinned), onclick: () => act.togglePin(album.id) },
      pinned ? "Oben ✓" : "Oben",
    ),
  );
}
