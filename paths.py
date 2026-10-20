"""
paths.py
--------
Answers two questions, without ever creating anything:

  1. Where is the application? (so optional saved data can sit right next to it,
     in plain sight, instead of being hidden in a profile folder)
  2. Is a folder safe/possible to write into?

By default the app writes NOTHING to disk. These helpers are only used when the
user turns on "write mode" (progress saved while the scan runs) or chooses an
export folder.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

# The one visible folder write mode uses, created next to the program itself.
DATA_DIR_NAME = "Vestige_DATA"
SESSION_DB_NAME = "session.db"


def app_base_dir() -> str:
    """Folder containing the program: the .exe (Windows), the .app (macOS) or
    the source folder when running from source.

    When packaged with PyInstaller, `sys.executable` is the real program file
    (NOT the temporary unpack folder `sys._MEIPASS`), so its folder is where the
    user actually put the app.
    """
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        # macOS: /somewhere/Name.app/Contents/MacOS/Name -> use the folder that
        # contains Name.app, because the user can see and manage that folder.
        marker = ".app" + os.sep + "Contents" + os.sep + "MacOS"
        idx = exe.find(marker)
        if idx != -1:
            return os.path.dirname(exe[: idx + len(".app")])
        return os.path.dirname(exe)
    return os.path.dirname(os.path.abspath(__file__))


def default_data_dir() -> str:
    """Where write mode saves its files: <program folder>/Vestige_DATA."""
    return os.path.join(app_base_dir(), DATA_DIR_NAME)


def session_db_path(data_dir: str) -> str:
    return os.path.join(data_dir, SESSION_DB_NAME)


def check_writable(folder: str, create: bool = False) -> tuple:
    """Return (ok, message). Actually tries to write a tiny temporary file,
    because permission flags (os.access) are unreliable on Windows and on
    network drives. The test file is removed immediately.

    `create=True` makes the folder first if it doesn't exist (only used for the
    write-mode data folder, never for an export folder the user must pick).
    """
    try:
        if create:
            os.makedirs(folder, exist_ok=True)
        if not os.path.isdir(folder):
            return False, f"The folder does not exist (or the drive isn't connected):\n{folder}"
        fd, tmp_path = tempfile.mkstemp(prefix=".write_test_", dir=folder)
        os.close(fd)
        os.remove(tmp_path)
        return True, ""
    except PermissionError:
        return False, f"This program isn't allowed to write to:\n{folder}"
    except OSError as exc:
        return False, f"Can't write to:\n{folder}\n\n{exc}"


# Folder-name fragments used by cloud-sync clients. If saved data lands inside
# one of these, it would be copied to a cloud service automatically - exactly
# what a privacy-minded user may not expect. We only WARN; the user decides.
_SYNC_MARKERS = (
    ("onedrive", "OneDrive"),
    ("dropbox", "Dropbox"),
    ("icloud drive", "iCloud Drive"),
    ("iclouddrive", "iCloud Drive"),
    ("mobile documents", "iCloud Drive"),
    ("google drive", "Google Drive"),
    ("googledrive", "Google Drive"),
    ("pcloud", "pCloud"),
    ("sync.com", "Sync.com"),
    ("mega", "MEGA"),
    ("box sync", "Box"),
)


def cloud_sync_warning(path: str):
    """Return the name of a cloud-sync service if `path` looks like it's inside
    one, otherwise None. A heuristic: a hint, not a guarantee either way."""
    parts = [p.lower() for p in os.path.abspath(path).replace("\\", "/").split("/")]
    for part in parts:
        for marker, label in _SYNC_MARKERS:
            if marker == "mega":
                # "mega" appears in many unrelated names; require an exact match.
                if part == "mega":
                    return label
            elif marker in part:
                return label
    return None


_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "signups") -> str:
    """Make a user-typed file name safe on Windows, macOS and Linux."""
    cleaned = _BAD_FILENAME_CHARS.sub("_", (name or "").strip()).strip(" .")
    return cleaned or fallback
