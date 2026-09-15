# Videx

PyQt6 video trimmer. It uses the portable `ffmpeg/ffmpeg.exe` included in this project; FFmpeg on `PATH` is also supported as a fallback.

```powershell
python -m venv venv
pip install -r requirements.txt
.\venv\Scripts\python.exe main.py
```

Open a video, set an in/out range, preview it, then export the trim.
