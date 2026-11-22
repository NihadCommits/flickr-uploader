// "Fotos ansehen" / "Fotos zuordnen": the files of a ticked folder, grouped by
// the day they were taken, with a small preview. Select single photos (Shift:
// a range) or whole days; in a per-file folder give them their album, in any
// folder put them into further albums as well (uploaded once). Everything
// comes from the database through the Python API; `state` only remembers the
// selection and what was typed.

import { count, el, icon } from "./ui.js";

const DAY = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "numeric", month: "long", year: "numeric" });

export function renderSorter(container, data, state, act) {
  const files = data.files;
  const days = groupByDay(files);
  const selectable = (file) => !file.skipped;
  const selected = files.filter((file) => state.selected.has(file.id));

  const albumInput = el("input", {
    type: "text",
    list: "sorter-albums",
    placeholder: "Album wählen oder neuen Namen eingeben",
    "aria-label": "Album",
    value: state.albumText || "",
    oninput: (event) => (state.albumText = event.target.value),
  });
  const extraInput = el("input", {
    type: "text",
    list: "sorter-albums",
    placeholder: "Weiteres Album, z. B. für einen Post",
    "aria-label": "Zusätzliches Album",
    value: state.extraText || "",
    oninput: (event) => (state.extraText = event.target.value),
  });
  const none = !selected.length;
  const bar = el(
    "div",
    { class: "sorter-bar" },
    el("strong", { class: "sorter-count", text: none ? "Nichts ausgewählt" : `${count(selected.length, "Foto", "Fotos")} ausgewählt` }),
    el("button", { class: "btn btn-ghost", disabled: none, onclick: act.clearSelection }, "Auswahl leeren"),
    el("datalist", { id: "sorter-albums" }, data.albums.map((album) => el("option", { value: album.title }))),
    data.per_file
      ? el(
          "div",
          { class: "sorter-row" },
          el("span", { class: "sorter-label", text: "Album" }),
          albumInput,
          el("button", { class: "btn btn-primary", disabled: none, onclick: () => act.assign(albumInput.value) }, "Zuordnen"),
          el("button", { class: "btn", disabled: none, onclick: act.unassign }, "Nicht hochladen"),
        )
      : null,
    el(
      "div",
      { class: "sorter-row" },
      el("span", { class: "sorter-label", text: "Zusätzlich in" }),
      extraInput,
      el("button", { class: data.per_file ? "btn" : "btn btn-primary", disabled: none, onclick: () => act.addExtra(extraInput.value) }, "Hinzufügen"),
      el("button", { class: "btn btn-ghost", disabled: none, onclick: () => act.removeExtra(extraInput.value) }, "Daraus entfernen"),
    ),
  );

  const groups = days.map(([day, dayFiles]) => {
    const open = dayFiles.filter(selectable);
    const allSelected = open.length > 0 && open.every((file) => state.selected.has(file.id));
    return el(
      "section",
      { class: "sorter-day" },
      el(
        "label",
        { class: "sorter-day-head" },
        el("input", {
          type: "checkbox",
          checked: allSelected,
          disabled: !open.length,
          onchange: (event) => act.selectMany(open.map((file) => file.id), event.target.checked),
        }),
        el("strong", { text: day }),
        el("span", { class: "muted small", text: dayText(dayFiles) }),
      ),
      el(
        "div",
        { class: "sorter-grid" },
        dayFiles.map((file) => tile(file, state, selectable(file), act)),
      ),
    );
  });

  container.replaceChildren(
    el(
      "div",
      { class: "dialog sorter", role: "dialog", "aria-labelledby": "sorter-title" },
      el(
        "div",
        { class: "sorter-head" },
        el("h3", { id: "sorter-title" }, `${data.per_file ? "Fotos zuordnen" : "Fotos ansehen"} · ${data.folderName}`),
        el("span", { class: "muted small", text: summary(files) }),
        el("span", { class: "spacer" }),
        el("button", { class: "btn btn-primary", onclick: act.close }, "Fertig"),
      ),
      el(
        "p",
        { class: "muted small", text: `Tipp: Einen ganzen Tag über sein Häkchen wählen, mehrere Fotos mit Umschalt-Klick. ${data.per_file ? "Nicht zugeordnete Fotos werden nicht hochgeladen. " : ""}„Zusätzlich in“ legt Fotos auch in ein weiteres Album, ohne sie doppelt hochzuladen; schon hochgeladene kommen beim nächsten „Hochladen starten“ dazu.` },
      ),
      bar,
      el("div", { class: "sorter-days" }, groups.length ? groups : el("p", { class: "muted", text: "In diesem Ordner liegen keine Fotos." })),
    ),
  );
  act.observeThumbnails(container);
}

function tile(file, state, selectable, act) {
  const picked = state.selected.has(file.id);
  const time = file.taken.slice(11, 16);
  return el(
    "button",
    {
      class: `sorter-tile${picked ? " picked" : ""}${selectable ? "" : " fixed"}`,
      title: `${file.name}${file.taken_known ? "" : " (Datum der Datei, kein Aufnahmedatum)"}`,
      disabled: !selectable,
      onclick: (event) => act.toggle(file.id, event.shiftKey),
    },
    el(
      "span",
      { class: "sorter-thumb", "data-file": file.video ? null : String(file.id) },
      act.thumbnailFor(file.id) ? el("img", { src: act.thumbnailFor(file.id), alt: "" }) : icon(file.video ? "updown" : "folder", 22),
    ),
    el("span", { class: "sorter-time", text: file.taken_known ? time : `${time}*` }),
    el("span", { class: "sorter-name", text: file.name }),
    file.done
      ? el("span", { class: "chip ok", text: `auf Flickr${file.album ? ` · ${file.album}` : ""}` })
      : file.skipped
        ? el("span", { class: "chip", text: "übersprungen" })
        : file.album
          ? el("span", { class: "chip info", text: file.album })
          : el("span", { class: "chip", text: "kein Album" }),
    file.extras.length ? el("span", { class: "chip", text: `+ ${file.extras.join(", ")}`, title: "Zusätzlich in diesen Alben" }) : null,
  );
}

function groupByDay(files) {
  const days = new Map();
  for (const file of files) {
    const [year, month, day] = file.taken.slice(0, 10).split("-").map(Number);
    const label = DAY.format(new Date(year, month - 1, day));
    if (!days.has(label)) days.set(label, []);
    days.get(label).push(file);
  }
  return [...days.entries()];
}

function dayText(files) {
  const albums = new Map();
  let open = 0;
  for (const file of files) {
    if (file.album) albums.set(file.album, (albums.get(file.album) || 0) + 1);
    else if (!file.skipped) open += 1;
  }
  const parts = [...albums.entries()].map(([title, n]) => `${title} ${n}`);
  if (open) parts.push(`ohne Album ${open}`);
  return ` · ${count(files.length, "Datei", "Dateien")}${parts.length ? " · " + parts.join(", ") : ""}`;
}

function summary(files) {
  const assigned = files.filter((file) => file.album).length;
  const extra = files.filter((file) => file.extras.length).length;
  const extraText = extra ? ` · ${extra} auch in weiteren Alben` : "";
  return `${count(files.length, "Datei", "Dateien")} · ${assigned} mit Album · ${files.length - assigned} ohne${extraText}`;
}
