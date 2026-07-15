# VT Vocal Countdown

Local control app that polls **QLab** (OSC) or **vMix** (HTTP XML) for remaining media time and plays spoken countdown announcements from recorded voice packs.

Works as a **standalone app** on **macOS** and **Windows** (no Python install required), or from source for development.

## Download (standalone)

Grab the latest build from [Releases](https://github.com/josh-justjosh/Countdown-App/releases):

| Platform | Asset |
|----------|--------|
| macOS (Apple Silicon) | `VT-Vocal-Countdown-macOS-arm64.zip` |
| Windows (x64) | `VT-Vocal-Countdown-Windows-x64.zip` |

### macOS

1. Unzip the download.
2. Open **VT Vocal Countdown**. If Gatekeeper blocks it: right-click the app → **Open** → confirm.
3. Your browser should open to [http://127.0.0.1:5050](http://127.0.0.1:5050).

### Windows

1. Unzip the download (keep the folder together — do not run the `.exe` alone outside its folder).
2. Run **VT Vocal Countdown.exe**.
3. Your browser should open to [http://127.0.0.1:5050](http://127.0.0.1:5050). Windows Firewall may ask to allow network access on first launch.

## First run

1. A **Default** stock voice is already installed (read-only). You can **Arm** immediately after connecting a source.
2. Choose **vMix** or **QLab**, enter the host IP (`127.0.0.1` if on the same machine), and **Test connection**.
   - In **QLab**, click **Connect**, enable the cues you want announced, and optionally set a **voice per cue**.
3. Select the **output device** (audio plays on the machine running this app).
4. Click **Arm**.

To use your own voice: **Manage voice** → **New…**, then record the pack / 10→1 take (or import a ZIP).

See [docs/USER_GUIDE.md](docs/USER_GUIDE.md) for the full UI walkthrough.

## Features (highlights)

- Multi voice packs; Default stock voice is locked from edits
- Continuous or separate 10→1 roll-down
- Schedule variants (e.g. `30` / `30 seconds`)
- QLab per-cue voice + cue vs schedule priority
- Secret **pips** (Greenwich-style): ⌘P / Ctrl+P
- Import / export voice + settings as ZIP

## Developers (from source)

Requirements: **Python 3.11+**, optional **ffmpeg** (non-WAV uploads).

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m backend.main
```

Open [http://127.0.0.1:5050](http://127.0.0.1:5050). Large display: [http://127.0.0.1:5050/display](http://127.0.0.1:5050/display).

### Build standalone locally

```bash
pip install -r requirements.txt -r requirements-build.txt
# macOS:
./scripts/build_release.sh
# Windows (PowerShell):
.\scripts\build_release.ps1
```

Outputs appear under `dist/`.

## Data location

| Mode | Profile & voice recordings |
|------|----------------------------|
| Standalone (macOS) | `~/Library/Application Support/VTVocalCountdown/` |
| Standalone (Windows) | `%LOCALAPPDATA%\VTVocalCountdown\` |
| From source | `./data/` (gitignored) |

Stock Default wavs ship under `assets/stock_voices/default/` and are copied into the data folder on first run if Default is empty.

## Notes

- The app listens on **`0.0.0.0:5050`** (reachable on your LAN). Restrict with a firewall if needed.
- Physical audio always plays on the **app host**.
- QLab and/or vMix must be reachable on the network; they are not bundled.

## License

MIT — see [LICENSE](LICENSE).
