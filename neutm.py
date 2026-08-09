#!/usr/bin/env python3
import os
import re
import sys
import json
import string
import random
import argparse
import shutil

try:
    from PyQt6.QtCore import Qt, QSettings, QThread, pyqtSignal
    from PyQt6.QtGui import QColor, QAction
    from PyQt6.QtWidgets import (
        QApplication,
        QMainWindow,
        QWidget,
        QHBoxLayout,
        QVBoxLayout,
        QListWidget,
        QListWidgetItem,
        QLabel,
        QPushButton,
        QPlainTextEdit,
        QSplitter,
        QGroupBox,
        QFileDialog,
        QInputDialog,
        QMessageBox,
        QSizePolicy,
    )

    HAS_QT = True
except ImportError:
    HAS_QT = False


DISABLE_SUFFIX = ".disable"
AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".wma", ".opus")
PRESETS_DIRNAME = ".presets"


SHUFFLE_PREFIX_RE = re.compile(r"^\[n-[A-Za-z0-9]{4}\]-")


def is_disabled(path):
    return path.lower().endswith(DISABLE_SUFFIX)


def strip_disable_suffix(path):
    if is_disabled(path):
        return path[: -len(DISABLE_SUFFIX)]
    return path


def is_audio_file(filename):
    lower = strip_disable_suffix(filename.lower())
    return lower.endswith(AUDIO_EXTS)


def get_folders(base_dir="."):
    return sorted(
        f
        for f in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, f)) and not f.startswith(".")
    )


def get_mp3_files(folder):
    files = []
    for root, _dirs, filenames in os.walk(folder):
        for f in filenames:
            if is_audio_file(f):
                files.append(os.path.join(root, f))
    return files


def folder_status(folder):
    files = get_mp3_files(folder)
    if not files:
        return "empty"

    disabled_count = sum(1 for f in files if is_disabled(f))
    enabled_count = len(files) - disabled_count

    if disabled_count == len(files):
        return "disabled"
    elif enabled_count == len(files):
        return "enabled"
    else:
        return "mixed"


def folder_counts(folder):
    files = get_mp3_files(folder)
    disabled_count = sum(1 for f in files if is_disabled(f))
    enabled_count = len(files) - disabled_count
    return enabled_count, disabled_count


def disable_folder(folder):
    changed = 0
    for src in get_mp3_files(folder):
        if is_disabled(src):
            continue
        dst = src + DISABLE_SUFFIX
        os.rename(src, dst)
        changed += 1
    return changed


def enable_folder(folder):
    changed = 0
    for src in get_mp3_files(folder):
        if is_disabled(src):
            dst = strip_disable_suffix(src)
            os.rename(src, dst)
            changed += 1
    return changed


def enable_all(base_dir="."):
    changed = 0
    for folder in get_folders(base_dir):
        changed += enable_folder(os.path.join(base_dir, folder))
    return changed


def disable_all(base_dir="."):
    changed = 0
    for folder in get_folders(base_dir):
        changed += disable_folder(os.path.join(base_dir, folder))
    return changed


def toggle_track(path):
    if is_disabled(path):
        new_path = strip_disable_suffix(path)
    else:
        new_path = path + DISABLE_SUFFIX
    os.rename(path, new_path)
    return new_path


def gamble(base_dir="."):
    names = [
        f
        for f in get_folders(base_dir)
        if folder_status(os.path.join(base_dir, f)) != "empty"
    ]

    if len(names) < 2:
        return None

    while True:
        amount = random.randint(1, min(5, len(names)))
        chosen = random.sample(names, amount)
        changes = []

        for name in chosen:
            path = os.path.join(base_dir, name)
            status = folder_status(path)
            if status == "disabled":
                enable_folder(path)
                changes.append((name, "enabled"))
            elif status in ("enabled", "mixed"):
                disable_folder(path)
                changes.append((name, "disabled"))

        enabled = sum(
            folder_status(os.path.join(base_dir, n)) in ("enabled", "mixed")
            for n in names
        )

        if enabled >= 1:
            return changes

        for name, action in changes:
            path = os.path.join(base_dir, name)
            if action == "enabled":
                disable_folder(path)
            else:
                enable_folder(path)


