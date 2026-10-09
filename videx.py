"""Videx — a focused PyQt6 video trimming editor."""
from __future__ import annotations

import shutil
import sys
import tempfile
from hashlib import sha1
from pathlib import Path

from PyQt6.QtCore import QProcess, QSize, QTime, QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QPainter, QPen, QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QStatusBar, QStyle, QToolButton, QVBoxLayout, QWidget, QSplashScreen


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


def parse_time(text):
    """Convert seconds, mm:ss, or hh:mm:ss input to milliseconds."""
    try:
        parts = [float(part) for part in text.strip().split(":")]
        if not 1 <= len(parts) <= 3 or any(part < 0 for part in parts):
            raise ValueError
        seconds = parts[-1]
        if len(parts) >= 2:
            seconds += parts[-2] * 60
        if len(parts) == 3:
            seconds += parts[0] * 3600
        return round(seconds * 1000)
    except ValueError as error:
        raise ValueError("Use seconds, mm:ss.xx, or hh:mm:ss.xx.") from error


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
        self.details.setText(f"{time_text(start)} — {time_text(end)} | {time_text(max(0, end - start))}")


class VidexWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Videx — Video Trimmer")
        self.setWindowState(Qt.WindowState.WindowMaximized)
        self.current_file = None
        self.active_clip_id = None
        self.clips = {}
        self.clip_items = {}
        self.preview_file = None
        self.preview_process = None
        self.selection_playback = False
        self.export_process = None
        self.export_output_path = None
        self.export_destination = None
        self.export_cancelled = False
        self.merge_process = None
        self.active_job_duration_ms = 0
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
        self.player.metaDataChanged.connect(self.on_metadata_changed)
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
        crop_row = QHBoxLayout()
        self.crop_label = QLabel("CROP  Original")
        self.crop_label.setObjectName("cropLabel")
        crop_reset = QPushButton("Reset")
        crop_16_9 = QPushButton("16:9")
        crop_4_3 = QPushButton("4:3")
        crop_square = QPushButton("1:1")
        crop_portrait = QPushButton("9:16")
        crop_top_95 = QPushButton("Top 95%")
        crop_top_95.setToolTip("Set crop to X 0%, Y 0%, W 100%, H 95%")
        self.freeform = QPushButton("Freeform")
        crop_reset.clicked.connect(lambda: self.set_crop_ratio(None))
        crop_16_9.clicked.connect(lambda: self.set_crop_ratio(16 / 9))
        crop_4_3.clicked.connect(lambda: self.set_crop_ratio(4 / 3))
        crop_square.clicked.connect(lambda: self.set_crop_ratio(1))
        crop_portrait.clicked.connect(lambda: self.set_crop_ratio(9 / 16))
        crop_top_95.clicked.connect(lambda: self.set_custom_crop(0, 0, 100, 95))
        self.freeform.clicked.connect(self.toggle_freeform)
        self.freeform_inputs = []
        for index, text in enumerate(("X", "Y", "W", "H")):
            spin = QSpinBox()
            spin.setPrefix(f"{text} ")
            spin.setSuffix("%")
            spin.setRange(0, 98) if index < 2 else spin.setRange(2, 100)
            spin.setFixedWidth(70)
            spin.setVisible(False)
            spin.valueChanged.connect(self.set_freeform_crop)
            self.freeform_inputs.append(spin)
            crop_row.addWidget(spin)
        crop_row.addWidget(self.crop_label); crop_row.addStretch(); crop_row.addWidget(QLabel("Crop")); crop_row.addWidget(crop_reset); crop_row.addWidget(crop_16_9); crop_row.addWidget(crop_4_3); crop_row.addWidget(crop_square); crop_row.addWidget(crop_portrait); crop_row.addWidget(crop_top_95); crop_row.addWidget(self.freeform)
        c_layout.addLayout(crop_row)
        row = QHBoxLayout()
        self.time_label = QLabel("00:00.00 / 00:00.00")
        in_caption = QLabel("IN")
        out_caption = QLabel("OUT")
        self.in_field = QLineEdit("00:00.00")
        self.out_field = QLineEdit("00:00.00")
        for field in (self.in_field, self.out_field):
            field.setFixedWidth(92)
            field.setToolTip("Enter seconds, mm:ss.xx, or hh:mm:ss.xx")
            field.editingFinished.connect(self.apply_trim_time)
        set_in = QPushButton("Set In")
        set_out = QPushButton("Set Out")
        set_in.setToolTip("Set the in point to the current playhead position")
        set_out.setToolTip("Set the out point to the current playhead position")
        set_in.clicked.connect(self.set_in_from_playhead)
        set_out.clicked.connect(self.set_out_from_playhead)
        self.play = QToolButton()
        self.play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play.clicked.connect(self.toggle_play)
        play_selection = QPushButton("Play Selection")
        play_selection.clicked.connect(self.play_trim)
        self.export = QPushButton("Export Trim")
        self.export.setObjectName("export")
        self.export.clicked.connect(self.export_trim)
        row.addWidget(self.time_label); row.addStretch(); row.addWidget(in_caption); row.addWidget(self.in_field); row.addWidget(set_in); row.addWidget(out_caption); row.addWidget(self.out_field); row.addWidget(set_out); row.addWidget(self.play); row.addWidget(play_selection); row.addWidget(self.export)
        c_layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setObjectName("progress")
        self.progress.setFormat("Preparing export…")
        self.progress.setVisible(False)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress, 1)
        self.cancel_export = QPushButton("Cancel")
        self.cancel_export.setVisible(False)
        self.cancel_export.clicked.connect(self.cancel_export_job)
        progress_row.addWidget(self.cancel_export)
        c_layout.addLayout(progress_row)
        editor_layout.addWidget(controls)
        workspace.addWidget(editor, 1)
        layout.addLayout(workspace, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Open a video to start trimming")

    def apply_style(self):
        self.setStyleSheet("""
          QMainWindow { background: #121722; color: #e8edf8; } QLabel { color: #c8d1e2; font-size: 12px; }
          QLabel#logo { font-size: 20px; color: #e0720c; font-weight: 800; letter-spacing: 3px; } 
          QLabel#subtitle { color: #a45409; font-size: 10px; letter-spacing: 2px; }
          QLabel#sideTitle { font-size: 12px; font-weight: 700; letter-spacing: 1px; } 
          QLabel#sideHint, QLabel#clipDetails { color: #a9bbd7; font-size: 10px; font-weight: 500; } 
          QLabel#clipName { color: #ecf1fc; font-weight: 600; }
          QVideoWidget { background: #090c12; border-radius: 10px; } 
          QFrame#controls, QFrame#sidebar { background: #1a2130; border: 1px solid #2a3448; border-radius: 10px; }
          QListWidget#clipList { background: transparent; border: none; outline: none; } 
          QListWidget#clipList::item { margin: 3px 0; border-radius: 6px; } QListWidget#clipList::item:selected { background: #2b4269; }
          QPushButton, QToolButton, QLineEdit { background: #273247; border: none; border-radius: 6px; padding: 8px 12px; color: #e8edf8; } 
          QLineEdit:focus { border: 1px solid #438cff; } QPushButton:hover, QToolButton:hover { background: #384c75; }
          QToolButton#deleteClip { background: transparent; color: #9eacc2; font-size: 18px; padding: 1px 5px; } 
          QToolButton#deleteClip:hover { color: #ff8794; background: #372731; }
          QPushButton#export, QPushButton#merge { background: #26ff88; font-weight: 600; color: #090000; } 
          QPushButton#export:hover, QPushButton#merge:hover { background: #00ff11; } 
          QProgressBar#progress { border: 1px solid #34435c; border-radius: 4px; height: 9px; text-align: center; color: #090000; font-weight: 700; } 
          QProgressBar#progress::chunk { background: #2676ff; border-radius: 3px; } 
          QStatusBar { color: #91a0b8; background: #121722; }
        """)

    def open_video(self):
        filenames, _ = QFileDialog.getOpenFileNames(self, "Add videos", "", "Video files (*.mp4 *.mov *.mkv *.avi *.webm *.m4v);;All files (*)")
        for filename in filenames:
            self.add_clip(Path(filename))

    def add_clip(self, source):
        clip_id = sha1(f"{source.resolve()}:{len(self.clips)}".encode()).hexdigest()
        clip = {"id": clip_id, "source": source, "start": 0, "end": 0, "duration": 0, "size": None, "crop": (0.0, 0.0, 1.0, 1.0)}
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
        self.update_crop_label()
        self.sync_freeform_inputs()
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

    def on_metadata_changed(self):
        clip = self.active_clip()
        if clip:
            size = self.player.metaData().value(QMediaMetaData.Key.Resolution)
            if size and size.width() and size.height():
                clip["size"] = (size.width(), size.height())
                self.update_crop_label()

    def set_crop_ratio(self, ratio):
        clip = self.active_clip()
        if not clip:
            return
        if ratio is None:
            clip["crop"] = (0.0, 0.0, 1.0, 1.0)
        elif not clip["size"]:
            QMessageBox.information(self, "Video is loading", "Wait for the video preview to load before choosing a crop ratio.")
            return
        else:
            source_ratio = clip["size"][0] / clip["size"][1]
            width, height = (ratio / source_ratio, 1.0) if source_ratio > ratio else (1.0, source_ratio / ratio)
            clip["crop"] = ((1 - width) / 2, (1 - height) / 2, width, height)
        self.update_crop_label()
        self.sync_freeform_inputs()

    def toggle_freeform(self):
        visible = not self.freeform_inputs[0].isVisible()
        for spin in self.freeform_inputs:
            spin.setVisible(visible)
        self.freeform.setText("Hide Fields" if visible else "Freeform")
        if visible:
            self.sync_freeform_inputs()

    def set_custom_crop(self, x, y, width, height):
        clip = self.active_clip()
        if not clip:
            return
        clip["crop"] = (x / 100, y / 100, width / 100, height / 100)
        self.update_crop_label()
        self.sync_freeform_inputs()

    def sync_freeform_inputs(self):
        clip = self.active_clip()
        if not clip:
            return
        values = [round(value * 100) for value in clip["crop"]]
        for spin, value in zip(self.freeform_inputs, values):
            old = spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(old)

    def set_freeform_crop(self, _value):
        clip = self.active_clip()
        if not clip:
            return
        x, y, width, height = (spin.value() for spin in self.freeform_inputs)
        width = max(2, min(width, 100 - x))
        height = max(2, min(height, 100 - y))
        clip["crop"] = (x / 100, y / 100, width / 100, height / 100)
        self.update_crop_label()

    def update_crop_label(self):
        clip = self.active_clip()
        if not clip:
            return
        _x, _y, width, height = clip["crop"]
        self.crop_label.setText("CROP  Original" if width == 1 and height == 1 else f"CROP  {width:.0%} × {height:.0%}")

    def crop_filter(self, clip):
        x, y, width, height = clip["crop"]
        if (x, y, width, height) == (0.0, 0.0, 1.0, 1.0):
            return ""
        return f"crop=trunc(iw*{width:.8f}/2)*2:trunc(ih*{height:.8f}/2)*2:trunc(iw*{x:.8f}/2)*2:trunc(ih*{y:.8f}/2)*2"

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
        if not self.in_field.hasFocus():
            self.in_field.setText(time_text(self.timeline.start))
        if not self.out_field.hasFocus():
            self.out_field.setText(time_text(self.timeline.end))

    def apply_trim_time(self):
        field = self.sender()
        if not self.timeline.duration or field not in (self.in_field, self.out_field):
            return
        try:
            value = min(parse_time(field.text()), self.timeline.duration)
        except ValueError as error:
            QMessageBox.warning(self, "Invalid trim time", str(error))
            self.update_labels()
            return
        if field is self.in_field:
            if value >= self.timeline.end:
                QMessageBox.warning(self, "Invalid in point", "The in point must be before the out point.")
                self.update_labels()
                return
            self.timeline.set_range(value, self.timeline.end)
        else:
            if value <= self.timeline.start:
                QMessageBox.warning(self, "Invalid out point", "The out point must be after the in point.")
                self.update_labels()
                return
            self.timeline.set_range(self.timeline.start, value)
        field.setText(time_text(value))

    def set_in_from_playhead(self):
        position = self.player.position()
        if position >= self.timeline.end:
            QMessageBox.warning(self, "Invalid in point", "Move the playhead before the out point first.")
            return
        self.timeline.set_range(position, self.timeline.end)
        self.in_field.setText(time_text(position))

    def set_out_from_playhead(self):
        position = self.player.position()
        if position <= self.timeline.start:
            QMessageBox.warning(self, "Invalid out point", "Move the playhead after the in point first.")
            return
        self.timeline.set_range(self.timeline.start, position)
        self.out_field.setText(time_text(position))

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

    def start_progress(self, duration_ms, label):
        self.active_job_duration_ms = max(1, duration_ms)
        self.progress.setValue(0)
        self.progress.setFormat(f"{label}  %p%")
        self.progress.setVisible(True)

    def consume_progress(self, process):
        """Read FFmpeg's machine-readable progress stream from stdout."""
        if not process:
            return
        for line in bytes(process.readAllStandardOutput()).decode(errors="replace").splitlines():
            if line.startswith(("out_time_ms=", "out_time_us=")):
                try:
                    # FFmpeg names this field ms, but emits microseconds.
                    elapsed_ms = int(line.split("=", 1)[1]) / 1000
                    self.progress.setValue(min(99, round(elapsed_ms * 100 / self.active_job_duration_ms)))
                except ValueError:
                    pass

    def finish_progress(self, successful):
        self.progress.setValue(100 if successful else 0)
        self.progress.setVisible(False)
        self.cancel_export.setVisible(False)

    def cancel_export_job(self):
        if not self.export_process or self.export_process.state() == QProcess.ProcessState.NotRunning:
            return
        self.export_cancelled = True
        self.cancel_export.setEnabled(False)
        self.cancel_export.setText("Cancelling…")
        self.export_process.terminate()
        QTimer.singleShot(1500, self.kill_export_if_running)

    def kill_export_if_running(self):
        if self.export_cancelled and self.export_process and self.export_process.state() != QProcess.ProcessState.NotRunning:
            self.export_process.kill()

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
        self.export_destination = Path(output)
        with tempfile.NamedTemporaryFile(prefix="videx-export-", suffix=".mp4", dir=str(self.export_destination.parent), delete=False) as temp_output:
            self.export_output_path = Path(temp_output.name)
        self.export_cancelled = False
        self.cancel_export.setText("Cancel")
        self.cancel_export.setEnabled(True)
        self.cancel_export.setVisible(True)
        self.export.setEnabled(False); self.export.setText("Exporting…")
        self.statusBar().showMessage("Exporting trim with FFmpeg…")
        self.start_progress(self.timeline.end - self.timeline.start, "Exporting trim")
        self.export_process = QProcess(self)
        self.export_process.readyReadStandardOutput.connect(lambda: self.consume_progress(self.export_process))
        self.export_process.finished.connect(self.export_finished)
        args = ["-y", "-ss", f"{self.timeline.start / 1000:.3f}", "-i", str(self.current_file), "-t", f"{(self.timeline.end - self.timeline.start) / 1000:.3f}"]
        if crop := self.crop_filter(self.active_clip()):
            args.extend(["-vf", crop])
        args.extend(["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(self.export_output_path)])
        self.export_process.start(ffmpeg, args)

    def export_finished(self, exit_code, _status):
        self.export.setEnabled(True); self.export.setText("Export Trim")
        successful = exit_code == 0 and not self.export_cancelled
        details = ""
        if successful:
            try:
                self.export_output_path.replace(self.export_destination)
            except OSError as error:
                successful = False
                details = str(error)
        if self.export_output_path and self.export_output_path.exists():
            self.export_output_path.unlink(missing_ok=True)
        self.finish_progress(successful)
        if self.export_cancelled:
            self.statusBar().showMessage("Export canceled", 5000)
        elif successful:
            self.statusBar().showMessage("Export complete", 5000)
            QMessageBox.information(self, "Export complete", "Your trimmed video has been saved.")
        else:
            details = details or (bytes(self.export_process.readAllStandardError()).decode(errors="replace") if self.export_process else "")
            QMessageBox.critical(self, "Export failed", details[-1200:] or "FFmpeg could not export this video.")
        self.export_cancelled = False
        self.cancel_export.setText("Cancel")

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
            crop = self.crop_filter(clip)
            crop_prefix = f"{crop}," if crop else ""
            filters.append(f"[{index}:v:0]{crop_prefix}trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v{index}]")
            filters.append(f"[{index}:a:0]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,aresample=48000[a{index}]")
        joined = "".join(f"[v{i}][a{i}]" for i in range(len(clips)))
        filters.append(f"{joined}concat=n={len(clips)}:v=1:a=1[video][audio]")
        args.extend(["-filter_complex", ";".join(filters), "-map", "[video]", "-map", "[audio]", "-c:v", "libx264", "-preset", "medium", "-c:a", "aac", "-movflags", "+faststart", output])
        self.merge.setEnabled(False); self.merge.setText("Merging…")
        self.statusBar().showMessage("Merging trimmed clips…")
        self.start_progress(sum(clip["end"] - clip["start"] for clip in clips), "Merging clips")
        self.merge_process = QProcess(self)
        self.merge_process.readyReadStandardOutput.connect(lambda: self.consume_progress(self.merge_process))
        self.merge_process.finished.connect(self.merge_finished)
        self.merge_process.start(ffmpeg, args[:-1] + ["-progress", "pipe:1", "-nostats", args[-1]])

    def merge_finished(self, exit_code, _status):
        self.merge.setEnabled(True); self.merge.setText("Merge All Trims")
        self.finish_progress(exit_code == 0)
        if exit_code == 0:
            self.statusBar().showMessage("Merge complete", 5000)
            QMessageBox.information(self, "Merge complete", "Your merged video has been saved.")
        else:
            details = bytes(self.merge_process.readAllStandardError()).decode(errors="replace") if self.merge_process else ""
            QMessageBox.critical(self, "Merge failed", details[-1200:] or "FFmpeg could not merge these clips.")


if __name__ == "__main__":
    input_paths = sys.argv[1:]
    app = QApplication(sys.argv)
    app.setApplicationName("Videx")
    
    pixmap = QPixmap("videx.png").scaled(500, 500, Qt.AspectRatioMode.KeepAspectRatioByExpanding)
    splash = QSplashScreen(pixmap)
    splash.show()
    
    app.processEvents()
    
    window = VidexWindow()
    window.show()
    for arguments in input_paths:
        video_path = Path(arguments).expanduser()
        if video_path.is_file():
            window.add_clip(video_path)
    splash.finish(window)
    sys.exit(app.exec())