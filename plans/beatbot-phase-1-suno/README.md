# Beatbot Phase 1 (Suno)

Quelle: `/home/dashi/.hermes/hermes-old-setup/.hermes`

Ziel: `/home/dashi/.hermes`

Arbeitsregel: Jede Phase wird erst nach Agent-Verifikation abgehakt.

## Phasen

- [x] Phase 1: Linux-Voraussetzungen und Headless-Browser vorbereiten
- [x] Phase 2: Selektive Suno-Migration aus dem Backup uebernehmen
- [x] Phase 3: Pfade anpassen und Suno-Plugin aktivieren
- [ ] Phase 4: Hermes-, Login-, Generate- und Telegram-Checks dokumentieren

## Ausschluesse

- [ ] `sessions/` wird nicht migriert
- [ ] `logs/` wird nicht migriert
- [ ] `state.db*` wird nicht migriert
- [ ] `auth.json` wird nicht migriert
- [ ] `cache/` wird nicht migriert
- [ ] `browser_sessions/` wird nicht migriert
- [ ] `models_dev_cache.json` wird nicht migriert

## Erfolgsdefinition

- [ ] `hermes profile list` zeigt `suno`
- [ ] `hermes -p suno plugins list` zeigt das Suno-Plugin aktiv
- [ ] `hermes -p suno gateway start` startet ohne Fehler
- [ ] Suno-Login-Check ist erfolgreich dokumentiert
- [ ] Test-Generation ist erfolgreich dokumentiert
- [ ] Telegram-Zustellung inkl. Audio-Datei ist erfolgreich dokumentiert