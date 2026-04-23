# Phase 3: Pfade und Plugin-Aktivierung

## Ziel

Migrierte Konfiguration auf den VPS-Pfadbestand `/home/dashi/.hermes` normieren und das Suno-Plugin aktiv schalten.

## Arbeitspunkte

- [ ] Alte lokale Pfade in `profiles/suno/config.yaml` ersetzen
- [ ] Alte lokale Pfade in `profiles/suno/skills/suno-create/` ersetzen
- [ ] Alte lokale Pfade in `plugins/suno/` ersetzen
- [ ] Suno-Plugin in der Profil-Konfiguration auf aktiv setzen
- [ ] `plugins/suno/.env` aus dem Template ableiten
- [ ] Output-Pfad in `.env` auf `/home/dashi/.hermes/profiles/suno/output` setzen

## Verifikation

- [ ] Kein `/Users/`-Pfad bleibt in den migrierten Suno-Dateien zurueck
- [ ] `profiles/suno/config.yaml` aktiviert Suno
- [ ] `plugins/suno/.env` existiert
- [ ] `.env` nutzt absolute VPS-Pfade

## Notizen

- Aktueller Zielzustand vor Migration: `plugins.disabled` enthaelt `suno`
- Bekannter Altpfad aus Backup: `/Users/damian/.hermes/profiles/suno`