# Phase 3: Pfade und Plugin-Aktivierung

## Ziel

Migrierte Konfiguration auf den VPS-Pfadbestand `/home/dashi/.hermes` normieren und das Suno-Plugin aktiv schalten.

## Arbeitspunkte

- [x] Alte lokale Pfade in `profiles/suno/config.yaml` ersetzen
- [x] Alte lokale Pfade in `profiles/suno/skills/suno-create/` ersetzen
- [x] Alte lokale Pfade im profil-lokalen Plugin `profiles/suno/plugins/suno-local/` ersetzen
- [x] Suno-Plugin in der Profil-Konfiguration auf aktiv setzen
- [x] `profiles/suno/plugins/suno-local/.env` aus dem Template bzw. bestehender VPS-Konfiguration ableiten
- [x] Output-Pfad in `.env` auf `/home/dashi/.hermes/profiles/suno/output` setzen

## Verifikation

- [x] Kein `/Users/`-Pfad bleibt in den migrierten Suno-Dateien zurueck
- [x] `profiles/suno/config.yaml` aktiviert das profil-lokale Suno-Plugin
- [x] `profiles/suno/plugins/suno-local/.env` existiert
- [x] `.env` nutzt absolute VPS-Pfade

## Notizen

- Aktueller Zielzustand vor Migration: `plugins.disabled` enthaelt `suno`
- Bekannter Altpfad aus Backup: `/Users/damian/.hermes/profiles/suno`
- Hermes laedt Plugins profil-scoped aus `HERMES_HOME/plugins`; fuer `-p suno` ist das `profiles/suno/plugins/`.
- Daher wurde das funktionale Plugin als `profiles/suno/plugins/suno-local/` bereitgestellt und in `profiles/suno/config.yaml` mit `plugins.enabled: [suno-local]` aktiviert.
- Das profil-lokale `suno-local` aus dem Backup wurde selektiv ohne `profile/`, `__pycache__/` und ohne das alte `.env` mit macOS-Pfad uebernommen.