SHUFFLE_CHARS = string.ascii_letters + string.digits


def random_shuffle_tag():
    return "".join(random.choices(SHUFFLE_CHARS, k=4))


def strip_shuffle_prefix(filename):
    return SHUFFLE_PREFIX_RE.sub("", filename)


def add_shuffle_prefix_to_file(path):
    dirpath, filename = os.path.split(path)
    base = strip_shuffle_prefix(filename)
    new_filename = f"[n-{random_shuffle_tag()}]-{base}"
    new_path = os.path.join(dirpath, new_filename)
    if new_path != path:
        os.rename(path, new_path)
    return new_path


def remove_shuffle_prefix_from_file(path):
    dirpath, filename = os.path.split(path)
    base = strip_shuffle_prefix(filename)
    if base == filename:
        return path
    new_path = os.path.join(dirpath, base)
    os.rename(path, new_path)
    return new_path


def shuffle_folder(folder):
    changed = 0
    for path in get_mp3_files(folder):
        add_shuffle_prefix_to_file(path)
        changed += 1
    return changed


def unshuffle_folder(folder):
    changed = 0
    for path in get_mp3_files(folder):
        _dirpath, filename = os.path.split(path)
        if SHUFFLE_PREFIX_RE.match(filename):
            remove_shuffle_prefix_from_file(path)
            changed += 1
    return changed


def shuffle_all(base_dir="."):
    changed = 0
    for folder in get_folders(base_dir):
        changed += shuffle_folder(os.path.join(base_dir, folder))
    return changed


def unshuffle_all(base_dir="."):
    changed = 0
    for folder in get_folders(base_dir):
        changed += unshuffle_folder(os.path.join(base_dir, folder))
    return changed


def canonical_key(path, base_dir="."):
    rel = os.path.relpath(path, base_dir)
    dirpath, filename = os.path.split(rel)
    filename = strip_disable_suffix(filename)
    filename = strip_shuffle_prefix(filename)
    key = os.path.join(dirpath, filename) if dirpath else filename
    return key.replace("\\", "/")


def build_preset(base_dir="."):
    preset = {}
    for folder in get_folders(base_dir):
        for path in get_mp3_files(os.path.join(base_dir, folder)):
            key = canonical_key(path, base_dir)
            enabled = not is_disabled(path)
            preset[key] = "enabled" if enabled else "disabled"
    return preset


def apply_preset(preset, base_dir="."):
    preset = {k.replace("\\", "/"): v for k, v in preset.items()}
    changed = 0
    seen_keys = set()
    for folder in get_folders(base_dir):
        for path in get_mp3_files(os.path.join(base_dir, folder)):
            key = canonical_key(path, base_dir)
            seen_keys.add(key)
            if key not in preset:
                continue
            desired = preset[key]
            currently_enabled = not is_disabled(path)
            if desired == "enabled" and not currently_enabled:
                os.rename(path, strip_disable_suffix(path))
                changed += 1
            elif desired == "disabled" and currently_enabled:
                os.rename(path, path + DISABLE_SUFFIX)
                changed += 1
    missing = [k for k in preset if k not in seen_keys]
    return changed, missing


