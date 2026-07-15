# VT Vocal Countdown — User guide

## Control page (`/`)

- **Source mode:** vMix or QLab (connection fields auto-save).
- **Arm / Disarm:** starts or stops polling remaining time and speaking the schedule.
- **Output device:** which Mac/Windows audio device receives announcements (and pips).
- **Announcement schedule:** rows fire when remaining time crosses each threshold. Drag phrases into slots; pick a voice per row (or leave **Active**).
- **Variants:** under 2 minutes, choose number-only vs “N seconds”; at 60s also **1 minute left on VT**.
- **10 → 1:** continuous take or separate callouts (style on the sequence row).
- **Manage voice:** open the voice library / recorder.
- **Export / Import:** ZIP of profile + recorded clips.

## QLab cues

When connected in QLab mode:

- Enable **Countdown** only on cues that should drive announcements.
- **Voice** column: Active (global pack) or a specific pack for that cue.
- **Cue voice first** / **Schedule voice first:** only used when *both* cue and schedule row have a specific voice set. If either is Active, the specific choice wins.

## Manage voice (`/voice`)

- **Library:** play clips, Play all (with gaps), record/edit/upload (unless the pack is locked).
- **Default** pack is locked (stock). Use **New…** for an editable pack.
- **Record pack:** guided walkthrough of schedule clips.
- **10→1 take:** count-in → Go → 10…1 with auto level normalisation.
- Trim overlay: set in/out points; **Normalise levels** equalises loudness.

## Display (`/display`)

Full-screen remaining time for a second monitor. Opens independently of the control page.

## Pips (secret)

On the control page, press **⌘P** (Mac) or **Ctrl+P** (Windows) for Greenwich-style hour/quarter marks, specific times, and leap-second options. Pips play alongside a speaking countdown (they do not cut it off).

## Troubleshooting

- **Cannot Arm:** a required clip for an enabled schedule row is missing on the active (or per-row) voice — open Manage voice / Record missing.
- **No sound:** confirm output device; try Play on a clip in the library.
- **QLab not connecting:** check IP, OSC ports (53000/53001), and QLab OSC access / passcode.
- **macOS “app is damaged / can’t be opened”:** usually Gatekeeper on an unsigned download — right-click → Open.
