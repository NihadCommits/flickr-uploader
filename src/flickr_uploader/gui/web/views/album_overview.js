// The albums tab: the user's album folders with their traffic lights, and the
// albums on Flickr as last read (searchable, with the folders that use them).

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

function flickrCard(flickr, view, act) {
  const search = el("input", {
    type: "search",
    class: "search",
    placeholder: "Album suchen …",
    value: view.albumSearch,
    "aria-label": "Album suchen",
    oninput: (event) => {
      view.albumSearch = event.target.value;
      list.replaceChildren(...albumRows(flickr.albums, view.albumSearch));
    },
  });
  const list = el("ul", { class: "list" }, albumRows(flickr.albums, view.albumSearch));
  return el(
    "div",
    { class: "card" },
    el(
      "div",
      { class: "card-head" },
      "Deine Alben auf Flickr",
      el("span", { class: "muted", text: `(${flickr.albums.length})` }),
      el("span", { class: "spacer" }),
      el("span", { class: "muted small", text: flickr.synced_at ? `Stand: ${formatTime(flickr.synced_at)}` : "noch nicht geholt" }),
      el("button", { class: "btn btn-sm", onclick: act.syncAlbums, disabled: view.syncing }, icon("sync", 14), view.syncing ? "Wird geholt …" : "Aktualisieren"),
    ),
    flickr.albums.length ? search : null,
    flickr.albums.length ? list : el("div", { class: "empty", text: "Die Albenliste wurde noch nicht von Flickr geholt." }),
  );
}

function albumRows(albums, query) {
  const wanted = (query || "").trim().toLocaleLowerCase("de-DE");
  const shown = wanted ? albums.filter((album) => album.title.toLocaleLowerCase("de-DE").includes(wanted)) : albums;
  if (!shown.length) return [el("li", {}, el("span", { class: "muted", text: "Kein Album gefunden." }))];
  return shown.map((album) =>
    el(
      "li",
      {},
      el(
        "div",
        { class: "grow" },
        el("div", { class: "title", text: album.title }),
        el("div", { class: "sub", text: `${count(album.photo_count, "Foto", "Fotos")} · ${count(album.video_count, "Video", "Videos")}` }),
      ),
      album.used_by ? el("span", { class: "chip ok", text: album.used_by === 1 ? "von 1 Ordner verwendet" : `von ${album.used_by} Ordnern verwendet` }) : null,
    ),
  );
}
