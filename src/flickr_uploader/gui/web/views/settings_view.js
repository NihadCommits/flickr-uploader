// The settings tab: the Flickr login, download defaults, what happens at
// start, and where the database and its backups are. Every value comes from
// the database (or, for the login, from the Windows Credential Manager)
// through the Python API and is written back at once. Key, secret and code
// only pass through their input fields on the way to Python; the page never
// gets them back and keeps no copy.

import { count, el, formatTime, icon } from "./ui.js";

export const MEDIA_LABELS = [
  ["all", "Alles"],
  ["photos", "Fotos"],
  ["videos", "Videos"],
];

export const TARGET_PROBLEMS = {
  missing: "Diesen Ordner gibt es gerade nicht (oder das Laufwerk fehlt). Bitte einen anderen wählen.",
  archive: "Dieser Ordner liegt inzwischen in deinem Foto-Archiv. Downloads kommen nie dorthin. Bitte einen anderen wählen.",
};

export function renderSettings(container, data, act) {
  const { settings, info, account, formOpen } = data;
  container.replaceChildren(
    el(
      "div",
      { class: "settings" },
      accountCard(account, formOpen, act),
      downloadCard(settings, act),
      startCard(settings, act),
      infoCard(info, act),
    ),
  );
}

// --- Flickr access ------------------------------------------------------------------

function accountCard(account, formOpen, act) {
  const ready = account.state === "ready";
  if (ready && !formOpen && !account.pending) {
    return card(
      "Flickr-Zugang",
      row(
        `Angemeldet als ${account.username || "(unbekannt)"}`,
        `Rechte: ${account.access_level}. Die App darf hochladen und Alben anlegen, aber nie etwas löschen.`,
        el(
          "div",
          { class: "row-buttons" },
          el("button", { class: "btn", onclick: act.openForm }, "Neu einrichten"),
          el("button", { class: "btn", onclick: act.logout }, "Abmelden"),
        ),
        account.problem ? el("div", { class: "note warn" }, icon("warn"), el("span", { text: account.problem })) : null,
        el("button", { class: "btn btn-ghost btn-sm link-button", onclick: () => act.openFlickrPage("authorized_apps") }, icon("external", 14), "Zugriff auf Flickr ansehen oder widerrufen"),
      ),
    );
  }
  return card(
    "Flickr-Zugang",
    el(
      "div",
      { class: "setup" },
      el("p", { class: "muted", text: "Damit die App auf dein Flickr-Konto zugreifen kann, brauchst du einen eigenen API-Key von Flickr (kostenlos für nicht-kommerzielle Nutzung). Key und Secret landen nur im Windows-Anmeldeinformationsspeicher, nie in einer Datei." }),
      account.state === "damaged" ? el("div", { class: "note warn" }, icon("warn"), el("span", { text: "Die gespeicherten Zugangsdaten sind beschädigt oder nicht lesbar. Bitte neu einrichten." })) : null,
      setupStep(1, "API-Key bei Flickr beantragen", "Auf der Flickr-Seite „Non-Commercial Key“ wählen und App-Name und Zweck kurz angeben. Danach zeigt Flickr Key und Secret.", el("button", { class: "btn", onclick: () => act.openFlickrPage("create_key") }, icon("external", 14), "Flickr-Seite öffnen")),
      setupStep(2, "Key und Secret eintragen", null, keyForm(account, act)),
      setupStep(3, "Zugriff auf Flickr erlauben", account.pending ? "Flickr ist jetzt im Browser offen. Erlaube dort den Zugriff („OK, I’ll authorize it“) und trag den Code ein, den Flickr danach zeigt." : "Nach Schritt 2 öffnet sich Flickr im Browser.", account.pending ? codeForm(act) : null),
      el(
        "div",
        { class: "setup-foot" },
        el("span", { class: "muted small", text: `Alternativ im Terminal: ${account.setup_command}` }),
        ready || account.pending ? el("button", { class: "btn btn-ghost btn-sm", onclick: act.closeForm }, "Abbrechen") : null,
      ),
    ),
  );
}

function setupStep(number, title, hint, ...content) {
  return el(
    "div",
    { class: "setup-step" },
    el("span", { class: "step-num" }, String(number)),
    el("div", { class: "setup-body" }, el("div", { class: "setting-label", text: title }), hint ? el("div", { class: "muted small", text: hint }) : null, ...content),
  );
}

function secretInput(label, name) {
  // Password fields: hidden on screen, never filled in or kept by the browser.
  return el("input", { type: "password", name, "aria-label": label, placeholder: label, autocomplete: "off", spellcheck: "false", autocapitalize: "off" });
}

