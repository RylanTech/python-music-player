# Terminal CLI Music Player

A terminal-based music player in Python built with `curses`, `mutagen`, `Pillow`, and `ffplay`. It organizes music from a `Music -> Artist -> Album -> Songs` directory structure and renders album covers as ASCII art.

> **Note:** This code was written with the use of AI.

---

## Setup

### 1. System Requirements
* **Python 3.10+**
* **`ffplay` (from FFmpeg)** for audio playback:
  * **macOS:** `brew install ffmpeg`
  * **Ubuntu / Debian:** `sudo apt install ffmpeg`
  * **Windows:** Install FFmpeg and add `ffplay.exe` to your `PATH`.

### 2. Python Environment & Dependencies
Create and activate a virtual environment in your project folder, then install the required packages:

```bash
python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows (Command Prompt)
.venv\Scripts\activate.bat

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install mutagen pillow
```

### Expected folder layout
```
~/Music/
└── Artist Name/
    └── Album Name/
        ├── folder.jpg        # or large_cover.jpg
        ├── 01 - Song One.mp3
        └── 02 - Song Two.flac
```
