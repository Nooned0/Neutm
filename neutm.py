#!/usr/bin/env python3
import os
import re
import sys
import json
import time
import base64
import math
from array import array
import string
import random
import hashlib
import argparse
import shutil
import tempfile
import atexit
import threading
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
from importlib.resources import files


try:
    from PyQt6.QtCore import Qt, QSettings, QThread, pyqtSignal, QTimer, QUrl, QRectF, QBuffer, QIODevice, QSize
    from PyQt6.QtGui import (
        QColor,
        QAction,
        QActionGroup,
        QIcon,
        QKeySequence,
        QPainter,
        QPixmap,
        QImage,
        QMovie,
    )
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
        QToolButton,
        QPlainTextEdit,
        QSplitter,
        QGroupBox,
        QFileDialog,
        QInputDialog,
        QMessageBox,
        QMenu,
        QSizePolicy,
        QTreeWidget,
        QTreeWidgetItem,
        QTreeWidgetItemIterator,
        QSlider,
        QStyle,
        QStyleOptionSlider,
        QDialog,
        QDialogButtonBox,
        QLineEdit,
        QFormLayout,
        QCheckBox,
        QHeaderView,
    )

    HAS_QT = True

except ImportError:
    HAS_QT = False

try:
    from PyQt6.QtMultimedia import (
        QMediaPlayer,
        QAudioOutput,
        QAudioDecoder,
        QAudioFormat,
        QMediaMetaData,
    )

    HAS_MULTIMEDIA = True
except ImportError:
    HAS_MULTIMEDIA = False

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import mutagen
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.flac import FLAC, Picture
    from mutagen.asf import ASF
    from mutagen.id3 import ID3, APIC

    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False


DISABLE_SUFFIX = ".disable"
AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".wma", ".opus")
PRESETS_DIRNAME = ".presets"
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

SONG_THUMB = 40
SONG_VIEWS = (
    ("default", "Default (file paths)"),
    ("compact", "Compact (title, artist, time)"),
    ("spaced", "Spaced (with cover art)"),
)
SONG_COLUMNS = (
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("time", "Time"),
    ("path", "File path"),
)
SONG_DEFAULT_COLUMNS = {
    "default": ["path"],
    "compact": ["title", "artist", "time"],
    "spaced": ["title", "artist", "time"],
}


SHUFFLE_PREFIX_RE = re.compile(r"^\[n-[A-Za-z0-9]{4}\]-")


UNDO_LIMIT = 20
_undo_stack = []
_current_action = None


def tracked_rename(src, dst):
    os.rename(src, dst)
    if _current_action is not None:
        _current_action["moves"].append((src, dst))


class record_action:
    def __init__(self, label):
        self.label = label
        self._outer = None

    def __enter__(self):
        global _current_action
        self._outer = _current_action
        if self._outer is None:
            _current_action = {"label": self.label, "moves": [], "tags": []}
        return self

    def __exit__(self, *_exc):
        global _current_action
        if self._outer is None:
            action = _current_action
            _current_action = None
            if action["moves"] or action["tags"]:
                _undo_stack.append(action)
                del _undo_stack[:-UNDO_LIMIT]
        return False


def record_tag_change(path, old_values=None, old_cover=None):
    if _current_action is not None:
        _current_action["tags"].append((path, old_values, old_cover))


def undo_label():
    return _undo_stack[-1]["label"] if _undo_stack else None


def undo_last():
    if not _undo_stack:
        return None
    action = _undo_stack.pop()
    restored = skipped = 0
    for src, dst in reversed(action["moves"]):
        if os.path.exists(dst) and not os.path.exists(src):
            try:
                os.rename(dst, src)
                restored += 1
            except OSError:
                skipped += 1
        else:
            skipped += 1
    for path, old, cover in reversed(action.get("tags", [])):
        try:
            if old is not None:
                write_tags(path, old)
            if cover is not None:
                write_cover(path, cover[0], cover[1] or "image/jpeg")
            restored += 1
        except TagEditError:
            skipped += 1
    return action["label"], restored, skipped


def undoable(label, with_args=False):
    def deco(fn):
        def wrapper(self, *args):
            with record_action(label):
                result = fn(self, *(args if with_args else ()))
            self._update_undo_action()
            return result

        wrapper.__name__ = fn.__name__
        return wrapper

    return deco


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
        tracked_rename(src, dst)
        changed += 1
    return changed


def enable_folder(folder):
    changed = 0
    for src in get_mp3_files(folder):
        if is_disabled(src):
            dst = strip_disable_suffix(src)
            tracked_rename(src, dst)
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
    tracked_rename(path, new_path)
    return new_path


