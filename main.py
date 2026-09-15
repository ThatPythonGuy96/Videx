"""Videx — a focused PyQt6 video trimming editor."""
from __future__ import annotations

import shutil
import sys
import tempfile
from hashlib import sha1
from pathlib import Path

from PyQt6.QtCore import QProcess, QSize, QTime, QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QPainter, QPen
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSizePolicy, QStatusBar, QStyle, QToolButton, QVBoxLayout, QWidget


class TrimTimeline(QWidget):
    """Timeline with draggable start/end handles and a seek playhead."""
    positionChanged = pyqtSignal(int)
    rangeChanged = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = self.position = self.start = self.end = 0
        self.dragging = None
        self.setMinimumHeight(48)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_duration(self, duration):
        self.duration = max(0, duration)
        self.start, self.end, self.position = 0, self.duration, 0
        self.update()

    def set_position(self, position):
        self.position = max(0, min(position, self.duration))
        self.update()

    def set_range(self, start, end):
        self.start = max(0, min(start, self.duration))
        self.end = max(self.start, min(end, self.duration))
        self.rangeChanged.emit(self.start, self.end)
        self.update()

    def track(self):
        return 18, max(1, self.width() - 36), self.height() // 2

    def x_for(self, value):
        margin, width, _ = self.track()
        return margin + round(width * value / max(1, self.duration))

    def value_for(self, x):
        margin, width, _ = self.track()
        return round(max(0, min(1, (x - margin) / width)) * self.duration)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin, width, y = self.track()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#303746"))
        p.drawRoundedRect(margin, y - 4, width, 8, 4, 4)
        left, right = self.x_for(self.start), self.x_for(self.end)
        p.setBrush(QColor("#2676ff"))
        p.drawRoundedRect(left, y - 4, max(1, right - left), 8, 4, 4)
        play_x = self.x_for(self.position)
        p.setPen(QPen(QColor("#f7f9ff"), 2))
        p.drawLine(play_x, y - 14, play_x, y + 14)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#dce9ff"))
        for x in (left, right):
            p.drawRoundedRect(x - 5, y - 13, 10, 26, 3, 3)

    def mousePressEvent(self, event):
        if not self.duration:
            return
        x = int(event.position().x())
        if abs(x - self.x_for(self.start)) < 14:
            self.dragging = "start"
        elif abs(x - self.x_for(self.end)) < 14:
            self.dragging = "end"
        else:
            self.dragging = "playhead"
            self.positionChanged.emit(self.value_for(x))

    def mouseMoveEvent(self, event):
        if not self.dragging:
            return
        value = self.value_for(int(event.position().x()))
        if self.dragging == "start":
            self.start = min(value, self.end)
            self.rangeChanged.emit(self.start, self.end)
        elif self.dragging == "end":
            self.end = max(value, self.start)
            self.rangeChanged.emit(self.start, self.end)
        else:
            self.positionChanged.emit(value)
        self.update()

    def mouseReleaseEvent(self, _event):
        self.dragging = None


def time_text(milliseconds):
    return QTime(0, 0).addMSecs(max(0, milliseconds)).toString("mm:ss.zzz")[:-1]


def ffmpeg_command():
    """Prefer the portable FFmpeg bundled beside this application."""
    bundled = Path(__file__).resolve().parent / "ffmpeg" / "ffmpeg.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("ffmpeg")


class ClipCard(QWidget):
    """Compact list entry; the list itself controls selection and ordering."""
    removeRequested = pyqtSignal(str)

    def __init__(self, clip_id, name, parent=None):
        super().__init__(parent)
        self.clip_id = clip_id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 7, 7)
        text = QVBoxLayout()
        self.name = QLabel(name)
        self.name.setObjectName("clipName")
        self.details = QLabel("Loading…")
        self.details.setObjectName("clipDetails")
        text.addWidget(self.name); text.addWidget(self.details)
        delete = QToolButton()
        delete.setText("×")
        delete.setObjectName("deleteClip")
        delete.setToolTip("Remove clip")
        delete.clicked.connect(lambda: self.removeRequested.emit(self.clip_id))
        layout.addLayout(text, 1); layout.addWidget(delete)

    def set_details(self, start, end):
        self.details.setText(f"{time_text(start)} — {time_text(end)}")


class VidexWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Videx — Video Trimmer")
        self.resize(1000, 720)
        self.current_file = None
        self.active_clip_id = None
        self.clips = {}
        self.clip_items = {}
        self.preview_file = None
        self.preview_process = None
        self.selection_playback = False
        self.export_process = None
        self.merge_process = None
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.7)
        self.player.setAudioOutput(self.audio)
        # Media backends can emit position changes far faster than the display
        # refresh rate. Coalescing them prevents the controls from competing
        # with video decoding and rendering.
        self.pending_position = 0
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)
        self.player.playbackStateChanged.connect(self.on_playback)
        self.player.errorOccurred.connect(lambda *_: self.statusBar().showMessage(self.player.errorString(), 6000))
        self.end_timer = QTimer(self)
        self.end_timer.setInterval(100)
        self.end_timer.timeout.connect(self.stop_at_end)
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(33)  # redraw controls at a maximum of 30 fps
        self.ui_timer.timeout.connect(self.refresh_playback_ui)
        self.ui_timer.start()
        self.build_ui()
        self.apply_style()

    def build_ui(self):
        open_action = QAction("Add Videos…", self, triggered=self.open_video)
        open_action.setShortcut("Ctrl+O")
        self.addAction(open_action)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 22, 28, 20)
        layout.setSpacing(14)
        header = QHBoxLayout()
        logo = QLabel("VIDEX")
        logo.setObjectName("logo")
        sub = QLabel("TRIM STUDIO")
        sub.setObjectName("subtitle")
        open_button = QPushButton("Add Videos")
        open_button.clicked.connect(self.open_video)
        header.addWidget(logo); header.addWidget(sub); header.addStretch(); header.addWidget(open_button)
        layout.addLayout(header)
        workspace = QHBoxLayout()
        workspace.setSpacing(14)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(245)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(12, 14, 12, 12)
        side_title = QLabel("CLIPS")
        side_title.setObjectName("sideTitle")
        side_hint = QLabel("Merge order: top to bottom")
        side_hint.setObjectName("sideHint")
        self.clip_list = QListWidget()
        self.clip_list.setObjectName("clipList")
        self.clip_list.currentItemChanged.connect(self.select_clip)
        self.merge = QPushButton("Merge All Trims")
        self.merge.setObjectName("merge")
        self.merge.clicked.connect(self.merge_trims)
        side_layout.addWidget(side_title); side_layout.addWidget(side_hint); side_layout.addWidget(self.clip_list, 1); side_layout.addWidget(self.merge)
        workspace.addWidget(sidebar)
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(14)
        self.video = QVideoWidget()
        self.video.setMinimumHeight(390)
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.player.setVideoOutput(self.video)
        editor_layout.addWidget(self.video, 1)
        controls = QFrame()
        controls.setObjectName("controls")
        c_layout = QVBoxLayout(controls)
        c_layout.setContentsMargins(18, 12, 18, 14)
        self.timeline = TrimTimeline()
        self.timeline.positionChanged.connect(self.player.setPosition)
        self.timeline.rangeChanged.connect(self.on_range_changed)
        c_layout.addWidget(self.timeline)
        row = QHBoxLayout()
        self.time_label = QLabel("00:00.00 / 00:00.00")
        self.in_label = QLabel("IN  00:00.00")
        self.out_label = QLabel("OUT  00:00.00")
        set_in = QPushButton("Set In")
        set_out = QPushButton("Set Out")
        set_in.clicked.connect(lambda: self.timeline.set_range(self.player.position(), self.timeline.end))
        set_out.clicked.connect(lambda: self.timeline.set_range(self.timeline.start, self.player.position()))
        self.play = QToolButton()
        self.play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play.clicked.connect(self.toggle_play)
        play_selection = QPushButton("Play Selection")
        play_selection.clicked.connect(self.play_trim)
        self.export = QPushButton("Export Trim")
        self.export.setObjectName("export")
        self.export.clicked.connect(self.export_trim)
        row.addWidget(self.time_label); row.addStretch(); row.addWidget(self.in_label); row.addWidget(set_in); row.addWidget(self.out_label); row.addWidget(set_out); row.addWidget(self.play); row.addWidget(play_selection); row.addWidget(self.export)
        c_layout.addLayout(row)
        editor_layout.addWidget(controls)
        workspace.addWidget(editor, 1)
        layout.addLayout(workspace, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Open a video to start trimming")

    def apply_style(self):
        self.setStyleSheet("""
          QMainWindow { background: #121722; color: #e8edf8; } QLabel { color: #c8d1e2; font-size: 12px; }
          QLabel#logo { font-size: 20px; color: #f2f5fc; font-weight: 800; letter-spacing: 3px; } QLabel#subtitle { color: #6f7f98; font-size: 10px; letter-spacing: 2px; }
          QLabel#sideTitle { font-size: 12px; font-weight: 700; letter-spacing: 1px; } QLabel#sideHint, QLabel#clipDetails { color: #8492a8; font-size: 10px; } QLabel#clipName { color: #ecf1fc; font-weight: 600; }
          QVideoWidget { background: #090c12; border-radius: 10px; } QFrame#controls, QFrame#sidebar { background: #1a2130; border: 1px solid #2a3448; border-radius: 10px; }
          QListWidget#clipList { background: transparent; border: none; outline: none; } QListWidget#clipList::item { margin: 3px 0; border-radius: 6px; } QListWidget#clipList::item:selected { background: #2b4269; }
          QPushButton, QToolButton { background: #273247; border: none; border-radius: 6px; padding: 8px 12px; color: #e8edf8; } QPushButton:hover, QToolButton:hover { background: #354463; }
          QToolButton#deleteClip { background: transparent; color: #9eacc2; font-size: 18px; padding: 1px 5px; } QToolButton#deleteClip:hover { color: #ff8794; background: #372731; }
          QPushButton#export, QPushButton#merge { background: #2676ff; font-weight: 600; } QPushButton#export:hover, QPushButton#merge:hover { background: #438cff; } QStatusBar { color: #91a0b8; background: #121722; }
        """)

    def open_video(self):
        filenames, _ = QFileDialog.getOpenFileNames(self, "Add videos", "", "Video files (*.mp4 *.mov *.mkv *.avi *.webm *.m4v);;All files (*)")
        for filename in filenames:
            self.add_clip(Path(filename))

    def add_clip(self, source):
        clip_id = sha1(f"{source.resolve()}:{len(self.clips)}".encode()).hexdigest()
        clip = {"id": clip_id, "source": source, "start": 0, "end": 0, "duration": 0}
        self.clips[clip_id] = clip
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, clip_id)
        item.setSizeHint(QSize(215, 60))
        card = ClipCard(clip_id, source.name)
        card.removeRequested.connect(self.remove_clip)
        self.clip_list.addItem(item)
        self.clip_list.setItemWidget(item, card)
        self.clip_items[clip_id] = item
        self.clip_list.setCurrentItem(item)

    def active_clip(self):
        return self.clips.get(self.active_clip_id)

    def select_clip(self, item, _previous):
        if not item:
            return
        self.save_active_trim()
        self.active_clip_id = item.data(Qt.ItemDataRole.UserRole)
        clip = self.active_clip()
        self.current_file = clip["source"]
        self.timeline.set_duration(clip["duration"])
        self.timeline.set_range(clip["start"], clip["end"] if clip["end"] else clip["duration"])
        self.prepare_preview()

    def save_active_trim(self):
        clip = self.active_clip()
        if clip and self.timeline.duration:
            clip["start"], clip["end"], clip["duration"] = self.timeline.start, self.timeline.end, self.timeline.duration
            self.update_clip_card(clip)

    def update_clip_card(self, clip):
        item = self.clip_items.get(clip["id"])
        if item:
            card = self.clip_list.itemWidget(item)
            if card:
                card.set_details(clip["start"], clip["end"])

    def remove_clip(self, clip_id):
        item = self.clip_items.pop(clip_id, None)
        if not item:
            return
        row = self.clip_list.row(item)
        self.clip_list.takeItem(row)
        self.clips.pop(clip_id, None)
        if clip_id == self.active_clip_id:
            self.active_clip_id = None
            self.current_file = None
            self.player.stop()
            self.timeline.set_duration(0)
            self.update_labels()
        self.statusBar().showMessage("Clip removed", 3000)

    def prepare_preview(self):
        """Remux non-MP4 sources for reliable native Qt playback.

        This copies the existing H.264/AAC streams without re-encoding, so it
        is quick and does not reduce quality. The original remains the export
        source; the MP4 is used only for preview playback.
        """
        if not self.current_file:
            return
        if self.current_file.suffix.lower() == ".mp4":
            self.load_preview(self.current_file)
            return
        ffmpeg = ffmpeg_command()
        if not ffmpeg:
            self.load_preview(self.current_file)
            return
        cache = Path(tempfile.gettempdir()) / "videx-preview"
        cache.mkdir(parents=True, exist_ok=True)
        source_id = f"{self.current_file.resolve()}:{self.current_file.stat().st_mtime_ns}".encode()
        self.preview_file = cache / f"{sha1(source_id).hexdigest()}.mp4"
        if self.preview_file.exists() and self.preview_file.stat().st_size > 0:
            self.load_preview(self.preview_file)
            return
        self.player.stop()
        self.statusBar().showMessage("Preparing an optimized preview…")
        self.preview_process = QProcess(self)
        self.preview_process.finished.connect(self.preview_finished)
        # Only video and the first audio stream are needed for editing preview.
        self.preview_process.start(ffmpeg, ["-y", "-i", str(self.current_file), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(self.preview_file)])

    def preview_finished(self, exit_code, _status):
        if exit_code == 0 and self.preview_file and self.preview_file.exists():
            self.load_preview(self.preview_file)
        else:
            # Falling back keeps uncommon codecs usable even when MP4 remuxing
            # is not possible for a particular source.
            self.load_preview(self.current_file)
            self.statusBar().showMessage("Preview optimization failed; using original file.", 6000)

    def load_preview(self, preview):
        self.player.setSource(QUrl.fromLocalFile(str(preview)))
        source_note = "optimized preview" if preview != self.current_file else "source preview"
        self.statusBar().showMessage(f"Loaded: {self.current_file.name} ({source_note})")

    def on_duration(self, duration):
        clip = self.active_clip()
        if clip:
            clip["duration"] = duration
            if not clip["end"]:
                clip["end"] = duration
            self.timeline.set_duration(duration)
            self.timeline.set_range(clip["start"], clip["end"])
            self.update_clip_card(clip)
        else:
            self.timeline.set_duration(duration)
        self.update_labels()

    def on_position(self, position):
        self.pending_position = position

    def on_range_changed(self, _start, _end):
        self.save_active_trim()
        self.update_labels()

    def refresh_playback_ui(self):
        """Keep controls responsive without redrawing for every backend tick."""
        if self.pending_position != self.timeline.position:
            self.timeline.set_position(self.pending_position)
            self.update_labels()

    def update_labels(self):
        self.time_label.setText(f"{time_text(self.player.position())} / {time_text(self.timeline.duration)}")
        self.in_label.setText(f"IN  {time_text(self.timeline.start)}")
        self.out_label.setText(f"OUT  {time_text(self.timeline.end)}")

    def toggle_play(self):
        self.selection_playback = False
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState: self.player.pause()
        else: self.player.play()

    def play_trim(self):
        if self.timeline.end <= self.timeline.start: return
        self.selection_playback = True
        self.player.setPosition(self.timeline.start)
        self.player.play()
        self.end_timer.start()

    def stop_at_end(self):
        if self.selection_playback and self.player.position() >= self.timeline.end:
            self.player.pause(); self.player.setPosition(self.timeline.start); self.selection_playback = False; self.end_timer.stop()

    def on_playback(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay))
        if not playing: self.end_timer.stop()

    def export_trim(self):
        if not self.current_file or self.timeline.end <= self.timeline.start:
            QMessageBox.warning(self, "Nothing to export", "Load a video and select a non-empty trim range first.")
            return
        ffmpeg = ffmpeg_command()
        if not ffmpeg:
            QMessageBox.critical(self, "FFmpeg not found", "Place ffmpeg.exe in the project's ffmpeg folder, or add FFmpeg to PATH.")
            return
        default = self.current_file.with_name(f"{self.current_file.stem}_trimmed.mp4")
        output, _ = QFileDialog.getSaveFileName(self, "Export trimmed clip", str(default), "MP4 video (*.mp4)")
        if not output: return
        self.export.setEnabled(False); self.export.setText("Exporting…")
        self.statusBar().showMessage("Exporting trim with FFmpeg…")
        self.export_process = QProcess(self)
        self.export_process.finished.connect(self.export_finished)
        self.export_process.start(ffmpeg, ["-y", "-ss", f"{self.timeline.start / 1000:.3f}", "-i", str(self.current_file), "-t", f"{(self.timeline.end - self.timeline.start) / 1000:.3f}", "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", output])

    def export_finished(self, exit_code, _status):
        self.export.setEnabled(True); self.export.setText("Export Trim")
        if exit_code == 0:
            self.statusBar().showMessage("Export complete", 5000)
            QMessageBox.information(self, "Export complete", "Your trimmed video has been saved.")
        else:
            details = bytes(self.export_process.readAllStandardError()).decode(errors="replace") if self.export_process else ""
            QMessageBox.critical(self, "Export failed", details[-1200:] or "FFmpeg could not export this video.")

    def ordered_clips(self):
        clips = []
        for row in range(self.clip_list.count()):
            clip_id = self.clip_list.item(row).data(Qt.ItemDataRole.UserRole)
            clip = self.clips.get(clip_id)
            if clip and clip["duration"] and clip["end"] > clip["start"]:
                clips.append(clip)
        return clips

    def merge_trims(self):
        self.save_active_trim()
        clips = self.ordered_clips()
        if len(clips) < 2:
            QMessageBox.warning(self, "Add more clips", "Add at least two videos with a non-empty trim range to merge them.")
            return
        ffmpeg = ffmpeg_command()
        if not ffmpeg:
            QMessageBox.critical(self, "FFmpeg not found", "Place ffmpeg.exe in the project's ffmpeg folder, or add FFmpeg to PATH.")
            return
        default = clips[0]["source"].with_name("videx_merged.mp4")
        output, _ = QFileDialog.getSaveFileName(self, "Export merged trims", str(default), "MP4 video (*.mp4)")
        if not output:
            return
        args = ["-y"]
        filters = []
        for index, clip in enumerate(clips):
            args.extend(["-i", str(clip["source"])])
            start, end = clip["start"] / 1000, clip["end"] / 1000
            # Normalising makes merge dependable even when input clips differ.
            filters.append(f"[{index}:v:0]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v{index}]")
            filters.append(f"[{index}:a:0]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,aresample=48000[a{index}]")
        joined = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
        filters.append(f"{joined}concat=n={len(clips)}:v=1:a=1[video][audio]")
        args.extend(["-filter_complex", ";".join(filters), "-map", "[video]", "-map", "[audio]", "-c:v", "libx264", "-preset", "medium", "-c:a", "aac", "-movflags", "+faststart", output])
        self.merge.setEnabled(False); self.merge.setText("Merging…")
        self.statusBar().showMessage("Merging trimmed clips…")
        self.merge_process = QProcess(self)
        self.merge_process.finished.connect(self.merge_finished)
        self.merge_process.start(ffmpeg, args)

    def merge_finished(self, exit_code, _status):
        self.merge.setEnabled(True); self.merge.setText("Merge All Trims")
        if exit_code == 0:
            self.statusBar().showMessage("Merge complete", 5000)
            QMessageBox.information(self, "Merge complete", "Your merged video has been saved.")
        else:
            details = bytes(self.merge_process.readAllStandardError()).decode(errors="replace") if self.merge_process else ""
            QMessageBox.critical(self, "Merge failed", details[-1200:] or "FFmpeg could not merge these clips.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Videx")
    window = VidexWindow()
    window.show()
    sys.exit(app.exec())
