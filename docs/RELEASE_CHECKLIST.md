# Checkliste vor einer Veröffentlichung

Vor jedem Release (und vor dem ersten Freischalten des Repos) müssen alle
Punkte abgehakt sein. Grund: Was einmal öffentlich in der Git-History
stand, gilt als kompromittiert, auch wenn es später gelöscht wird.

## Secrets

- [ ] Kompletter Secret-Scan über **alle** Commits, nicht nur den aktuellen Stand:
      `gitleaks git --config .gitleaks.toml --redact --verbose`
      (gitleaks liegt nach `pre-commit install` im pre-commit-Cache, alternativ
      den letzten CI-Lauf "Secret scan" prüfen)
- [ ] Falls jemals ein Key/Token committet wurde: **sofort bei Flickr widerrufen
      und neu erzeugen.** History umschreiben allein reicht nicht.
- [ ] `git log --all --stat` einmal durchsehen: keine `.db`-, `.env`-,
      `credentials*`-, CSV- oder Fotodateien in irgendeinem Commit.
- [ ] Nach dem Freischalten auf GitHub unter *Settings → Code security*
      **Secret scanning** und **Push protection** aktivieren (für öffentliche
      Repos kostenlos).

## Persönliche Daten

- [ ] **Commit-E-Mail:** Jeder Commit enthält die E-Mail-Adresse des Autors.
      Wenn die private Adresse nicht öffentlich sein soll, möglichst früh die
      GitHub-noreply-Adresse verwenden (GitHub → *Settings → Emails*, dort
      "Keep my email addresses private"). Schon bestehende Commits behalten
      ihre alte Adresse, außer die History wird vor der Veröffentlichung neu
      geschrieben.
- [ ] Keine echten Fotos, Albennamen, Ordnerpfade oder Logs in Tests, Doku oder
      Screenshots.

## Projekt

- [ ] Lizenz gewählt und als `LICENSE` eingecheckt (Lizenzen eingebundener
      fremder Dateien gehören nach `gui/web/vendor/`, derzeit gibt es keine).
- [ ] README: Status-Hinweis aktualisiert, Screenshot ergänzt.
- [ ] CI ist grün.