def gamble(base_dir="."):
    files = get_mp3_files(base_dir)

    if len(files) < 2:
        return None

    max_amount = max(1, min(len(files), max(5, len(files) // 8)))

    while True:
        amount = random.randint(1, max_amount)
        chosen = random.sample(files, amount)

        current_paths = [toggle_track(path) for path in chosen]

        all_files = get_mp3_files(base_dir)
        enabled = sum(1 for f in all_files if not is_disabled(f))

        if enabled >= 1:
            results = []
            for path in current_paths:
                action = "disabled" if is_disabled(path) else "enabled"
                rel = os.path.relpath(path, base_dir).replace("\\", "/")
                results.append((rel, action))
            return results

        for path in current_paths:
            toggle_track(path)


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
        tracked_rename(path, new_path)
    return new_path


def remove_shuffle_prefix_from_file(path):
    dirpath, filename = os.path.split(path)
    base = strip_shuffle_prefix(filename)
    if base == filename:
        return path
    new_path = os.path.join(dirpath, base)
    tracked_rename(path, new_path)
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
                tracked_rename(path, strip_disable_suffix(path))
                changed += 1
            elif desired == "disabled" and currently_enabled:
                tracked_rename(path, path + DISABLE_SUFFIX)
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


def library_counts(base_dir="."):
    files = get_mp3_files(base_dir)
    total = len(files)
    disabled = sum(1 for f in files if is_disabled(f))
    return total - disabled, total


def track_count(n):
    return f"{n} track" if n == 1 else f"{n} tracks"


def format_time(seconds):
    if not seconds or seconds < 0:
        seconds = 0
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _first_str(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _id3_text(tags, key):
    frame = tags.get(key)
    if frame is None:
        return None
    text = getattr(frame, "text", None)
    return _first_str(text) if text else _first_str(str(frame))


COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "artwork")
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def find_folder_cover(path):
    folder = os.path.dirname(path)
    for d in (folder, os.path.dirname(folder)):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in sorted(names):
            stem, ext = os.path.splitext(name.lower())
            if stem in COVER_NAMES and ext in COVER_EXTS:
                try:
                    with open(os.path.join(d, name), "rb") as fh:
                        return fh.read()
                except OSError:
                    pass
    return None


def read_track_tags(path, name_path=None):
    real_name = strip_shuffle_prefix(
        os.path.basename(strip_disable_suffix(name_path or path))
    )
    fallback_title = os.path.splitext(real_name)[0]

    info = {
        "title": fallback_title,
        "artist": "",
        "album": "",
        "duration": None,
        "cover": None,
        "cover_mime": None,
    }

    if not HAS_MUTAGEN:
        return info

    try:
        audio = mutagen.File(path)
    except Exception:
        return info
    if audio is None:
        return info

    try:
        length = getattr(audio.info, "length", None)
        if length:
            info["duration"] = length
    except Exception:
        pass

    tags = getattr(audio, "tags", None)

    try:
        if isinstance(audio, MP4):
            if tags:
                info["title"] = _first_str(tags.get("\xa9nam")) or info["title"]
                info["artist"] = _first_str(tags.get("\xa9ART")) or ""
                info["album"] = _first_str(tags.get("\xa9alb")) or ""
                covers = tags.get("covr")
                if covers:
                    cover = covers[0]
                    is_png = getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG
                    info["cover"] = bytes(cover)
                    info["cover_mime"] = "image/png" if is_png else "image/jpeg"
        elif isinstance(audio, ASF):
            if tags:
                info["title"] = _first_str(tags.get("Title")) or info["title"]
                info["artist"] = _first_str(tags.get("Author")) or ""
                info["album"] = _first_str(tags.get("WM/AlbumTitle")) or ""
                pics = tags.get("WM/Picture")
                if pics:
                    picture = getattr(pics[0].value, "picture", None)
                    if picture is not None:
                        info["cover"] = picture.data
                        info["cover_mime"] = picture.mime or "image/jpeg"
        elif hasattr(audio, "pictures"):
            if tags:
                info["title"] = _first_str(tags.get("title")) or info["title"]
                info["artist"] = _first_str(tags.get("artist")) or ""
                info["album"] = _first_str(tags.get("album")) or ""
            if audio.pictures:
                info["cover"] = audio.pictures[0].data
                info["cover_mime"] = audio.pictures[0].mime or "image/jpeg"
        elif tags is not None and not isinstance(tags, ID3) and hasattr(tags, "get"):
            info["title"] = _first_str(tags.get("title")) or info["title"]
            info["artist"] = _first_str(tags.get("artist")) or ""
            info["album"] = _first_str(tags.get("album")) or ""
            pic_b64 = tags.get("metadata_block_picture")
            if pic_b64:
                try:
                    pic = Picture(base64.b64decode(pic_b64[0]))
                    info["cover"] = pic.data
                    info["cover_mime"] = pic.mime or "image/jpeg"
                except Exception:
                    pass
        elif tags is not None:
            info["title"] = _id3_text(tags, "TIT2") or info["title"]
            info["artist"] = _id3_text(tags, "TPE1") or ""
            info["album"] = _id3_text(tags, "TALB") or ""
            apics = tags.getall("APIC") if hasattr(tags, "getall") else []
            if apics:
                info["cover"] = apics[0].data
                info["cover_mime"] = apics[0].mime or "image/jpeg"
    except Exception:
        pass

    if not info["title"]:
        info["title"] = fallback_title

    if not info["cover"]:
        try:
            for frame in (
                (tags.getall("APIC") + tags.getall("PIC"))
                if hasattr(tags, "getall")
                else []
            ):
                if getattr(frame, "data", None):
                    info["cover"] = frame.data
                    break
        except Exception:
            pass
    if not info["cover"]:
        info["cover"] = find_folder_cover(path)
    return info


EDIT_FIELDS = (
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("albumartist", "Album artist"),
    ("tracknumber", "Track #"),
    ("date", "Year / date"),
)


class TagEditError(Exception):
    pass


_tag_tmp_dir = None
_tag_lock = threading.Lock()


def _tag_path(path):
    global _tag_tmp_dir
    if not is_disabled(path):
        return path
    with _tag_lock:
        if _tag_tmp_dir is None:
            _tag_tmp_dir = tempfile.mkdtemp(prefix="neutm_tags_")
            atexit.register(shutil.rmtree, _tag_tmp_dir, ignore_errors=True)
    link = os.path.join(
        _tag_tmp_dir,
        f"{abs(hash(path)):x}_{os.path.basename(strip_disable_suffix(path))}",
    )
    try:
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(os.path.abspath(path), link)
    except (OSError, NotImplementedError):
        return None
    return link


def _open_audio(path, easy=True):
    if not HAS_MUTAGEN:
        raise TagEditError("mutagen is not installed (pip install mutagen).")
    real = _tag_path(path)
    if real is None:
        raise TagEditError(
            "Tags on a disabled track can't be edited on this system. "
            "Enable the track first."
        )
    try:
        audio = mutagen.File(real, easy=easy)
    except Exception as exc:
        raise TagEditError(f"Could not read file: {exc}")
    if audio is None:
        raise TagEditError("Unsupported or unreadable audio format.")
    return audio


def _tag_values(tags):
    out = {}
    for key, _label in EDIT_FIELDS:
        try:
            out[key] = _first_str(tags.get(key)) or ""
        except Exception:
            out[key] = ""
    return out


def read_editable_tags(path):
    audio = _open_audio(path)
    return _tag_values(audio.tags) if audio.tags is not None else {
        key: "" for key, _label in EDIT_FIELDS
    }


def write_tags(path, values):
    audio = _open_audio(path)
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception as exc:
            raise TagEditError(f"This format can't hold tags: {exc}")
    old = _tag_values(audio.tags)
    changed = False
    for key, _label in EDIT_FIELDS:
        if key not in values:
            continue
        new = values[key].strip()
        if new == old[key]:
            continue
        changed = True
        try:
            if new:
                audio.tags[key] = [new]
            elif key in audio.tags:
                del audio.tags[key]
        except Exception as exc:
            raise TagEditError(f"Could not set {key}: {exc}")
    if not changed:
        return None
    try:
        audio.save()
    except Exception as exc:
        raise TagEditError(f"Could not save tags: {exc}")
    return old


def read_embedded_cover(path):
    audio = _open_audio(path, easy=False)
    tags = audio.tags
    try:
        if isinstance(audio, MP4):
            covers = tags.get("covr") if tags else None
            if covers:
                is_png = getattr(covers[0], "imageformat", None) == MP4Cover.FORMAT_PNG
                return bytes(covers[0]), "image/png" if is_png else "image/jpeg"
        elif hasattr(audio, "pictures"):
            if audio.pictures:
                pic = audio.pictures[0]
                return pic.data, pic.mime or "image/jpeg"
        elif isinstance(tags, ID3):
            apics = tags.getall("APIC")
            if apics:
                return apics[0].data, apics[0].mime or "image/jpeg"
        elif tags is not None and hasattr(tags, "get"):
            b64 = tags.get("metadata_block_picture")
            if b64:
                pic = Picture(base64.b64decode(b64[0]))
                return pic.data, pic.mime or "image/jpeg"
    except Exception:
        pass
    return None, None


def _make_picture(data, mime):
    pic = Picture()
    pic.type = 3
    pic.mime = mime
    pic.desc = "Cover"
    pic.data = data
    return pic


def write_cover(path, data, mime="image/jpeg"):
    audio = _open_audio(path, easy=False)
    tags = audio.tags
    try:
        if isinstance(audio, ASF):
            raise TagEditError("Cover editing isn't supported for WMA/ASF files.")
        if isinstance(audio, MP4):
            if tags is None:
                audio.add_tags()
                tags = audio.tags
            if data:
                fmt = (
                    MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                )
                tags["covr"] = [MP4Cover(data, imageformat=fmt)]
            elif "covr" in tags:
                del tags["covr"]
        elif hasattr(audio, "pictures"):
            audio.clear_pictures()
            if data:
                audio.add_picture(_make_picture(data, mime))
        elif tags is None or isinstance(tags, ID3):
            if tags is None:
                if not data:
                    return
                audio.add_tags()
                tags = audio.tags
            tags.delall("APIC")
            if data:
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        else:
            if data:
                pic = _make_picture(data, mime)
                tags["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")
                ]
            elif "metadata_block_picture" in tags:
                del tags["metadata_block_picture"]
        audio.save()
    except TagEditError:
        raise
    except Exception as exc:
        raise TagEditError(f"Could not save cover: {exc}")


def prepare_cover_bytes(raw, max_side=1200):
    img = QImage.fromData(raw)
    if img.isNull():
        raise TagEditError("That isn't a readable image.")
    mime = None
    if raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    if mime and max(img.width(), img.height()) <= max_side and len(raw) <= 3_000_000:
        return raw, mime, img.width(), img.height()
    if max(img.width(), img.height()) > max_side:
        img = img.scaled(
            max_side,
            max_side,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPEG", 90)
    return bytes(buf.data()), "image/jpeg", img.width(), img.height()


def prepare_cover_image(file_path, max_side=1200):
    try:
        with open(file_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise TagEditError(f"Could not read image: {exc}")
    try:
        return prepare_cover_bytes(raw, max_side)
    except TagEditError:
        raise TagEditError("That file isn't a readable image.")


MB_USER_AGENT = "Neutm/1.0 (local music library manager)"
MB_API = "https://musicbrainz.org/ws/2/"
CAA_API = "https://coverartarchive.org/"
MB_MIN_INTERVAL = 1.1

_mb_lock = threading.Lock()
_mb_last = 0.0


class MusicBrainzError(Exception):
    pass


def _mb_get(url, accept="application/json", timeout=10):
    global _mb_last
    with _mb_lock:
        wait = MB_MIN_INTERVAL - (time.time() - _mb_last)
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": MB_USER_AGENT, "Accept": accept}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                raise MusicBrainzError(
                    "MusicBrainz is busy or rate-limiting. Try again in a moment."
                )
            if exc.code == 404:
                raise MusicBrainzError("Not found.")
            raise MusicBrainzError(f"MusicBrainz returned HTTP {exc.code}.")
        except (urllib.error.URLError, OSError) as exc:
            raise MusicBrainzError(f"Could not reach MusicBrainz: {exc}")
        finally:
            _mb_last = time.time()


def _lucene_phrase(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _lucene_text(text):
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", text)


def musicbrainz_search(title, artist="", max_recordings=6, releases_per=3):
    title = title.strip()
    artist = artist.strip()
    if not title:
        raise MusicBrainzError("Enter a title first.")
    if artist:
        query = f'recording:"{_lucene_phrase(title)}" AND artist:"{_lucene_phrase(artist)}"'
    else:
        query = _lucene_text(title)
    url = MB_API + "recording/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": max_recordings}
    )
    try:
        data = json.loads(_mb_get(url).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise MusicBrainzError("MusicBrainz sent a response Neutm couldn't read.")

    rows = []
    for rec in data.get("recordings", []) or []:
        credit = rec.get("artist-credit") or []
        artist_name = "".join(
            (c.get("name") or (c.get("artist") or {}).get("name", ""))
            + (c.get("joinphrase") or "")
            for c in credit
        ).strip()
        base = {
            "score": int(rec.get("score") or 0),
            "title": rec.get("title") or "",
            "artist": artist_name,
            "recording_id": rec.get("id"),
        }
        releases = sorted(
            rec.get("releases") or [],
            key=lambda r: (r.get("status") != "Official", r.get("date") or "9999"),
        )
        if not releases:
            rows.append({**base, "album": "", "date": "", "release_id": None})
            continue
        for rel in releases[:releases_per]:
            rows.append(
                {
                    **base,
                    "album": rel.get("title") or "",
                    "date": rel.get("date") or "",
                    "release_id": rel.get("id"),
                }
            )
    rows.sort(key=lambda r: -r["score"])
    return rows


def musicbrainz_fetch_cover(release_id):
    if not release_id:
        raise MusicBrainzError("That match has no release, so there's no cover art.")
    try:
        return _mb_get(
            f"{CAA_API}release/{release_id}/front-500", accept="image/*", timeout=15
        )
    except MusicBrainzError as exc:
        if str(exc) == "Not found.":
            raise MusicBrainzError("No cover art on file for that release.")
        raise


SPEC_WIN = 1024
SPEC_HOP = 512


def spectrum_band_edges(rate, bands=20, f_lo=40.0, f_hi=12000.0):
    freqs = np.geomspace(f_lo, min(f_hi, rate / 2 - 1), bands + 1)
    bins = np.round(freqs * SPEC_WIN / rate).astype(int)
    for i in range(1, len(bins)):
        if bins[i] <= bins[i - 1]:
            bins[i] = bins[i - 1] + 1
    return bins


class LastFmError(Exception):
    pass


class LastFmClient:
    def __init__(self, api_key, api_secret, session_key=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_key = session_key

    def _sign(self, params):
        src = "".join(
            f"{k}{params[k]}" for k in sorted(params) if k not in ("format", "callback")
        )
        src += self.api_secret
        return hashlib.md5(src.encode("utf-8")).hexdigest()

    def _call(self, method, params, http_method="GET"):
        call_params = dict(params)
        call_params["method"] = method
        call_params["api_key"] = self.api_key
        call_params["api_sig"] = self._sign(call_params)
        call_params["format"] = "json"

        encoded = urllib.parse.urlencode(call_params).encode("utf-8")
        headers = {"User-Agent": "Neutm/1.0"}

        if http_method == "GET":
            req = urllib.request.Request(
                f"{LASTFM_API_URL}?{encoded.decode('utf-8')}", headers=headers
            )
        else:
            req = urllib.request.Request(LASTFM_API_URL, data=encoded, headers=headers)

        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        if isinstance(payload, dict) and "error" in payload:
            raise LastFmError(payload.get("message", "Unknown Last.fm error"))
        return payload

    def get_token(self):
        return self._call("auth.getToken", {})["token"]

    def auth_url(self, token):
        return f"https://www.last.fm/api/auth/?api_key={self.api_key}&token={token}"

    def get_session(self, token):
        payload = self._call("auth.getSession", {"token": token})
        self.session_key = payload["session"]["key"]
        return self.session_key

    def update_now_playing(self, artist, track, album=None, duration=None):
        if not self.session_key:
            return
        params = {"artist": artist, "track": track, "sk": self.session_key}
        if album:
            params["album"] = album
        if duration:
            params["duration"] = int(duration)
        self._call("track.updateNowPlaying", params, http_method="POST")

    def scrobble(self, artist, track, timestamp, album=None, duration=None):
        if not self.session_key:
            return
        params = {
            "artist": artist,
            "track": track,
            "timestamp": int(timestamp),
            "sk": self.session_key,
        }
        if album:
            params["album"] = album
        if duration:
            params["duration"] = int(duration)
        self._call("track.scrobble", params, http_method="POST")


def resource_path(*parts):
    if getattr(sys, "_MEIPASS", None):
        return os.path.join(sys._MEIPASS, "assets", *parts)

    return str(files("assets").joinpath(*parts))


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


class LastFmWorker(QThread):
    failed = pyqtSignal(str)

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job

    def run(self):
        try:
            self.job()
        except Exception as exc:
            self.failed.emit(str(exc))


class JobWorker(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job

    def run(self):
        try:
            self.succeeded.emit(self.job())
        except Exception as exc:
            self.failed.emit(str(exc))


class TagLoadWorker(QThread):
    loaded = pyqtSignal(str, object)

    def __init__(self, paths, need_cover, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.need_cover = need_cover
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        thumbs = {}
        for path in self.paths:
            if self._cancel:
                return
            try:
                mtime = os.stat(path).st_mtime_ns
                tags = read_track_tags(_tag_path(path) or path, path)
            except Exception:
                continue
            thumb = None
            cover = tags.get("cover")
            if self.need_cover and cover:
                key = hash(bytes(cover))
                if key not in thumbs:
                    img = QImage.fromData(bytes(cover))
                    thumbs[key] = (
                        None
                        if img.isNull()
                        else img.scaled(
                            SONG_THUMB,
                            SONG_THUMB,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                thumb = thumbs[key]
            self.loaded.emit(
                path,
                {
                    "title": tags.get("title") or "",
                    "artist": tags.get("artist") or "",
                    "album": tags.get("album") or "",
                    "duration": tags.get("duration"),
                    "thumb": thumb,
                    "has_thumb": self.need_cover,
                    "mtime": mtime,
                },
            )


class ClickSlider(QSlider):
    def _value_at(self, pos):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )
        if self.orientation() == Qt.Orientation.Horizontal:
            span = max(1, groove.width() - handle.width())
            offset = pos.x() - groove.x() - handle.width() // 2
        else:
            span = max(1, groove.height() - handle.height())
            offset = pos.y() - groove.y() - handle.height() // 2
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), offset, span, opt.upsideDown
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.setValue(self._value_at(event.position().toPoint()))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(self._value_at(event.position().toPoint()))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class DiscGroupBox(QGroupBox):
    DISC_OPACITY = 0.7
    DISC_SCALE = 0.9
    DISC_OFFSET = 0.45

    def __init__(self, title, gif_path, parent=None):
        super().__init__(title, parent)
        self._movie = None
        self.anchor = None
        if gif_path and os.path.isfile(gif_path):
            movie = QMovie(gif_path)
            if movie.isValid():
                movie.setCacheMode(QMovie.CacheMode.CacheAll)
                movie.frameChanged.connect(lambda _frame: self.update())
                movie.jumpToFrame(0)
                self._movie = movie

    def set_spinning(self, spinning):
        movie = self._movie
        if movie is None:
            return
        if spinning:
            if movie.state() == QMovie.MovieState.NotRunning:
                movie.start()
            else:
                movie.setPaused(False)
        elif movie.state() == QMovie.MovieState.Running:
            movie.setPaused(True)

    def paintEvent(self, event):
        if self._movie is not None and self.anchor is not None:
            frame = self._movie.currentPixmap()
            if not frame.isNull():
                cover = self.anchor.geometry()
                size = int(cover.height() * self.DISC_SCALE)
                cx = cover.center().x() + int(size * self.DISC_OFFSET)
                cy = cover.center().y()
                painter = QPainter(self)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.setOpacity(self.DISC_OPACITY)
                painter.drawPixmap(cx - size // 2, cy - size // 2, size, size, frame)
                painter.end()
        super().paintEvent(event)


class AnalyzerWidget(QWidget):
    NUM_BANDS = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self._levels = [0.0] * self.NUM_BANDS
        self._targets = [0.0] * self.NUM_BANDS
        self._active = False
        self.setMinimumSize(120, 36)
        self.setMaximumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_active(self, active):
        self._active = active
        if not active:
            self._targets = [0.0] * self.NUM_BANDS

    def set_levels(self, levels):
        n = len(levels)
        if n == 0:
            return
        if n == self.NUM_BANDS:
            self._targets = [max(0.0, min(1.0, v)) for v in levels]
        else:
            self._targets = [
                max(0.0, min(1.0, levels[min(n - 1, int(i * n / self.NUM_BANDS))]))
                for i in range(self.NUM_BANDS)
            ]

    def _tick(self):
        changed = False
        for i in range(self.NUM_BANDS):
            target = self._targets[i]
            current = self._levels[i]
            if abs(target - current) > 0.002:
                rate = 0.55 if target > current else 0.2
                self._levels[i] = current + (target - current) * rate
                changed = True
        if changed:
            self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        n = self.NUM_BANDS
        gap = 2.0
        bar_w = max(2.0, (rect.width() - gap * (n - 1)) / n)
        base_color = self.palette().highlight().color()

        for i, level in enumerate(self._levels):
            level = max(0.02, min(1.0, level))
            h = level * rect.height()
            x = i * (bar_w + gap)
            y = rect.height() - h
            color = QColor(base_color)
            color.setAlphaF(0.35 + level * 0.65)
            painter.fillRect(QRectF(x, y, bar_w, h), color)
        painter.end()


class MusicBrainzResultsDialog(QDialog):
    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MusicBrainz matches")
        self.setMinimumWidth(560)
        self.rows = rows
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Pick the best match. Nothing is saved until you click Save in the tag editor."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        self.list = QListWidget()
        for row in rows:
            album = row["album"] or "(no release)"
            if row["date"]:
                album += f" ({row['date'][:4]})"
            text = f"{row['artist'] or '?'} - {row['title']}, {album}, {row['score']}%"
            self.list.addItem(text)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list)

        self.cover_check = QCheckBox("Also fetch cover art for this release")
        self.cover_check.setChecked(True)
        layout.addWidget(self.cover_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use this match")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_row(self):
        index = self.list.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def want_cover(self):
        return self.cover_check.isChecked()


class TagEditDialog(QDialog):
    def __init__(self, filename, values, cover=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Tags - {filename}")
        self.setMinimumWidth(460)
        self.new_cover = None
        self.remove_cover = False
        self._had_cover = bool(cover)
        self._worker = None

        form = QFormLayout(self)

        art_row = QHBoxLayout()
        self.cover_preview = QLabel()
        self.cover_preview.setFixedSize(120, 120)
        self.cover_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_preview.setStyleSheet("background-color: rgba(128, 128, 128, 40);")
        art_col = QVBoxLayout()
        self.cover_status = QLabel()
        self.cover_status.setWordWrap(True)
        self.cover_status.setStyleSheet("color: gray; font-size: 11px;")
        browse_btn = QPushButton("Browse for image...")
        browse_btn.clicked.connect(self._on_browse)
        self.remove_btn = QPushButton("Remove cover")
        self.remove_btn.setEnabled(self._had_cover)
        self.remove_btn.clicked.connect(self._on_remove)
        art_col.addWidget(self.cover_status)
        art_col.addWidget(browse_btn)
        art_col.addWidget(self.remove_btn)
        art_col.addStretch(1)
        art_row.addWidget(self.cover_preview)
        art_row.addLayout(art_col, 1)
        form.addRow("Cover:", art_row)
        self._show_cover(cover, "Embedded cover" if cover else "No embedded cover")

        self.edits = {}
        for key, label in EDIT_FIELDS:
            edit = QLineEdit(values.get(key, ""))
            self.edits[key] = edit
            form.addRow(f"{label}:", edit)

        lookup_row = QHBoxLayout()
        self.lookup_btn = QPushButton("Look up on MusicBrainz...")
        self.lookup_btn.setToolTip(
            "Searches MusicBrainz using the Title (and Artist, if filled in) above"
        )
        self.lookup_btn.clicked.connect(self._on_lookup)
        self.lookup_status = QLabel("")
        self.lookup_status.setWordWrap(True)
        self.lookup_status.setStyleSheet("color: gray; font-size: 11px;")
        lookup_row.addWidget(self.lookup_btn)
        lookup_row.addWidget(self.lookup_status, 1)
        form.addRow(lookup_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def done(self, result):
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait()
        super().done(result)

    def _show_cover(self, data, status):
        self.cover_status.setText(status)
        self.cover_preview.clear()
        if data:
            img = QImage.fromData(bytes(data))
            if not img.isNull():
                self.cover_preview.setPixmap(
                    QPixmap.fromImage(
                        img.scaled(
                            120,
                            120,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                )

    def _apply_cover_bytes(self, raw, label):
        data, mime, w, h = prepare_cover_bytes(raw)
        self.new_cover = (data, mime)
        self.remove_cover = False
        self._show_cover(data, f"{label} ({w}x{h}). Saved when you click Save.")

    def _on_browse(self):
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose cover image",
            "",
            "Images (*.jpg *.jpeg *.png *.webp *.bmp *.gif)",
        )
        if not path:
            return
        try:
            data, mime, w, h = prepare_cover_image(path)
        except TagEditError as exc:
            QMessageBox.warning(self, "Cover image", str(exc))
            return
        self.new_cover = (data, mime)
        self.remove_cover = False
        self._show_cover(
            data,
            f"New cover: {os.path.basename(path)} ({w}x{h}). Saved when you click Save.",
        )

    def _on_remove(self):
        self.new_cover = None
        self.remove_cover = True
        self._show_cover(None, "Cover will be removed when you click Save.")

    def _busy(self, busy, message=""):
        self.lookup_btn.setEnabled(not busy)
        self.lookup_status.setText(message)

    def _start_job(self, job, on_success, message):
        self._busy(True, message)
        worker = JobWorker(job, self)
        worker.succeeded.connect(on_success)
        worker.failed.connect(lambda msg: self._busy(False, msg))
        self._worker = worker
        worker.start()

    def _on_lookup(self):
        title = self.edits["title"].text().strip()
        artist = self.edits["artist"].text().strip()
        if not title:
            self._busy(False, "Type a title first (a filename-style title works too).")
            return
        self._start_job(
            lambda: musicbrainz_search(title, artist),
            self._on_lookup_results,
            "Searching MusicBrainz...",
        )

    def _on_lookup_results(self, rows):
        if not rows:
            self._busy(False, "No matches found. Try editing the title or artist.")
            return
        self._busy(False, f"{len(rows)} match(es) found.")
        picker = MusicBrainzResultsDialog(rows, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        row = picker.selected_row()
        if row is None:
            return
        for key in ("title", "artist", "album", "date"):
            if row.get(key):
                self.edits[key].setText(row[key])
        self._busy(False, "Fields filled in. Review them, then click Save.")
        if picker.want_cover() and row.get("release_id"):
            self._start_job(
                lambda: musicbrainz_fetch_cover(row["release_id"]),
                self._on_cover_fetched,
                "Fetching cover art...",
            )

    def _on_cover_fetched(self, raw):
        try:
            self._apply_cover_bytes(raw, "Cover from MusicBrainz")
        except TagEditError as exc:
            self._busy(False, str(exc))
            return
        self._busy(False, "Fields and cover filled in. Review them, then click Save.")

    def cover_change(self):
        if self.new_cover is not None:
            return ("set", self.new_cover[0], self.new_cover[1])
        if self.remove_cover:
            return ("remove", None, None)
        return None

    def values(self):
        return {key: edit.text() for key, edit in self.edits.items()}


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
        self.resize(1050, 700)

        if self.base_dir is None:
            chosen = QFileDialog.getExistingDirectory(
                self,
                "Select your music folder (e.g. a USB drive)",
                default_browse_start_dir(),
            )
            self.base_dir = chosen if chosen else os.getcwd()

        self.settings.setValue("music_root_path", self.base_dir)

        self._init_playback_state()
        self._build_menu_bar()

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        self.folder_path_label = QLabel()
        self.folder_path_label.setStyleSheet("font-weight: bold;")
        root_layout.addWidget(self.folder_path_label)

        self.totals_label = QLabel()
        self.totals_label.setStyleSheet("color: gray; font-size: 11px;")
        root_layout.addWidget(self.totals_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, stretch=1)

        left_box = QGroupBox("Folders")
        left_layout = QVBoxLayout(left_box)

        self.folder_list = QTreeWidget()
        self.folder_list.setHeaderHidden(True)
        self.folder_list.setIndentation(14)
        self.folder_list.currentItemChanged.connect(self.on_folder_selected)
        self.folder_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.folder_list.customContextMenuRequested.connect(self.on_folder_context_menu)
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
        self.song_list = QTreeWidget()
        self.song_list.setColumnCount(6)
        self.song_list.setHeaderLabels(
            ["", "Title", "Artist", "Album", "Time", "File path"]
        )
        self.song_list.setRootIsDecorated(False)
        self.song_list.setItemsExpandable(False)
        song_header = self.song_list.header()
        song_header.setStretchLastSection(False)
        song_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.song_list.setColumnWidth(0, SONG_THUMB + 16)
        for idx in (1, 2, 3, 5):
            song_header.setSectionResizeMode(idx, QHeaderView.ResizeMode.Stretch)
        song_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        song_header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        song_header.customContextMenuRequested.connect(self.on_song_header_menu)
        self._apply_song_columns()
        self.song_list.currentItemChanged.connect(self.on_song_selected)
        self.song_list.itemDoubleClicked.connect(self.on_toggle_track)
        self.song_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.song_list.customContextMenuRequested.connect(self.on_song_context_menu)
        right_layout.addWidget(self.song_list, stretch=1)

        song_btn_row = QHBoxLayout()
        self.toggle_track_btn = QPushButton("Toggle Selected Track")
        self.toggle_track_btn.setEnabled(False)
        self.toggle_track_btn.clicked.connect(self.on_toggle_track)
        song_btn_row.addWidget(self.toggle_track_btn)

        self.play_selected_btn = QPushButton("Play From Here")
        self.play_selected_btn.setEnabled(False)
        self.play_selected_btn.clicked.connect(self.on_play_selected_track)
        song_btn_row.addWidget(self.play_selected_btn)

        right_layout.addLayout(song_btn_row)

        right_layout.addWidget(QLabel("Activity log:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        right_layout.addWidget(self.log)

        splitter.addWidget(right_box)
        splitter.setSizes([480, 570])

        self.player_bar = self._build_player_bar()
        root_layout.addWidget(self.player_bar)

        self.refresh()

    def log_msg(self, msg):
        self.log.appendPlainText(msg)

    def refresh(self):
        self._update_folder_label()
        self._update_library_totals()

        selected_folder = None
        current = self.folder_list.currentItem()
        if current:
            selected_folder = current.data(0, Qt.ItemDataRole.UserRole)

        self.folder_list.clear()
        top_folders = get_folders(self.base_dir)

        if not top_folders:
            self.log_msg(f"No folders found in '{self.base_dir}'.")
            return

        self._add_folder_items(None, "")
        self.folder_list.expandAll()

        item_to_select = (
            self._find_folder_item(selected_folder) if selected_folder else None
        )
        if item_to_select is not None:
            self.folder_list.setCurrentItem(item_to_select)
        elif self.folder_list.topLevelItemCount() > 0:
            self.folder_list.setCurrentItem(self.folder_list.topLevelItem(0))

    def _add_folder_items(self, parent_item, rel_dir):
        full_dir = os.path.join(self.base_dir, rel_dir) if rel_dir else self.base_dir
        for name in get_folders(full_dir):
            rel_path = os.path.join(rel_dir, name) if rel_dir else name
            item = (
                QTreeWidgetItem(parent_item)
                if parent_item is not None
                else QTreeWidgetItem(self.folder_list)
            )
            item.setData(0, Qt.ItemDataRole.UserRole, rel_path)
            row = self.build_folder_row(item, rel_path)
            item.setSizeHint(0, row.sizeHint())
            self._add_folder_items(item, rel_path)

    def _find_folder_item(self, rel_path):
        iterator = QTreeWidgetItemIterator(self.folder_list)
        while iterator.value():
            item = iterator.value()
            if item.data(0, Qt.ItemDataRole.UserRole) == rel_path:
                return item
            iterator += 1
        return None

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

        name_label = QLabel(os.path.basename(folder))
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

        self.folder_list.setItemWidget(item, 0, row)
        return row

    def show_songs(self, folder):
        self._song_folder = folder
        self._render_songs()
        self.toggle_track_btn.setEnabled(False)

    def _song_path(self, item):
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _visible_song_columns(self):
        return set(self.song_columns[self.song_view])

    def _apply_song_columns(self):
        visible = self._visible_song_columns()
        spaced = self.song_view == "spaced"
        self.song_list.setColumnHidden(0, not spaced)
        for idx, (key, _label) in enumerate(SONG_COLUMNS, start=1):
            self.song_list.setColumnHidden(idx, key not in visible)
        self.song_list.header().setVisible(self.song_view != "default")
        side = SONG_THUMB if spaced else 16
        self.song_list.setIconSize(QSize(side, side))

    def _cached_song_info(self, path, need_cover):
        entry = self._song_cache.get(path)
        if entry is None:
            return None
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return None
        if entry["mtime"] != mtime or (need_cover and not entry["has_thumb"]):
            return None
        return entry

    def _fill_song_item(self, item, path, info=None):
        info = info or {}
        fallback = os.path.splitext(
            strip_shuffle_prefix(os.path.basename(strip_disable_suffix(path)))
        )[0]
        duration = info.get("duration")
        item.setText(1, info.get("title") or fallback)
        item.setText(2, info.get("artist") or "")
        item.setText(3, info.get("album") or "")
        item.setText(4, format_time(duration) if duration else "")
        item.setText(5, os.path.relpath(path, self.base_dir))
        if self.song_view == "spaced":
            item.setSizeHint(0, QSize(SONG_THUMB + 8, SONG_THUMB + 8))
            thumb = info.get("thumb")
            item.setIcon(
                0, QIcon(QPixmap.fromImage(thumb)) if thumb is not None else QIcon()
            )

    def _cancel_tag_workers(self):
        for worker in self._tag_workers:
            worker.cancel()

    def _render_songs(self, keep_selection=False):
        current = self.song_list.currentItem()
        selected = self._song_path(current) if (keep_selection and current) else None
        self._cancel_tag_workers()
        self.song_list.clear()
        self._song_items = {}
        self._apply_song_columns()
        if self._song_folder is None:
            return

        visible = self._visible_song_columns()
        need_tags = bool(visible & {"title", "artist", "album", "time"})
        need_cover = self.song_view == "spaced"
        pending = []
        grey = QColor(128, 128, 128)

        full_path = os.path.join(self.base_dir, self._song_folder)
        for path in get_mp3_files(full_path):
            item = QTreeWidgetItem()
            item.setData(0, Qt.ItemDataRole.UserRole, path)
            info = self._cached_song_info(path, need_cover) if need_tags else None
            self._fill_song_item(item, path, info)
            if need_tags and info is None:
                pending.append(path)
            if is_disabled(path):
                for col in range(self.song_list.columnCount()):
                    item.setForeground(col, grey)
            self._song_items[path] = item
            self.song_list.addTopLevelItem(item)

        if selected in self._song_items:
            self.song_list.setCurrentItem(self._song_items[selected])
        if pending:
            worker = TagLoadWorker(pending, need_cover, self)
            worker.loaded.connect(self._on_song_loaded)
            worker.finished.connect(
                lambda w=worker: (
                    self._tag_workers.remove(w) if w in self._tag_workers else None
                )
            )
            worker.finished.connect(worker.deleteLater)
            self._tag_workers.append(worker)
            worker.start()

    def _on_song_loaded(self, path, info):
        self._song_cache[path] = info
        item = self._song_items.get(path)
        if item is not None:
            self._fill_song_item(item, path, info)

    def _save_song_view(self):
        self.settings.setValue("song_view", self.song_view)
        self.settings.setValue("song_view_columns", json.dumps(self.song_columns))

    def _sync_view_menu(self):
        visible = self._visible_song_columns()
        for key, action in self.song_view_actions.items():
            action.blockSignals(True)
            action.setChecked(key == self.song_view)
            action.blockSignals(False)
        for key, action in self.song_col_actions.items():
            action.blockSignals(True)
            action.setChecked(key in visible)
            action.blockSignals(False)

    def on_song_view_changed(self, key):
        if key == self.song_view:
            return
        self.song_view = key
        self._save_song_view()
        self._sync_view_menu()
        self._render_songs(keep_selection=True)

    def on_song_column_toggled(self, key, checked):
        cols = set(self.song_columns[self.song_view])
        if checked:
            cols.add(key)
        else:
            cols.discard(key)
        if not cols:
            self._sync_view_menu()
            self.log_msg("At least one song list column has to stay visible.")
            return
        self.song_columns[self.song_view] = [k for k, _l in SONG_COLUMNS if k in cols]
        self._save_song_view()
        self._sync_view_menu()
        self._render_songs(keep_selection=True)

    def on_song_header_menu(self, pos):
        menu = QMenu(self)
        visible = self._visible_song_columns()
        for key, label in SONG_COLUMNS:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key in visible)
            action.setData(key)
        chosen = menu.exec(self.song_list.header().mapToGlobal(pos))
        if chosen is not None:
            self.on_song_column_toggled(chosen.data(), chosen.isChecked())

    def on_folder_selected(self, current, _previous):
        if current is None:
            self.song_list.clear()
            return
        folder = current.data(0, Qt.ItemDataRole.UserRole)
        self.show_songs(folder)

    def on_song_selected(self, current, _previous):
        self.toggle_track_btn.setEnabled(current is not None)
        self.play_selected_btn.setEnabled(current is not None)

    @undoable("Toggle folder", with_args=True)
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

    @undoable("Toggle track")
    def on_toggle_track(self, *_args):
        item = self.song_list.currentItem()
        if item is None:
            return
        path = self._song_path(item)
        new_path = toggle_track(path)
        display_name = os.path.relpath(new_path, self.base_dir)
        action = "Disabled" if new_path.lower().endswith(DISABLE_SUFFIX) else "Enabled"
        self.log_msg(f"{action} track '{display_name}'.")
        self.refresh()

    @undoable("Gamble")
    def on_gamble(self):
        self.log_msg("Lets see what we get.")
        result = gamble(self.base_dir)
        if result is None:
            self.log_msg("Not enough tracks to gamble. Spread out your songs vro...")
        else:
            self.log_msg(f"Gambled {len(result)} track(s):")
            for track, action in result:
                self.log_msg(f"  {action}: {track}")
        self.refresh()

    @undoable("Enable All")
    def on_enable_all(self):
        changed = enable_all(self.base_dir)
        self.log_msg(f"Enabled all folders ({changed} file(s) restored).")
        self.refresh()

    @undoable("Shuffle Order")
    def on_shuffle_all(self):
        changed = shuffle_all(self.base_dir)
        self.log_msg(f"Shuffled sort order for {changed} file(s) across all folders.")
        self.refresh()

    @undoable("Remove Shuffle")
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

        edit_menu = menu_bar.addMenu("&Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.undo_action.setEnabled(False)
        self.undo_action.triggered.connect(self.on_undo)
        edit_menu.addAction(self.undo_action)

        view_menu = menu_bar.addMenu("&View")
        list_menu = view_menu.addMenu("Song list")
        self.song_view_group = QActionGroup(self)
        self.song_view_actions = {}
        for key, label in SONG_VIEWS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(key == self.song_view)
            action.triggered.connect(lambda _c, k=key: self.on_song_view_changed(k))
            self.song_view_group.addAction(action)
            list_menu.addAction(action)
            self.song_view_actions[key] = action
        list_menu.addSeparator()
        self.song_col_actions = {}
        visible_cols = self._visible_song_columns()
        for key, label in SONG_COLUMNS:
            action = QAction(f"Show {label.lower()}", self)
            action.setCheckable(True)
            action.setChecked(key in visible_cols)
            action.toggled.connect(
                lambda checked, k=key: self.on_song_column_toggled(k, checked)
            )
            list_menu.addAction(action)
            self.song_col_actions[key] = action

        playback_menu = menu_bar.addMenu("&Playback")

        play_pause_action = QAction("Play/Pause", self)
        play_pause_action.triggered.connect(self.on_play_pause)
        playback_menu.addAction(play_pause_action)

        next_action = QAction("Next Track", self)
        next_action.triggered.connect(self.on_next_track)
        playback_menu.addAction(next_action)

        prev_action = QAction("Previous Track", self)
        prev_action.triggered.connect(self.on_prev_track)
        playback_menu.addAction(prev_action)

        playback_menu.addSeparator()

        shuffle_action = QAction("Shuffle", self)
        shuffle_action.setCheckable(True)
        shuffle_action.setChecked(self.shuffle_enabled)
        shuffle_action.toggled.connect(self.on_shuffle_toggled)
        playback_menu.addAction(shuffle_action)

        repeat_action = QAction("Cycle Repeat Mode", self)
        repeat_action.triggered.connect(self.on_repeat_cycle)
        playback_menu.addAction(repeat_action)

        lastfm_menu = menu_bar.addMenu("&Last.fm")

        lastfm_setup_action = QAction("Set API Key/Secret...", self)
        lastfm_setup_action.triggered.connect(self.on_lastfm_setup)
        lastfm_menu.addAction(lastfm_setup_action)

        lastfm_connect_action = QAction("Connect Account...", self)
        lastfm_connect_action.triggered.connect(self.on_lastfm_connect)
        lastfm_menu.addAction(lastfm_connect_action)

        lastfm_disconnect_action = QAction("Disconnect Account", self)
        lastfm_disconnect_action.triggered.connect(self.on_lastfm_disconnect)
        lastfm_menu.addAction(lastfm_disconnect_action)

        lastfm_menu.addSeparator()

        self.lastfm_enable_action = QAction("Enable Scrobbling", self)
        self.lastfm_enable_action.setCheckable(True)
        self.lastfm_enable_action.setChecked(self.lastfm_enabled)
        self.lastfm_enable_action.toggled.connect(self.on_lastfm_toggle_enabled)
        lastfm_menu.addAction(self.lastfm_enable_action)

    def on_undo(self):
        result = undo_last()
        if result is None:
            self.log_msg("Nothing to undo.")
        else:
            label, restored, skipped = result
            msg = f"Undid {label} ({restored} file(s) restored)."
            if skipped:
                msg += f" {skipped} could not be restored (changed outside the app?)."
            self.log_msg(msg)
        self._update_undo_action()
        self.refresh()

    def _update_undo_action(self):
        label = undo_label()
        self.undo_action.setEnabled(label is not None)
        self.undo_action.setText(f"Undo {label}" if label else "Undo")

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

    @undoable("Load preset", with_args=True)
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

    @undoable("Import preset")
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
        _undo_stack.clear()
        self._update_undo_action()
        self.settings.setValue("music_root_path", self.base_dir)
        self.log_msg(f"Music folder changed to '{self.base_dir}'.")
        self.refresh()

    def _update_folder_label(self):
        self.folder_path_label.setText(f"Music folder: {self.base_dir}")
        folder_name = os.path.basename(self.base_dir.rstrip(os.sep)) or self.base_dir
        self.setWindowTitle(f"Neutm - {folder_name}")

    def _update_library_totals(self):
        enabled, total = library_counts(self.base_dir)
        disabled = total - enabled
        self.totals_label.setText(
            f"Library: {enabled} enabled / {disabled} disabled ({total} total)"
        )

    def closeEvent(self, event):
        for worker in list(self._tag_workers):
            worker.cancel()
            worker.wait()
        self.settings.setValue("window_geometry", self.saveGeometry())
        if self.player is not None:
            self.player.stop()
        if getattr(self, "_tmp_dir", None):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        event.accept()

    def _init_playback_state(self):
        self.queue = []
        self._queue_scope = None
        self.queue_index = -1
        self._current_track_path = None
        self._current_tags = None
        self._track_start_epoch = None
        self._seek_slider_pressed = False
        self._lastfm_workers = []

        self.shuffle_enabled = self.settings.value("shuffle_enabled", False, type=bool)
        self.repeat_mode = self.settings.value("repeat_mode", "off", type=str)
        if self.repeat_mode not in ("off", "all", "one"):
            self.repeat_mode = "off"

        self.song_view = self.settings.value("song_view", "default", type=str)
        if self.song_view not in dict(SONG_VIEWS):
            self.song_view = "default"
        self.song_columns = {k: list(v) for k, v in SONG_DEFAULT_COLUMNS.items()}
        try:
            saved = json.loads(self.settings.value("song_view_columns", "{}", type=str))
            for view in self.song_columns:
                keep = [k for k, _l in SONG_COLUMNS if k in (saved.get(view) or [])]
                if keep:
                    self.song_columns[view] = keep
        except (ValueError, AttributeError, TypeError):
            pass
        self._song_folder = None
        self._song_items = {}
        self._song_cache = {}
        self._tag_workers = []

        self.lastfm_enabled = self.settings.value("lastfm_enabled", False, type=bool)
        self.lastfm_client = None
        api_key = self.settings.value("lastfm_api_key", "", type=str)
        api_secret = self.settings.value("lastfm_api_secret", "", type=str)
        session_key = self.settings.value("lastfm_session_key", "", type=str)
        if api_key and api_secret:
            self.lastfm_client = LastFmClient(api_key, api_secret, session_key or None)

        self._scrobble_timer = QTimer(self)
        self._scrobble_timer.setSingleShot(True)
        self._scrobble_timer.timeout.connect(self._do_scrobble)

        self._spec_samples = array("h")
        self._spec_scale = 1.0 / 32768.0
        self._spec_offset = 0.0
        self._spec_rate = 44100
        self._spec_decim = 1
        self._spec_ready_logged = False
        self._spec_cache = {}

        if HAS_MULTIMEDIA:
            self.player = QMediaPlayer(self)
            self.audio_output = QAudioOutput(self)
            self.player.setAudioOutput(self.audio_output)

            volume_pct = self.settings.value("volume", 80, type=int)
            self.audio_output.setVolume(max(0, min(100, volume_pct)) / 100.0)

            self.player.positionChanged.connect(self._on_position_changed)
            self.player.durationChanged.connect(self._on_duration_changed)
            self.player.mediaStatusChanged.connect(self._on_media_status_changed)
            self.player.playbackStateChanged.connect(self._on_playback_state_changed)
            self.player.errorOccurred.connect(self._on_player_error)
            self.player.metaDataChanged.connect(self._on_player_metadata)

            self.spec_decoder = QAudioDecoder(self)
            self.spec_decoder.bufferReady.connect(self._on_spec_buffer)
            self.spec_decoder.error.connect(self._on_spec_error)
            self.spec_decoder.finished.connect(self._on_spec_finished)
        else:
            self.player = None
            self.audio_output = None
            self.spec_decoder = None

        self._spec_timer = QTimer(self)
        self._spec_timer.setInterval(30)
        self._spec_timer.timeout.connect(self._update_spectrum)

    def _repeat_label(self):
        return {
            "off": "Repeat: Off",
            "all": "Repeat: All",
            "one": "Repeat: One",
        }[self.repeat_mode]

    def _build_player_bar(self):
        bar = DiscGroupBox("Now Playing", resource_path("disc.gif"))
        outer = QVBoxLayout(bar)

        info_row = QHBoxLayout()

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(56, 56)
        window = self.palette().window().color()
        text = self.palette().windowText().color()
        ghost = QColor(
            (window.red() * 85 + text.red() * 15) // 100,
            (window.green() * 85 + text.green() * 15) // 100,
            (window.blue() * 85 + text.blue() * 15) // 100,
        )
        self.cover_label.setStyleSheet(f"background-color: {ghost.name()};")
        info_row.addWidget(self.cover_label)
        bar.anchor = self.cover_label
        info_row.addSpacing(28)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        self.now_playing_title = QLabel("Nothing playing")
        self.now_playing_title.setStyleSheet("font-weight: bold;")
        self.now_playing_sub = QLabel("Press Play to start the library")
        self.now_playing_sub.setStyleSheet("color: gray; font-size: 11px;")
        self.now_playing_sub.setWordWrap(True)
        text_col.addWidget(self.now_playing_title)
        text_col.addWidget(self.now_playing_sub)

        self.scope_banner = QPushButton()
        self.scope_banner.setFlat(True)
        self.scope_banner.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scope_banner.setStyleSheet(
            "text-align: left; color: palette(link); padding: 0;"
            "font-size: 11px; border: none;"
        )
        self.scope_banner.clicked.connect(self.on_play_library)
        self.scope_banner.hide()
        text_col.addWidget(self.scope_banner)

        info_row.addLayout(text_col, stretch=1)

        self.analyzer = AnalyzerWidget()
        info_row.addWidget(self.analyzer, stretch=1)

        outer.addLayout(info_row)

        seek_row = QHBoxLayout()
        self.elapsed_label = QLabel("0:00")
        self.duration_label = QLabel("0:00")
        self.seek_slider = ClickSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        seek_row.addWidget(self.elapsed_label)
        seek_row.addWidget(self.seek_slider, stretch=1)
        seek_row.addWidget(self.duration_label)
        outer.addLayout(seek_row)

        controls_row = QHBoxLayout()

        self.prev_btn = QToolButton()
        self.prev_btn.setText("\u23ee\ufe0e")
        self.prev_btn.setToolTip("Previous")
        self.prev_btn.clicked.connect(self.on_prev_track)
        controls_row.addWidget(self.prev_btn)

        self.play_pause_btn = QToolButton()
        self.play_pause_btn.setText("Play")
        self.play_pause_btn.clicked.connect(self.on_play_pause)
        controls_row.addWidget(self.play_pause_btn)

        self.next_btn = QToolButton()
        self.next_btn.setText("\u23ed\ufe0e")
        self.next_btn.setToolTip("Next")
        self.next_btn.clicked.connect(self.on_next_track)
        controls_row.addWidget(self.next_btn)

        self.shuffle_play_btn = QToolButton()
        self.shuffle_play_btn.setCheckable(True)
        self.shuffle_play_btn.setChecked(self.shuffle_enabled)
        self.shuffle_play_btn.setText("Shuffle")
        self.shuffle_play_btn.toggled.connect(self.on_shuffle_toggled)
        controls_row.addWidget(self.shuffle_play_btn)

        self.repeat_btn = QToolButton()
        self.repeat_btn.setText(self._repeat_label())
        self.repeat_btn.clicked.connect(self.on_repeat_cycle)
        controls_row.addWidget(self.repeat_btn)

        controls_row.addStretch(1)

        controls_row.addWidget(QLabel("Vol"))
        self.volume_slider = ClickSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(110)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(self.settings.value("volume", 80, type=int))
        self.volume_slider.valueChanged.connect(self.on_volume_changed)
        controls_row.addWidget(self.volume_slider)

        outer.addLayout(controls_row)

        if not HAS_MULTIMEDIA:
            bar.setEnabled(False)
            bar.setTitle("Now Playing (install PyQt6.QtMultimedia to enable)")

        self._update_transport_enabled(False)
        return bar

    def _update_scope_banner(self):
        if self._queue_scope is None:
            self.scope_banner.hide()
            return
        folder, include_disabled = self._queue_scope
        suffix = " (disabled included)" if include_disabled else ""
        self.scope_banner.setText(
            f"Playing folder: {folder}{suffix} - click to play whole library"
        )
        self.scope_banner.show()

    def _update_transport_enabled(self, has_queue):
        self.prev_btn.setEnabled(has_queue)
        self.next_btn.setEnabled(has_queue)

    def _build_queue(self, start_path=None, folder=None, include_disabled=False):
        root = os.path.join(self.base_dir, folder) if folder else self.base_dir
        files = [
            f for f in get_mp3_files(root) if include_disabled or not is_disabled(f)
        ]
        if start_path and start_path not in files:
            files.append(start_path)
        files.sort(key=strip_disable_suffix)
        if not files:
            return [], -1

        if self.shuffle_enabled:
            queue = files[:]
            random.shuffle(queue)
            if start_path and start_path in queue:
                queue.remove(start_path)
                queue.insert(0, start_path)
            start_index = 0
        else:
            queue = files
            start_index = queue.index(start_path) if start_path in queue else 0

        return queue, start_index

    def on_play_library(self):
        if not HAS_MULTIMEDIA:
            QMessageBox.warning(
                self,
                "Playback",
                "PyQt6.QtMultimedia is not installed, so playback is unavailable.",
            )
            return
        queue, index = self._build_queue()
        if not queue:
            self.log_msg("No enabled songs to play.")
            return
        self.queue = queue
        self._queue_scope = None
        self._update_scope_banner()
        self._play_index(index)

    def on_folder_context_menu(self, pos):
        item = self.folder_list.itemAt(pos)
        if item is None:
            return
        folder = item.data(0, Qt.ItemDataRole.UserRole)
        enabled, disabled = folder_counts(os.path.join(self.base_dir, folder))

        menu = QMenu(self)
        play_action = menu.addAction(f"Play folder ({track_count(enabled)})")
        play_action.setEnabled(HAS_MULTIMEDIA and enabled > 0)
        play_all_action = menu.addAction(
            f"Play folder (disabled included, {track_count(enabled + disabled)})"
        )
        play_all_action.setEnabled(HAS_MULTIMEDIA and disabled > 0)

        chosen = menu.exec(self.folder_list.viewport().mapToGlobal(pos))
        if chosen is play_action:
            self.on_play_folder(folder, include_disabled=False)
        elif chosen is play_all_action:
            self.on_play_folder(folder, include_disabled=True)

    def on_play_folder(self, folder, include_disabled=False):
        if not HAS_MULTIMEDIA:
            QMessageBox.warning(
                self,
                "Playback",
                "PyQt6.QtMultimedia is not installed, so playback is unavailable.",
            )
            return
        queue, index = self._build_queue(
            folder=folder, include_disabled=include_disabled
        )
        if not queue:
            self.log_msg(f"No tracks to play in '{folder}'.")
            return
        self.queue = queue
        self._queue_scope = (folder, include_disabled)
        self._update_scope_banner()
        suffix = ", disabled included" if include_disabled else ""
        self.log_msg(f"Playing folder '{folder}' ({track_count(len(queue))}{suffix}).")
        self._play_index(index)

    def on_song_context_menu(self, pos):
        item = self.song_list.itemAt(pos)
        if item is None:
            return
        self.song_list.setCurrentItem(item)
        path = self._song_path(item)
        disabled = is_disabled(path)

        menu = QMenu(self)
        play_action = menu.addAction(
            "Play (without enabling)" if disabled else "Play From Here"
        )
        play_action.setEnabled(HAS_MULTIMEDIA)
        toggle_action = menu.addAction("Enable track" if disabled else "Disable track")
        edit_action = menu.addAction("Edit Tags...")
        edit_action.setEnabled(HAS_MUTAGEN)
        if not HAS_MUTAGEN:
            edit_action.setToolTip("Install mutagen to edit tags")

        chosen = menu.exec(self.song_list.viewport().mapToGlobal(pos))
        if chosen is play_action:
            self.on_play_selected_track()
        elif chosen is toggle_action:
            self.on_toggle_track()
        elif chosen is edit_action:
            self.on_edit_tags()

    def on_edit_tags(self):
        item = self.song_list.currentItem()
        if item is None:
            return
        path = self._song_path(item)
        if (
            self.player is not None
            and path == self._current_track_path
            and self.player.playbackState() != QMediaPlayer.PlaybackState.StoppedState
        ):
            QMessageBox.information(
                self,
                "Edit Tags",
                "Stop playback of this track before editing its tags.",
            )
            return
        try:
            values = read_editable_tags(path)
        except TagEditError as exc:
            QMessageBox.warning(self, "Edit Tags", str(exc))
            return

        try:
            current_cover, _mime = read_embedded_cover(path)
        except TagEditError:
            current_cover = None

        name = os.path.basename(strip_disable_suffix(path))
        dialog = TagEditDialog(name, values, current_cover, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        cover_change = dialog.cover_change()
        changed_fields = False
        try:
            with record_action("Edit tags"):
                old = write_tags(path, dialog.values())
                if old is not None:
                    changed_fields = True
                    record_tag_change(path, old_values=old)
                if cover_change is not None:
                    prev_bytes, prev_mime = read_embedded_cover(path)
                    if cover_change[0] == "set":
                        write_cover(path, cover_change[1], cover_change[2])
                    else:
                        write_cover(path, None)
                    record_tag_change(path, old_cover=(prev_bytes, prev_mime))
        except TagEditError as exc:
            QMessageBox.warning(self, "Edit Tags", str(exc))
            self._update_undo_action()
            return

        parts = []
        if changed_fields:
            parts.append("tags")
        if cover_change is not None:
            parts.append("cover")
        self._song_cache.pop(path, None)
        self._render_songs(keep_selection=True)
        if parts:
            self.log_msg(f"Updated {' and '.join(parts)} for '{name}'.")
        else:
            self.log_msg(f"No changes for '{name}'.")
        self._update_undo_action()

    def on_play_selected_track(self):
        item = self.song_list.currentItem()
        if item is None:
            return
        path = self._song_path(item)
        if not HAS_MULTIMEDIA:
            QMessageBox.warning(
                self,
                "Playback",
                "PyQt6.QtMultimedia is not installed, so playback is unavailable.",
            )
            return
        queue, index = self._build_queue(start_path=path)
        if not queue:
            return
        self.queue = queue
        self._queue_scope = None
        self._update_scope_banner()
        self._play_index(index)

    def _playable_path(self, path):
        if not is_disabled(path):
            return path
        if getattr(self, "_tmp_dir", None) is None:
            self._tmp_dir = tempfile.mkdtemp(prefix="neutm_")
        link = os.path.join(
            self._tmp_dir,
            f"{abs(hash(path)):x}_{os.path.basename(strip_disable_suffix(path))}",
        )
        if not os.path.exists(link):
            try:
                os.symlink(os.path.abspath(path), link)
            except (OSError, NotImplementedError):
                shutil.copy2(path, link)
        return link

    def _play_index(self, index):
        if not self.queue or index < 0 or index >= len(self.queue):
            return
        self.queue_index = index
        path = self.queue[index]
        self._current_track_path = path
        source = self._playable_path(path)
        self._current_tags = read_track_tags(source, path)
        self._scrobble_timer.stop()
        self._update_now_playing_display()

        self._start_spectrum_analysis(source)
        self.player.setSource(QUrl.fromLocalFile(os.path.abspath(source)))
        self.player.play()

        self._track_start_epoch = int(time.time())
        self._maybe_now_playing_scrobble()
        self._schedule_scrobble()
        self._update_transport_enabled(True)

    def _scaled_cover(self, img):
        size = self.cover_label.width()
        return QPixmap.fromImage(
            img.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _update_now_playing_display(self):
        tags = self._current_tags or {}
        title = tags.get("title") or os.path.basename(self._current_track_path or "")
        artist = tags.get("artist") or ""
        album = tags.get("album") or ""

        self.now_playing_title.setText(title)
        sub_parts = [p for p in (artist, album) if p]
        if sub_parts:
            self.now_playing_sub.setText(" - ".join(sub_parts))
        else:
            self.now_playing_sub.setText(
                os.path.relpath(self._current_track_path, self.base_dir)
            )

        self.cover_label.clear()
        cover = tags.get("cover")
        if cover:
            img = QImage.fromData(bytes(cover))
            if not img.isNull():
                self.cover_label.setPixmap(self._scaled_cover(img))

    def on_play_pause(self):
        if not HAS_MULTIMEDIA:
            return
        if not self.queue:
            self.on_play_library()
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def on_next_track(self):
        self._advance(1, user_initiated=True)

    def on_prev_track(self):
        if self.player and self.player.position() > 3000:
            self.player.setPosition(0)
            return
        self._advance(-1, user_initiated=True)

    def _advance(self, direction, user_initiated=False, from_end_of_media=False):
        if not self.queue:
            return

        if from_end_of_media and self.repeat_mode == "one":
            self._play_index(self.queue_index)
            return

        new_index = self.queue_index + direction

        if new_index < 0:
            new_index = len(self.queue) - 1 if self.repeat_mode == "all" else 0
        elif new_index >= len(self.queue):
            if self.repeat_mode == "all":
                new_index = 0
            else:
                self.player.stop()
                self.log_msg("Reached end of queue.")
                return

        self._play_index(new_index)

    def on_shuffle_toggled(self, checked):
        self.shuffle_enabled = checked
        self.settings.setValue("shuffle_enabled", checked)
        if self.queue and 0 <= self.queue_index < len(self.queue):
            current_path = self.queue[self.queue_index]
            scope = self._queue_scope
            queue, index = self._build_queue(
                start_path=current_path,
                folder=scope[0] if scope else None,
                include_disabled=scope[1] if scope else False,
            )
            self.queue = queue
            self.queue_index = index

    def on_repeat_cycle(self):
        order = ["off", "all", "one"]
        idx = order.index(self.repeat_mode)
        self.repeat_mode = order[(idx + 1) % len(order)]
        self.settings.setValue("repeat_mode", self.repeat_mode)
        self.repeat_btn.setText(self._repeat_label())

    def on_volume_changed(self, value):
        if self.audio_output is not None:
            self.audio_output.setVolume(value / 100.0)
        self.settings.setValue("volume", value)

    def _on_seek_pressed(self):
        self._seek_slider_pressed = True

    def _on_seek_released(self):
        self._seek_slider_pressed = False
        if self.player is not None:
            self.player.setPosition(self.seek_slider.value())

    def _on_position_changed(self, pos_ms):
        if not self._seek_slider_pressed:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(pos_ms)
            self.seek_slider.blockSignals(False)
        self.elapsed_label.setText(format_time(pos_ms / 1000))

    def _on_duration_changed(self, dur_ms):
        self.seek_slider.setRange(0, max(0, dur_ms))
        self.duration_label.setText(format_time(dur_ms / 1000))

    def _on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._advance(1, from_end_of_media=True)

    def _on_playback_state_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_pause_btn.setText("Pause" if playing else "Play")
        self.analyzer.set_active(playing)
        self.player_bar.set_spinning(playing)

        if playing:
            self._spec_timer.start()
        else:
            self._spec_timer.stop()
            self.analyzer.set_levels([0.0] * AnalyzerWidget.NUM_BANDS)

    def _on_player_error(self, _error, error_string):
        if error_string:
            self.log_msg(f"Playback error: {error_string}")

    def _on_player_metadata(self):
        if self._current_tags and self._current_tags.get("cover"):
            return
        try:
            md = self.player.metaData()
            for key in (
                QMediaMetaData.Key.CoverArtImage,
                QMediaMetaData.Key.ThumbnailImage,
            ):
                img = md.value(key)
                if isinstance(img, QImage) and not img.isNull():
                    self.cover_label.setPixmap(self._scaled_cover(img))
                    return
        except Exception:
            pass

    def _start_spectrum_analysis(self, path):
        self._spec_samples = array("h")
        self._spec_decim = 1
        self._spec_ready_logged = False
        if self.spec_decoder is None:
            return
        self.spec_decoder.stop()
        self.spec_decoder.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
        self.spec_decoder.start()

    def _on_spec_error(self, *_args):
        self.log_msg(
            f"Visualizer: could not decode this file ({self.spec_decoder.errorString()})."
        )

    def _on_spec_finished(self):
        if not len(self._spec_samples):
            pass

    def _on_spec_buffer(self):
        dec = self.spec_decoder
        while dec.bufferAvailable():
            buf = dec.read()
            n_bytes = buf.byteCount()
            if not buf.isValid() or n_bytes <= 0:
                continue
            try:
                ptr = buf.constData()
                ptr.setsize(n_bytes)
                raw = bytes(ptr.asarray(n_bytes))
                fmt = buf.format()
                sf = fmt.sampleFormat()
                code, scale, offset = {
                    QAudioFormat.SampleFormat.Int16: ("h", 1.0 / 32768.0, 0.0),
                    QAudioFormat.SampleFormat.Int32: ("i", 1.0 / 2147483648.0, 0.0),
                    QAudioFormat.SampleFormat.Float: ("f", 1.0, 0.0),
                    QAudioFormat.SampleFormat.UInt8: ("B", 1.0 / 128.0, 128.0),
                }.get(sf, (None, 0, 0))
                if code is None:
                    continue

                chunk = array(code)
                chunk.frombytes(raw[: len(raw) - len(raw) % chunk.itemsize])

                if not len(self._spec_samples) or self._spec_samples.typecode != code:
                    self._spec_samples = array(code)
                    rate = fmt.sampleRate() or 44100
                    self._spec_decim = 1 if HAS_NUMPY else max(1, round(rate / 11025))
                    self._spec_rate = rate / self._spec_decim
                    self._spec_scale, self._spec_offset = scale, offset

                channels = max(1, fmt.channelCount())
                mono = chunk[0::channels] if channels > 1 else chunk
                self._spec_samples.extend(
                    mono[:: self._spec_decim] if self._spec_decim > 1 else mono
                )
            except Exception as exc:
                if not self._spec_ready_logged:
                    self.log_msg(f"Visualizer decode error: {exc}")
                    self._spec_ready_logged = True

    def _band_levels(self, start):
        bands = AnalyzerWidget.NUM_BANDS
        if HAS_NUMPY:
            n = SPEC_WIN
            seg = np.asarray(self._spec_samples[start : start + n], dtype=np.float32)
            if len(seg) < n:
                return None
            seg = (seg - self._spec_offset) * self._spec_scale
            win = self._spec_cache.get("hann")
            if win is None:
                win = self._spec_cache["hann"] = np.hanning(n).astype(np.float32)
            mags = np.abs(np.fft.rfft(seg * win)) * (4.0 / n)
            key = ("edges", int(self._spec_rate))
            edges = self._spec_cache.get(key)
            if edges is None:
                edges = self._spec_cache[key] = spectrum_band_edges(self._spec_rate)
            amps = [float(mags[edges[i] : edges[i + 1]].max()) for i in range(bands)]
        else:
            n = 512
            seg = self._spec_samples[start : start + n]
            if len(seg) < n:
                return None
            key = ("goertzel", int(self._spec_rate))
            cached = self._spec_cache.get(key)
            if cached is None:
                hann = [
                    0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)
                ]
                f_hi = min(5000.0, self._spec_rate / 2 - 1)
                coefs = [
                    2
                    * math.cos(
                        2
                        * math.pi
                        * (40.0 * (f_hi / 40.0) ** ((i + 0.5) / bands))
                        / self._spec_rate
                    )
                    for i in range(bands)
                ]
                cached = self._spec_cache[key] = (hann, coefs)
            hann, coefs = cached
            off, sc = self._spec_offset, self._spec_scale
            xs = [(x - off) * sc * h for x, h in zip(seg, hann)]
            amps = []
            for c in coefs:
                s1 = s2 = 0.0
                for x in xs:
                    s0 = x + c * s1 - s2
                    s2 = s1
                    s1 = s0
                power = s1 * s1 + s2 * s2 - c * s1 * s2
                amps.append(math.sqrt(power) * 4.0 / n if power > 0 else 0.0)

        levels = []
        for i, amp in enumerate(amps):
            db = 20.0 * math.log10(amp + 1e-9) + i * 0.5
            levels.append(max(0.0, min(1.0, (db + 70.0) / 55.0)))
        return levels

    def _update_spectrum(self):
        if self.player is None or not len(self._spec_samples):
            return
        pos_samples = int(self.player.position() / 1000.0 * self._spec_rate)
        half = (SPEC_WIN if HAS_NUMPY else 512) // 2
        levels = self._band_levels(max(0, pos_samples - half))
        if levels is not None:
            self.analyzer.set_levels(levels)

    def _run_lastfm_job(self, job):
        worker = LastFmWorker(job, self)
        worker.failed.connect(lambda msg: self.log_msg(f"Last.fm: {msg}"))
        worker.finished.connect(
            lambda: (
                self._lastfm_workers.remove(worker)
                if worker in self._lastfm_workers
                else None
            )
        )
        worker.finished.connect(worker.deleteLater)
        self._lastfm_workers.append(worker)
        worker.start()

    def _maybe_now_playing_scrobble(self):
        if not self.lastfm_enabled:
            self.log_msg("Last.fm: skipped, scrobbling not enabled")
            return
        if not self.lastfm_client:
            self.log_msg("Last.fm: skipped, no API key/secret set")
            return
        if not self.lastfm_client.session_key:
            self.log_msg("Last.fm: skipped, not connected (no session key)")
            return
        tags = self._current_tags or {}
        artist, title = tags.get("artist"), tags.get("title")
        if not artist or not title:
            self.log_msg(
                f"Last.fm: skipped, missing tag (artist={artist!r}, title={title!r})"
            )
            return
        album, duration = tags.get("album"), tags.get("duration")
        client = self.lastfm_client
        self.log_msg(f"Last.fm: sending now playing: {artist} - {title}")
        self._run_lastfm_job(
            lambda: client.update_now_playing(artist, title, album, duration)
        )

    def _schedule_scrobble(self):
        tags = self._current_tags or {}
        duration = tags.get("duration")
        if not duration or duration < 30:
            return
        delay = min(duration / 2.0, 240.0)
        self._scrobble_timer.start(int(delay * 1000))

    def _do_scrobble(self):
        if not (
            self.lastfm_enabled
            and self.lastfm_client
            and self.lastfm_client.session_key
        ):
            return
        tags = self._current_tags or {}
        artist, title = tags.get("artist"), tags.get("title")
        if not artist or not title or not self._track_start_epoch:
            return
        album, duration = tags.get("album"), tags.get("duration")
        client = self.lastfm_client
        start_epoch = self._track_start_epoch
        self._run_lastfm_job(
            lambda: client.scrobble(artist, title, start_epoch, album, duration)
        )

    def on_lastfm_setup(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Last.fm API Credentials")
        form = QFormLayout(dialog)

        key_edit = QLineEdit(self.settings.value("lastfm_api_key", "", type=str))
        secret_edit = QLineEdit(self.settings.value("lastfm_api_secret", "", type=str))
        secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API Key:", key_edit)
        form.addRow("API Secret:", secret_edit)

        hint = QLabel(
            "Create a free API app at last.fm/api/account/create to get these."
        )
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        form.addRow(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        api_key = key_edit.text().strip()
        api_secret = secret_edit.text().strip()
        if not api_key or not api_secret:
            QMessageBox.warning(
                self, "Last.fm", "Both an API key and secret are required."
            )
            return

        self.settings.setValue("lastfm_api_key", api_key)
        self.settings.setValue("lastfm_api_secret", api_secret)
        session_key = self.settings.value("lastfm_session_key", "", type=str)
        self.lastfm_client = LastFmClient(api_key, api_secret, session_key or None)
        self.log_msg("Saved Last.fm API credentials.")

    def on_lastfm_connect(self):
        if not self.lastfm_client:
            QMessageBox.warning(
                self,
                "Last.fm",
                "Set your API key/secret first (Last.fm > Set API Key/Secret...).",
            )
            return
        try:
            token = self.lastfm_client.get_token()
        except Exception as exc:
            QMessageBox.warning(self, "Last.fm", f"Could not reach Last.fm:\n{exc}")
            return

        webbrowser.open(self.lastfm_client.auth_url(token))
        proceed = QMessageBox.question(
            self,
            "Last.fm",
            "A browser window opened so you can authorize Neutm.\n\n"
            "Once you've approved access on the Last.fm site, click Yes to finish connecting.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if proceed != QMessageBox.StandardButton.Yes:
            return

        try:
            session_key = self.lastfm_client.get_session(token)
        except Exception as exc:
            QMessageBox.warning(
                self, "Last.fm", f"Could not complete authorization:\n{exc}"
            )
            return

        self.settings.setValue("lastfm_session_key", session_key)
        self.log_msg("Connected to Last.fm.")
        QMessageBox.information(self, "Last.fm", "Connected to Last.fm successfully.")

    def on_lastfm_disconnect(self):
        self.settings.setValue("lastfm_session_key", "")
        if self.lastfm_client:
            self.lastfm_client.session_key = None
        self.log_msg("Disconnected from Last.fm.")

    def on_lastfm_toggle_enabled(self, checked):
        self.lastfm_enabled = checked
        self.settings.setValue("lastfm_enabled", checked)


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
        "--gamble",
        action="store_true",
        help="Randomly enable/disable individual tracks across the whole library",
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
        print("Gambling tracks...")

        result = gamble(base)

        if result:
            print("\nChanges:")
            for track, action in result:
                print(f"  {action.upper():<8} {track}")

            print(f"\n[OK] Gambled {len(result)} track(s)")
        else:
            print("[ERROR] Not enough tracks to gamble")

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
