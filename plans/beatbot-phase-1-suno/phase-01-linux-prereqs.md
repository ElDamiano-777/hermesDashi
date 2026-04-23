# Phase 1: Linux-Voraussetzungen

## Ziel

Headless-Browser-Betrieb fuer das Suno-Plugin auf dem VPS vorbereiten, bevor Dateien migriert werden.

## Arbeitspunkte

- [x] Aktuellen Ist-Zustand pruefen: `Xvfb`, `chromium` oder `chromium-browser`, Python-/Node-Playwright
- [x] Passenden Chromium-Binary-Pfad fuer den VPS festlegen
- [x] Fehlende Browser-Abhaengigkeiten installieren
- [x] Playwright fuer das Suno-Plugin verifizieren oder nachinstallieren
- [x] Xvfb-Startkommando fuer den VPS festlegen
- [x] CDP-Port- und User-Data-Dir-Anforderungen aus Suno-Doku gegenpruefen

## Verifikation

- [x] `Xvfb` ist verfuegbar
- [x] Chromium ist aufrufbar
- [x] Browser-Engine kann headless oder unter Xvfb starten
- [x] Anforderungen fuer `SUNO_CDP_PORT` und persistentes Profil sind dokumentiert

## Notizen

- Quelle fuer das Plugin-Template: `/home/dashi/.hermes/hermes-old-setup/.hermes/plugins/suno/.env.example`
- Bestaetigt: `Xvfb` und `xvfb-run` vorhanden
- Bestaetigt: Python-`playwright` in `/home/dashi/.hermes/hermes-agent/venv` installiert
- Bestaetigt: Playwright-Chromium unter `/home/dashi/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome` vorhanden
- Festgelegter VPS-Startpfad: `/home/dashi/.local/bin/chromium` und `/home/dashi/.local/bin/chromium-browser` zeigen auf `/home/dashi/.hermes/bin/chromium-playwright-wrapper`
- Festgelegtes Xvfb-Startkommando im Wrapper: `xvfb-run -a --server-args="-screen 0 1280x720x24"`
- Verifikation: `chromium --version` liefert `Google Chrome for Testing 147.0.7727.15`
- Verifikation: CDP-Start mit persistentem `--user-data-dir` funktioniert auf Port `9336`
- Ubuntu 24.04 VPS-Besonderheit: Wrapper injiziert `--no-sandbox`, weil Chromium sonst vor dem CDP-Start mit `No usable sandbox!` abbricht