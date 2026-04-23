# Phase 2: Selektive Migration

## Ziel

Nur den benoetigten Suno-Bestand aus dem Backup uebernehmen, ohne Runtime-State oder korruptionsanfaellige Daten mitzunehmen.

## Arbeitspunkte

- [x] `profiles/suno/config.yaml` aus der Quelle abgleichen und selektiv uebernehmen
- [x] `profiles/suno/SOUL.md` aus der Quelle abgleichen und selektiv uebernehmen
- [x] `profiles/suno/memories/MEMORY.md` aus der Quelle abgleichen und selektiv uebernehmen
- [x] `profiles/suno/memories/USER.md` aus der Quelle abgleichen und selektiv uebernehmen
- [x] `profiles/suno/skills/suno-create/` aus der Quelle abgleichen und selektiv uebernehmen
- [x] `plugins/suno/` aus der Quelle abgleichen und selektiv uebernehmen
- [x] Ausschluesse vor dem Kopieren erneut pruefen

## Verifikation

- [x] Keine ausgeschlossenen Runtime-Dateien wurden kopiert
- [x] Zielstruktur enthaelt alle freigegebenen Suno-Dateien
- [x] Quelle wurde nur aus `hermes-old-setup/.hermes` uebernommen

## Ausschluesse

- [x] `profiles/suno/sessions/` nicht uebernommen
- [x] `profiles/suno/logs/` nicht uebernommen
- [x] `profiles/suno/state.db*` nicht uebernommen
- [x] `profiles/suno/auth.json` nicht uebernommen
- [x] `profiles/suno/cache/` nicht uebernommen
- [x] `profiles/suno/browser_sessions/` nicht uebernommen
- [x] `profiles/suno/models_dev_cache.json` nicht uebernommen

## Notizen

- `profiles/suno/config.yaml`, `SOUL.md`, `memories/MEMORY.md` und `memories/USER.md` waren bereits inhaltlich deckungsgleich zur Quelle.
- In `profiles/suno/skills/suno-create/` wurden `node_modules/` und `__pycache__/` aus der Quelle bewusst nicht uebernommen.
- In `plugins/suno/` wurden nur lokale Artefakte (`._*`, Temp-Dateien) entfernt; als bewusste lokale Abweichung bleibt `plugins/suno/.env` bestehen.