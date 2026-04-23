# Phase 2: Selektive Migration

## Ziel

Nur den benoetigten Suno-Bestand aus dem Backup uebernehmen, ohne Runtime-State oder korruptionsanfaellige Daten mitzunehmen.

## Arbeitspunkte

- [ ] `profiles/suno/config.yaml` aus der Quelle abgleichen und selektiv uebernehmen
- [ ] `profiles/suno/SOUL.md` aus der Quelle abgleichen und selektiv uebernehmen
- [ ] `profiles/suno/memories/MEMORY.md` aus der Quelle abgleichen und selektiv uebernehmen
- [ ] `profiles/suno/memories/USER.md` aus der Quelle abgleichen und selektiv uebernehmen
- [ ] `profiles/suno/skills/suno-create/` aus der Quelle abgleichen und selektiv uebernehmen
- [ ] `plugins/suno/` aus der Quelle abgleichen und selektiv uebernehmen
- [ ] Ausschluesse vor dem Kopieren erneut pruefen

## Verifikation

- [ ] Keine ausgeschlossenen Runtime-Dateien wurden kopiert
- [ ] Zielstruktur enthaelt alle freigegebenen Suno-Dateien
- [ ] Quelle wurde nur aus `hermes-old-setup/.hermes` uebernommen

## Ausschluesse

- [ ] `profiles/suno/sessions/` nicht uebernommen
- [ ] `profiles/suno/logs/` nicht uebernommen
- [ ] `profiles/suno/state.db*` nicht uebernommen
- [ ] `profiles/suno/auth.json` nicht uebernommen
- [ ] `profiles/suno/cache/` nicht uebernommen
- [ ] `profiles/suno/browser_sessions/` nicht uebernommen
- [ ] `profiles/suno/models_dev_cache.json` nicht uebernommen