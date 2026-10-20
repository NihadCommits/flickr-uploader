// Small helpers shared by the views. Text always goes in as text, never as
// HTML: folder and album names come from the disk and from Flickr. Only the
// fixed icon drawings below are inserted as markup.

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const ICONS = {
  folder: '<path d="M2.5 4.5A1.5 1.5 0 0 1 4 3h3l1.5 1.5H12A1.5 1.5 0 0 1 13.5 6v5.5A1.5 1.5 0 0 1 12 13H4a1.5 1.5 0 0 1-1.5-1.5z"/>',
  chevron: '<path d="M6 4l4 4-4 4"/>',
  check: '<path d="M3.5 8.5l3 3 6-7"/>',
  info: '<circle cx="8" cy="8" r="6"/><path d="M8 7.2v3.6M8 5.2v.1"/>',
  warn: '<path d="M8 2.5l6 10.5H2z"/><path d="M8 6.5v3M8 11.2v.1"/>',
  arrow: '<path d="M3 8h10M9 4l4 4-4 4"/>',
  sync: '<path d="M13 5.5A5.5 5.5 0 0 0 3.2 6M3 10.5A5.5 5.5 0 0 0 12.8 10"/><path d="M13 2.5v3h-3M3 13.5v-3h3"/>',
  updown: '<path d="M5 13V3M2.5 5.5L5 3l2.5 2.5M11 3v10M8.5 10.5L11 13l2.5-2.5"/>',
  upload: '<path d="M8 11V3M5 6l3-3 3 3"/><path d="M3 11v1.5A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5V11"/>',
  pause: '<path d="M6 4v8M10 4v8"/>',
  download: '<path d="M8 3v8M5 8l3 3 3-3"/><path d="M3 11v1.5A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5V11"/>',
  external: '<path d="M9 3h4v4M13 3L7.5 8.5"/><path d="M12 9.5v2A1.5 1.5 0 0 1 10.5 13h-6A1.5 1.5 0 0 1 3 11.5v-6A1.5 1.5 0 0 1 4.5 4h2"/>',
};

export function icon(name, size = 16) {
  const span = document.createElement("span");
  span.className = "icon";
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  return span;
}

const BYTE_UNITS = ["Byte", "KB", "MB", "GB", "TB"];

export function formatBytes(bytes) {
  let value = bytes || 0;
  let unit = 0;
  while (value >= 1000 && unit < BYTE_UNITS.length - 1) {
    value /= 1000;
    unit += 1;
  }
  const digits = unit === 0 || value >= 100 ? 0 : 1;
  return `${value.toLocaleString("de-DE", { maximumFractionDigits: digits })} ${BYTE_UNITS[unit]}`;
}

export function count(number, one, many) {
  return `${(number || 0).toLocaleString("de-DE")} ${number === 1 ? one : many}`;
}

export function dot(color, title) {
  return el("span", { class: `dot ${color}`, title, "aria-label": title });
}

export function formatTime(iso) {
  if (!iso) return "";
  const when = new Date(iso);
  const today = new Date().toDateString() === when.toDateString();
  const time = when.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  return today ? `heute, ${time}` : `${when.toLocaleDateString("de-DE")}, ${time}`;
}

export const LIGHT_LABELS = {
  green: "auf Flickr",
  blue: "neues Album",
  yellow: "Entscheidung nötig",
};