function keyForm(account, act) {
  const key = secretInput("API-Key (32 Zeichen)", "key");
  const secret = secretInput("Secret (16 Zeichen)", "secret");
  const form = el(
    "form",
    {
      class: "setup-form",
      onsubmit: (event) => {
        event.preventDefault();
        act.startLogin(key, secret);
      },
    },
    key,
    secret,
    el("button", { class: `btn ${account.pending ? "" : "btn-primary"}`, type: "submit" }, account.pending ? "Neu starten" : "Weiter zu Flickr"),
  );
  return form;
}

function codeForm(act) {
  const code = el("input", { type: "text", name: "code", "aria-label": "Code von Flickr", placeholder: "Code von Flickr, z. B. 123-456-789", autocomplete: "off", spellcheck: "false" });
  return el(
    "form",
    {
      class: "setup-form",
      onsubmit: (event) => {
        event.preventDefault();
        act.finishLogin(code);
      },
    },
    code,
    el("button", { class: "btn btn-primary", type: "submit" }, "Anmeldung abschließen"),
    el("button", { class: "btn btn-ghost", type: "button", onclick: act.reopenLoginPage }, "Seite nochmal öffnen"),
  );
}

function downloadCard(settings, act) {
  const target = settings.download_target;
  return card(
    "Herunterladen",
    row(
      "Vorauswahl",
      "Dieser Filter ist gesetzt, wenn du im Reiter „Herunterladen“ ein Album anhakst. Pro Album lässt er sich dort ändern.",
      el(
        "div",
        { class: "seg", role: "radiogroup", "aria-label": "Vorauswahl beim Anhaken" },
        MEDIA_LABELS.map(([value, label]) =>
          el(
            "button",
            {
              class: value === settings.download_media ? "active" : "",
              role: "radio",
              "aria-checked": String(value === settings.download_media),
              onclick: () => act.setSetting("download_media", value),
            },
            label,
          ),
        ),
      ),
    ),
    row(
      "Zielordner",
      target ? null : "Noch keiner gewählt. Jedes Album kommt dort in einen eigenen Ordner.",
      el("button", { class: "btn", onclick: act.chooseTarget }, "Ändern …"),
      target ? el("div", { class: "path-line", text: target }) : null,
      settings.download_target_problem ? el("div", { class: "note warn" }, icon("warn"), el("span", { text: TARGET_PROBLEMS[settings.download_target_problem] })) : null,
    ),
  );
}

function startCard(settings, act) {
  return card(
    "Beim Start",
    el(
      "label",
      { class: "check-row" },
      el("input", {
        type: "checkbox",
        checked: settings.sync_albums_at_start,
        onchange: (event) => act.setSetting("sync_albums_at_start", event.target.checked),
      }),
      el(
        "span",
        {},
        el("span", { text: "Albenliste beim Start von Flickr holen" }),
        el("span", { class: "muted small", text: "Hält die Ampeln aktuell. Von Hand geht es jederzeit im Reiter „Alben“." }),
      ),
    ),
  );
}

function infoCard(info, act) {
  const backups = info.backups
    ? `${count(info.backups, "Backup", "Backups")} (die neuesten ${info.keep_backups} bleiben), das letzte ${formatTime(info.latest_backup)}`
    : "Noch keins";
  return card(
    "Info & Datensicherung",
    row("Version", null, el("span", { text: info.version })),
    row(
      "Datenbank",
      null,
      el("button", { class: "btn btn-ghost", onclick: () => act.openFolder(info.database_folder) }, "Im Explorer zeigen"),
      el("div", { class: "path-line", text: info.database }),
    ),
    row(
      "Backups",
      "Vor jedem Upload und vor jeder Umstellung der Datenbank legt die App selbst ein Backup an.",
      el(
        "div",
        { class: "row-buttons" },
        el("button", { class: "btn btn-ghost", disabled: !info.backups, onclick: () => act.openFolder(info.backup_folder) }, "Im Explorer zeigen"),
        el("button", { class: "btn", onclick: act.backupNow }, "Jetzt Backup anlegen"),
      ),
      el("div", { class: "muted small", text: backups }),
    ),
  );
}

function card(title, ...rows) {
  return el("section", { class: "card" }, el("div", { class: "card-head" }, title), el("div", { class: "setting-rows" }, rows));
}

// A labelled setting: label and hint on the left, the control on the right,
// optional extra lines (a path, a warning) below.
function row(label, hint, control, ...below) {
  return el(
    "div",
    { class: "setting" },
    el("div", { class: "setting-text" }, el("div", { class: "setting-label", text: label }), hint ? el("div", { class: "muted small", text: hint }) : null),
    el("div", { class: "setting-control" }, control),
    below.some(Boolean) ? el("div", { class: "setting-below" }, below) : null,
  );
}
