# Phase 4: Verifikation und Testlauf

## Ziel

Hermes-, Plugin-, Login-, Generate- und Telegram-Funktion nach der Migration nachweisen.

## Arbeitspunkte

- [x] `hermes profile list` ausfuehren und `suno` bestaetigen
- [x] `hermes -p suno plugins list` ausfuehren und Plugin-Status bestaetigen
- [ ] `hermes -p suno gateway start` ohne Fehler pruefen
- [x] Suno-Login-Prozess oder Session-Check ausfuehren und dokumentieren
- [ ] Test-Beat per CLI ausloesen und Ergebnis dokumentieren
- [ ] Telegram-Zustellung und Audio-Transfer dokumentieren

## Verifikation

- [x] Profil-Integritaet bestaetigt
- [x] Plugin-Status bestaetigt
- [ ] Gateway-Start bestaetigt
- [x] Login-Check bestaetigt
- [ ] Generate-Test bestaetigt
- [x] Telegram-Link bestaetigt

## Testkommando

- [ ] `hermes -p suno chat -q "Erstelle einen kurzen Test-Beat im Stil von Detroit Techno"`

## Ergebnisse

- `./hermes-agent/venv/bin/python ./hermes-agent/hermes profile list` zeigt das Profil `suno` mit Modell `gpt-4.1`.
- `./hermes-agent/venv/bin/python ./hermes-agent/hermes -p suno plugins list` zeigt `suno-local` als `enabled`.
- `./hermes-agent/venv/bin/python ./hermes-agent/hermes -p suno gateway start` scheitert aktuell an fehlender `systemd --user`-Unit: `hermes-gateway-suno.service not found`.
- Fallback-Check: `./hermes-agent/venv/bin/python ./hermes-agent/hermes -p suno gateway run` startet im Vordergrund und zeigt den Gateway-Startbanner.
- Direkter Session-Check ueber das profil-lokale Plugin meldet `session_valid=true` und `session check status=200`.
- Premium/Billing-Check bleibt unklar (`billing info status=401`).
- Direkter Generate-Test ueber `profiles/suno/plugins/suno-local/suno.py` startet, scheitert aber serverseitig mit `Captcha check failed: status=401, body={"detail": "Unauthorized"}`.
- Direkter Telegram-API-Test mit dem Profil-Bot war erfolgreich: `getMe` liefert den Bot, und `sendMessage` an `chat_id=-1003969468087` mit `message_thread_id=59` wurde erfolgreich zugestellt.
- Die Topic-URL `https://t.me/c/3969468087/59/1860` mappt korrekt auf `chat_id=-1003969468087` und `thread_id=59`; die letzte Zahl ist die konkrete Nachrichten-ID, nicht die Topic-ID.
- Ursache fuer das spaetere Nicht-Antworten im Topic war kein Telegram-Routingfehler: der `suno`-Bot bekam die Updates, aber der profil-lokale Gateway lief nicht wirklich. Stale `profiles/suno/gateway.pid` und `profiles/suno/gateway_state.json` liessen den Status irrefuehrend wirken, waehrend effektiv nur der Default-Gateway unter `/home/dashi/.hermes` aktiv war.
- Fix: stale `profiles/suno/gateway.pid` und `profiles/suno/gateway_state.json` entfernen und den Gateway explizit mit `HERMES_HOME=/home/dashi/.hermes/profiles/suno ./hermes-agent/venv/bin/python ./hermes-agent/hermes gateway run` neu starten. Danach fiel `pending_update_count` des Profil-Bots auf `0`, und Topic-Nachrichten wurden wieder verarbeitet.
- Audio-Transfer nach Telegram bleibt offen, weil noch kein erfolgreicher Generate-Lauf mit auslieferbaren Audiodateien vorliegt.