def presets_dir(base_dir="."):
    d = os.path.join(base_dir, PRESETS_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def list_presets(base_dir="."):
    d = presets_dir(base_dir)
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def save_preset(name, base_dir="."):
    preset = build_preset(base_dir)
    path = os.path.join(presets_dir(base_dir), f"{name}.json")
    with open(path, "w") as f:
        json.dump(preset, f, indent=2, sort_keys=True)
    return path


def load_preset(name, base_dir="."):
    path = os.path.join(presets_dir(base_dir), f"{name}.json")
    with open(path) as f:
        return json.load(f)


def delete_preset(name, base_dir="."):
    path = os.path.join(presets_dir(base_dir), f"{name}.json")
    os.remove(path)


def export_preset_to_file(path, base_dir="."):
    preset = build_preset(base_dir)
    with open(path, "w") as f:
        json.dump(preset, f, indent=2, sort_keys=True)


def import_preset_from_file(path):
    with open(path) as f:
        return json.load(f)


def _unique_dest_path(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while True:
        candidate = f"{root} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def export_enabled_songs(dest_dir, base_dir=".", progress_cb=None):
    os.makedirs(dest_dir, exist_ok=True)
    dest_abs = os.path.abspath(dest_dir)

    to_copy = []
    for folder in get_folders(base_dir):
        folder_path = os.path.join(base_dir, folder)
        if os.path.abspath(folder_path) == dest_abs:
            continue
        for path in get_mp3_files(folder_path):
            if not is_disabled(path):
                to_copy.append(path)

    total = len(to_copy)
    copied = []
    for i, path in enumerate(to_copy, start=1):
        filename = strip_shuffle_prefix(os.path.basename(path))
        dest_path = _unique_dest_path(os.path.join(dest_dir, filename))
        shutil.copy2(path, dest_path)
        copied.append((path, dest_path))
        if progress_cb:
            progress_cb(i, total)
    return copied


def default_browse_start_dir():
    candidates = []
    if sys.platform.startswith("linux"):
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if user:
            candidates.append(f"/run/media/{user}")
            candidates.append(f"/media/{user}")
        candidates.append("/media")
        candidates.append("/mnt")
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return os.path.expanduser("~")


STATUS_LABELS = {
    "enabled": "Enabled",
    "disabled": "Disabled",
    "mixed": "Mixed",
    "empty": "Empty",
}


if HAS_QT:
    STATUS_META = {
        "enabled": (STATUS_LABELS["enabled"], QColor("#2e7d32")),
        "disabled": (STATUS_LABELS["disabled"], QColor("#c62828")),
        "mixed": (STATUS_LABELS["mixed"], QColor("#b8860b")),
        "empty": (STATUS_LABELS["empty"], QColor(128, 128, 128)),
    }


class ExportWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, dest_dir, base_dir):
        super().__init__()
        self.dest_dir = dest_dir
        self.base_dir = base_dir

    def run(self):
        try:
            copied = export_enabled_songs(
                self.dest_dir, self.base_dir, progress_cb=self._on_progress
            )
            self.finished_ok.emit(copied)
        except OSError as exc:
            self.failed.emit(str(exc))

    def _on_progress(self, done, total):
        self.progress.emit(done, total)


class SelectorWindow(QMainWindow):
    def __init__(self, initial_dir=None):
        super().__init__()
        self.settings = QSettings()

        remembered = self.settings.value("music_root_path", "", type=str)
        if initial_dir and os.path.isdir(initial_dir):
            self.base_dir = os.path.abspath(initial_dir)
        elif remembered and os.path.isdir(remembered):
            self.base_dir = remembered
        else:
            self.base_dir = None

        self.setWindowTitle("Neutm - Neutral Minus")
        self.resize(1050, 560)

        self._build_menu_bar()

        if self.base_dir is None:
            chosen = QFileDialog.getExistingDirectory(
                self,
                "Select your music folder (e.g. a USB drive)",
                default_browse_start_dir(),
            )
            self.base_dir = chosen if chosen else os.getcwd()

        self.settings.setValue("music_root_path", self.base_dir)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        self.folder_path_label = QLabel()
        self.folder_path_label.setStyleSheet("font-weight: bold;")
        root_layout.addWidget(self.folder_path_label)

        subtitle = QLabel(
            "Click a folder to preview its songs. Use the Enable/Disable button to toggle all songs in that folder."
        )
        subtitle.setStyleSheet("color: gray;")
        root_layout.addWidget(subtitle)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, stretch=1)

        left_box = QGroupBox("Folders")
        left_layout = QVBoxLayout(left_box)

        self.folder_list = QListWidget()
        self.folder_list.currentItemChanged.connect(self.on_folder_selected)
        left_layout.addWidget(self.folder_list, stretch=1)

        btn_row = QHBoxLayout()
        self.gamble_btn = QPushButton("Gamble")
        self.gamble_btn.clicked.connect(self.on_gamble)

        self.enable_all_btn = QPushButton("Enable All")
        self.enable_all_btn.clicked.connect(self.on_enable_all)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)

        btn_row.addWidget(self.gamble_btn)
        btn_row.addWidget(self.enable_all_btn)
        btn_row.addWidget(self.refresh_btn)
        left_layout.addLayout(btn_row)

        shuffle_box = QGroupBox("Alphabetical shuffle (for units that only sort A-Z)")
        shuffle_layout = QVBoxLayout(shuffle_box)
        shuffle_desc = QLabel(
            "Adds a random '[n-XXXX]-' tag to the front of every filename in every\n"
            "folder so that song order is randomised every time you shuffle."
        )
        shuffle_desc.setStyleSheet("color: gray;")
        shuffle_desc.setWordWrap(True)
        shuffle_desc.setMinimumWidth(0)
        shuffle_desc.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        shuffle_layout.addWidget(shuffle_desc)

        shuffle_btn_row = QHBoxLayout()
        self.shuffle_btn = QPushButton("Shuffle Order")
        self.shuffle_btn.clicked.connect(self.on_shuffle_all)

        self.unshuffle_btn = QPushButton("Remove Shuffle")
        self.unshuffle_btn.clicked.connect(self.on_unshuffle_all)

        shuffle_btn_row.addWidget(self.shuffle_btn)
        shuffle_btn_row.addWidget(self.unshuffle_btn)
        shuffle_layout.addLayout(shuffle_btn_row)

        left_layout.addWidget(shuffle_box)

        splitter.addWidget(left_box)

        right_box = QGroupBox("Selected folder")
        right_layout = QVBoxLayout(right_box)

        right_layout.addWidget(QLabel("Song list:"))
        self.song_list = QListWidget()
        self.song_list.currentItemChanged.connect(self.on_song_selected)
        self.song_list.itemDoubleClicked.connect(self.on_toggle_track)
        right_layout.addWidget(self.song_list, stretch=1)

        self.toggle_track_btn = QPushButton("Toggle Selected Track")
        self.toggle_track_btn.setEnabled(False)
        self.toggle_track_btn.clicked.connect(self.on_toggle_track)
        right_layout.addWidget(self.toggle_track_btn)

        right_layout.addWidget(QLabel("Activity log:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        right_layout.addWidget(self.log)

        splitter.addWidget(right_box)
        splitter.setSizes([480, 570])

        self.refresh()

    def log_msg(self, msg):
        self.log.appendPlainText(msg)

    def refresh(self):
        self._update_folder_label()

        selected_folder = None
        current = self.folder_list.currentItem()
        if current:
            selected_folder = current.data(Qt.ItemDataRole.UserRole)

        self.folder_list.clear()
        folders = get_folders(self.base_dir)

        if not folders:
            self.log_msg(f"No folders found in '{self.base_dir}'.")
            return

        select_index = None
        for i, folder in enumerate(folders):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, folder)
            self.folder_list.addItem(item)
            item.setSizeHint(self.build_folder_row(item, folder).sizeHint())
            if folder == selected_folder:
                select_index = i

        if select_index is not None:
            self.folder_list.setCurrentRow(select_index)
        elif self.folder_list.count() > 0:
            self.folder_list.setCurrentRow(0)

    def build_folder_row(self, item, folder):
        full_path = os.path.join(self.base_dir, folder)
        status = folder_status(full_path)
        label_text, color = STATUS_META[status]

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(8, 4, 8, 4)
        row_layout.setSpacing(6)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)

        name_label = QLabel(folder)
        name_label.setStyleSheet(f"color: {color.name()};")
        name_label.setMinimumWidth(0)
        name_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        text_col.addWidget(name_label)

        if status != "empty":
            enabled_count, disabled_count = folder_counts(full_path)
            total = enabled_count + disabled_count
            counts_label = QLabel(f"{enabled_count}/{total} enabled")
            counts_label.setStyleSheet("color: gray; font-size: 11px;")
            counts_label.setMinimumWidth(0)
            counts_label.setSizePolicy(
                QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
            )
            text_col.addWidget(counts_label)

        status_label = QLabel(f"({label_text})")
        status_label.setStyleSheet(f"color: {color.name()};")

        toggle_btn = QPushButton(
            "Disable" if status in ("enabled", "mixed") else "Enable"
        )
        toggle_btn.setMinimumWidth(0)
        toggle_btn.setEnabled(status != "empty")
        toggle_btn.clicked.connect(lambda _checked, f=folder: self.on_toggle_folder(f))

        row_layout.addLayout(text_col, stretch=1)
        row_layout.addWidget(status_label)
        row_layout.addWidget(toggle_btn)

        self.folder_list.setItemWidget(item, row)
        return row

    def show_songs(self, folder):
        self.song_list.clear()
        full_path = os.path.join(self.base_dir, folder)
        for path in get_mp3_files(full_path):
            enabled = not is_disabled(path)
            display_name = os.path.relpath(path, self.base_dir)
            item = QListWidgetItem(display_name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            if not enabled:
                item.setForeground(QColor(128, 128, 128))
            self.song_list.addItem(item)
        self.toggle_track_btn.setEnabled(False)

    def on_folder_selected(self, current, _previous):
        if current is None:
            self.song_list.clear()
            return
        folder = current.data(Qt.ItemDataRole.UserRole)
        self.show_songs(folder)

    def on_song_selected(self, current, _previous):
        self.toggle_track_btn.setEnabled(current is not None)

    def on_toggle_folder(self, folder):
        full_path = os.path.join(self.base_dir, folder)
        status = folder_status(full_path)

        if status in ("enabled", "mixed"):
            n = disable_folder(full_path)
            self.log_msg(f"Disabled '{folder}' ({n} file(s) renamed).")
        elif status == "disabled":
            n = enable_folder(full_path)
            self.log_msg(f"Enabled '{folder}' ({n} file(s) renamed).")

        self.refresh()

    def on_toggle_track(self, *_args):
        item = self.song_list.currentItem()
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        new_path = toggle_track(path)
        display_name = os.path.relpath(new_path, self.base_dir)
        action = "Disabled" if new_path.lower().endswith(DISABLE_SUFFIX) else "Enabled"
        self.log_msg(f"{action} track '{display_name}'.")
        self.refresh()

    def on_gamble(self):
        self.log_msg("Lets see what we get.")
        result = gamble(self.base_dir)
        if result is None:
            self.log_msg("Not enough folders to gamble. Spread out your songs vro...")
        else:
            self.log_msg(f"Gambled {len(result)} folder(s):")
            for folder, action in result:
                self.log_msg(f"  {action}: {folder}")
        self.refresh()

    def on_enable_all(self):
        changed = enable_all(self.base_dir)
        self.log_msg(f"Enabled all folders ({changed} file(s) restored).")
        self.refresh()

    def on_shuffle_all(self):
        changed = shuffle_all(self.base_dir)
        self.log_msg(f"Shuffled sort order for {changed} file(s) across all folders.")
        self.refresh()

    def on_unshuffle_all(self):
        changed = unshuffle_all(self.base_dir)
        self.log_msg(f"Removed shuffle tags from {changed} file(s).")
        self.refresh()

    def _build_menu_bar(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")

        change_folder_action = QAction("Change Music Folder...", self)
        change_folder_action.triggered.connect(self.on_change_music_folder)
        file_menu.addAction(change_folder_action)

        file_menu.addSeparator()

        save_action = QAction("Save Preset...", self)
        save_action.triggered.connect(self.on_save_preset)
        file_menu.addAction(save_action)

        self.load_preset_menu = file_menu.addMenu("Load Preset")
        self.load_preset_menu.aboutToShow.connect(self._populate_load_preset_menu)

        self.delete_preset_menu = file_menu.addMenu("Delete Preset")
        self.delete_preset_menu.aboutToShow.connect(self._populate_delete_preset_menu)

        file_menu.addSeparator()

        export_action = QAction("Export Preset to File...", self)
        export_action.triggered.connect(self.on_export_preset)
        file_menu.addAction(export_action)

        import_action = QAction("Import Preset from File...", self)
        import_action.triggered.connect(self.on_import_preset)
        file_menu.addAction(import_action)

        file_menu.addSeparator()

        self.export_songs_action = QAction("Export Enabled Songs to Folder...", self)
        self.export_songs_action.triggered.connect(self.on_export_enabled_songs)
        file_menu.addAction(self.export_songs_action)

        file_menu.addSeparator()

        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _populate_load_preset_menu(self):
        self.load_preset_menu.clear()
        names = list_presets(self.base_dir)
        if not names:
            empty_action = QAction("(no saved presets)", self)
            empty_action.setEnabled(False)
            self.load_preset_menu.addAction(empty_action)
            return
        for name in names:
            action = QAction(name, self)
            action.triggered.connect(lambda _checked, n=name: self.on_load_preset(n))
            self.load_preset_menu.addAction(action)

    def _populate_delete_preset_menu(self):
        self.delete_preset_menu.clear()
        names = list_presets(self.base_dir)
        if not names:
            empty_action = QAction("(no saved presets)", self)
            empty_action.setEnabled(False)
            self.delete_preset_menu.addAction(empty_action)
            return
        for name in names:
            action = QAction(name, self)
            action.triggered.connect(lambda _checked, n=name: self.on_delete_preset(n))
            self.delete_preset_menu.addAction(action)

    def on_save_preset(self):
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        path = save_preset(name, self.base_dir)
        self.log_msg(f"Saved preset '{name}' -> {path}")

    def on_load_preset(self, name):
        try:
            preset = load_preset(name, self.base_dir)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(
                self, "Load Preset", f"Could not load preset '{name}':\n{exc}"
            )
            return
        changed, missing = apply_preset(preset, self.base_dir)
        msg = f"Applied preset '{name}': {changed} file(s) changed."
        if missing:
            msg += f" {len(missing)} track(s) in the preset were not found on disk."
        self.log_msg(msg)
        self.refresh()

    def on_delete_preset(self, name):
        confirm = QMessageBox.question(
            self,
            "Delete Preset",
            f"Delete preset '{name}'? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        delete_preset(name, self.base_dir)
        self.log_msg(f"Deleted preset '{name}'.")

    def on_export_preset(self):
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export Preset", "preset.json", "JSON files (*.json)"
        )
        if not path:
            return
        export_preset_to_file(path, self.base_dir)
        self.log_msg(f"Exported current enable/disable state to '{path}'.")

    def on_import_preset(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import Preset", "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            preset = import_preset_from_file(path)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(
                self, "Import Preset", f"Could not read preset file:\n{exc}"
            )
            return
        changed, missing = apply_preset(preset, self.base_dir)
        msg = f"Imported preset from '{path}': {changed} file(s) changed."
        if missing:
            msg += f" {len(missing)} track(s) in the preset were not found on disk."
        self.log_msg(msg)
        self.refresh()

    def on_export_enabled_songs(self):
        dest = QFileDialog.getExistingDirectory(
            self,
            "Choose destination folder for enabled songs",
            default_browse_start_dir(),
        )
        if not dest:
            return
        if os.path.abspath(dest) == os.path.abspath(self.base_dir):
            QMessageBox.warning(
                self,
                "Export Enabled Songs",
                "Destination can't be the same as your music root folder.",
            )
            return

        self.export_songs_action.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.log_msg(f"Exporting enabled songs to '{dest}'...")

        self._export_worker = ExportWorker(dest, self.base_dir)
        self._export_worker.progress.connect(self._on_export_progress)
        self._export_worker.finished_ok.connect(
            lambda copied: self._on_export_finished(dest, copied)
        )
        self._export_worker.failed.connect(self._on_export_failed)
        self._export_worker.start()

    def _on_export_progress(self, done, total):
        self.log_msg(f"Exporting... {done}/{total} song(s) copied.")

    def _on_export_finished(self, dest, copied):
        QApplication.restoreOverrideCursor()
        self.export_songs_action.setEnabled(True)
        self.log_msg(f"Exported {len(copied)} enabled song(s) to '{dest}'.")
        QMessageBox.information(
            self,
            "Export Enabled Songs",
            f"Copied {len(copied)} enabled song(s) into:\n{dest}",
        )

    def _on_export_failed(self, message):
        QApplication.restoreOverrideCursor()
        self.export_songs_action.setEnabled(True)
        self.log_msg(f"Export failed: {message}")
        QMessageBox.warning(self, "Export Enabled Songs", f"Export failed:\n{message}")

    def on_change_music_folder(self):
        start_dir = (
            self.base_dir
            if os.path.isdir(self.base_dir)
            else default_browse_start_dir()
        )
        chosen = QFileDialog.getExistingDirectory(
            self, "Select Music Folder", start_dir
        )
        if not chosen or os.path.abspath(chosen) == os.path.abspath(self.base_dir):
            return
        self.base_dir = chosen
        self.settings.setValue("music_root_path", self.base_dir)
        self.log_msg(f"Music folder changed to '{self.base_dir}'.")
        self.refresh()

    def _update_folder_label(self):
        self.folder_path_label.setText(f"Music folder: {self.base_dir}")
        folder_name = os.path.basename(self.base_dir.rstrip(os.sep)) or self.base_dir
        self.setWindowTitle(f"Neutm - {folder_name}")

    def closeEvent(self, event):
        self.settings.setValue("window_geometry", self.saveGeometry())
        event.accept()


def cli():
    parser = argparse.ArgumentParser(
        prog="neutm",
        description="Manage music folders for USB players",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
        Enable a folder:
            neutm /media/USB --enable-folder Track01

        Disable everything:
            neutm /media/USB --disable-all

        Load a playlist setup:
            neutm /media/USB --load-preset Playlist4
        """,
    )

    parser.add_argument("directory", nargs="?", default=".", help="Music directory")

    info_group = parser.add_argument_group("Information")

    info_group.add_argument(
        "--status", action="store_true", help="Show folder statuses"
    )

    toggle_group = parser.add_argument_group("Enable/Disable")

    toggle_group.add_argument(
        "--toggle-track", metavar="TRACK", help="Enable/disable a single track"
    )

    toggle_group.add_argument(
        "--enable-folder", metavar="FOLDER", help="Enable all tracks in a folder"
    )

    toggle_group.add_argument(
        "--disable-folder", metavar="FOLDER", help="Disable all tracks in a folder"
    )

    toggle_group.add_argument(
        "--enable-all", action="store_true", help="Enable all disabled tracks"
    )

    toggle_group.add_argument(
        "--disable-all", action="store_true", help="Disable all tracks"
    )

    random_group = parser.add_argument_group("Randomisation")

    random_group.add_argument(
        "--shuffle", action="store_true", help="Add random shuffle prefixes"
    )

    random_group.add_argument(
        "--unshuffle", action="store_true", help="Remove shuffle prefixes"
    )

    random_group.add_argument(
        "--gamble", action="store_true", help="Randomly enable/disable folders"
    )

    preset_group = parser.add_argument_group("Presets")

    preset_group.add_argument(
        "--save-preset", metavar="NAME", help="Save current state as preset"
    )

    preset_group.add_argument("--load-preset", metavar="NAME", help="Load a preset")

    preset_group.add_argument(
        "--list-presets", action="store_true", help="List available presets"
    )

    export_group = parser.add_argument_group("Export")

    export_group.add_argument(
        "--export-enabled",
        metavar="DEST",
        help="Copy all currently-enabled songs (from every folder) flat into DEST",
    )

    args = parser.parse_args()

    base = args.directory

    if args.status:
        print(f"\nMusic Library: {os.path.abspath(base)}")
        print("-" * 30)

        for folder in get_folders(base):
            path = os.path.join(base, folder)
            status = STATUS_LABELS[folder_status(path)].upper()
            print(f"[{status:<8}] {folder}")

    elif args.enable_all:
        changed = enable_all(base)
        print(f"[OK] Enabled {changed} files")

    elif args.disable_all:
        changed = disable_all(base)
        print(f"[OK] Disabled {changed} files")

    elif args.shuffle:
        changed = shuffle_all(base)
        print(f"[OK] Shuffled {changed} files")

    elif args.unshuffle:
        changed = unshuffle_all(base)
        print(f"[OK] Removed shuffle tags from {changed} files")

    elif args.toggle_track:
        path = os.path.join(base, args.toggle_track)

        if not os.path.isfile(path):
            print(f"[ERROR] File not found: {args.toggle_track}")
            return

        new_path = toggle_track(path)
        print(f"[OK] Toggled: {new_path}")

    elif args.gamble:
        print("Gambling folders...")

        result = gamble(base)

        if result:
            print("\nChanges:")
            for folder, action in result:
                print(f"  {action.upper():<8} {folder}")

            print(f"\n[OK] Gambled {len(result)} folder(s)")
        else:
            print("[ERROR] Not enough folders to gamble")

    elif args.enable_folder:
        folder = os.path.join(base, args.enable_folder)

        if not os.path.isdir(folder):
            print(f"[ERROR] Folder not found: {args.enable_folder}")
            return

        changed = enable_folder(folder)
        print(f"[OK] Enabled {changed} files in '{args.enable_folder}'")

    elif args.disable_folder:
        folder = os.path.join(base, args.disable_folder)

        if not os.path.isdir(folder):
            print(f"[ERROR] Folder not found: {args.disable_folder}")
            return

        changed = disable_folder(folder)
        print(f"[OK] Disabled {changed} files in '{args.disable_folder}'")

    elif args.save_preset:
        path = save_preset(args.save_preset, base)
        print(f"[OK] Saved preset '{args.save_preset}'")

    elif args.load_preset:
        try:
            preset = load_preset(args.load_preset, base)
        except FileNotFoundError:
            print(f"[ERROR] Preset not found: {args.load_preset}")
            return

        changed, missing = apply_preset(preset, base)

        print(f"[OK] Loaded preset '{args.load_preset}'")
        print(f"Changed: {changed} files")

        if missing:
            print(f"[WARNING] Missing {len(missing)} tracks")

    elif args.list_presets:
        presets = list_presets(base)

        if not presets:
            print("No presets found.")
        else:
            print("Presets:")
            for preset in presets:
                print(f"  - {preset}")

    elif args.export_enabled:
        dest = args.export_enabled
        if os.path.abspath(dest) == os.path.abspath(base):
            print("[ERROR] Destination can't be the same as the music directory")
            return
        copied = export_enabled_songs(dest, base)
        print(f"[OK] Exported {len(copied)} enabled song(s) to '{dest}'")

    else:
        parser.print_help()

    print("\n")


def main():
    cli_flags = {
        "-h",
        "--help",
        "--status",
        "--enable-folder",
        "--disable-folder",
        "--toggle-track",
        "--enable-all",
        "--disable-all",
        "--shuffle",
        "--unshuffle",
        "--gamble",
        "--save-preset",
        "--load-preset",
        "--list-presets",
        "--export-enabled",
    }

    if any(arg.startswith("-") and arg in cli_flags for arg in sys.argv):
        cli()
        return

    initial_dir = sys.argv[1] if len(sys.argv) > 1 else None

    app = QApplication(sys.argv)
    app.setOrganizationName("Neutral-")
    app.setApplicationName("Neutral-")

    window = SelectorWindow(initial_dir)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
