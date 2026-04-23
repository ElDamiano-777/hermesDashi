# Phase 4: Verifikation und Testlauf

## Ziel

Hermes-, Plugin-, Login-, Generate- und Telegram-Funktion nach der Migration nachweisen.

## Arbeitspunkte

- [ ] `hermes profile list` ausfuehren und `suno` bestaetigen
- [ ] `hermes -p suno plugins list` ausfuehren und Plugin-Status bestaetigen
- [ ] `hermes -p suno gateway start` ohne Fehler pruefen
- [ ] Suno-Login-Prozess oder Session-Check ausfuehren und dokumentieren
- [ ] Test-Beat per CLI ausloesen und Ergebnis dokumentieren
- [ ] Telegram-Zustellung und Audio-Transfer dokumentieren

## Verifikation

- [ ] Profil-Integritaet bestaetigt
- [ ] Plugin-Status bestaetigt
- [ ] Gateway-Start bestaetigt
- [ ] Login-Check bestaetigt
- [ ] Generate-Test bestaetigt
- [ ] Telegram-Link bestaetigt

## Testkommando

- [ ] `hermes -p suno chat -q "Erstelle einen kurzen Test-Beat im Stil von Detroit Techno"`