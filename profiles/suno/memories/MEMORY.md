Suno Beat Workflow: 1) Acknowledge 2) create preview state via `suno_preview_state(action=create)` 3) send preview with Style + Lyrics + buttons 4) wait for Go/Edit/Cancel 5) call `suno_generate` 6) send both MP3s via `send_message` with `MEDIA:` tags 7) clear preview state.
§
`suno_preview_state(action=create)` returns `style`, `lyrics`, `instrumental`, `title`. The preview state is the source of truth between turns.
§
`style_engineer.py` always appends the fixed structure guidance to `style` and always provides the default lyrics structure template.
§
`suno_generate` must receive both `styles=<style>` and `lyrics=<lyrics>`.
§
Use `lyrics_mode="instrumental"` for beat/instrumental requests, otherwise `lyrics_mode="custom"`.
§
Bei `edit_style` oder `edit_lyrics` zuerst `suno_preview_state(action=set_edit_mode)` aufrufen, dann die naechste User-Nachricht via `suno_preview_state(action=apply_edit, message=...)` in den gespeicherten Preview-State schreiben und den kompletten Preview erneut senden.
§
Bei `stop`/`cancel`/`abbruch` sofort Workflow stoppen, Preview-State loeschen und bestaetigen.
§
Immer `send_message` fuer Telegram verwenden, niemals echo/terminal/write_file fuer Benachrichtigungen.
§
Nie `terminal`, `echo`, Pipes oder `execute_code` fuer Preview-Edits verwenden. Immer `suno_preview_state`.
§
Helge Schneider Beat-Stil: User will humorvollen, jazzig-funken, dadaistischen Stil. Style: "jazz, cabaret, playful, upright bass, swinging drums, comic, piano, brass".
§
Wenn User nach `Katzenklo`-Vibe verlangt: Style auf jazzig, swingend, humorvoll, Komik-Vocals, Katzenthema.
