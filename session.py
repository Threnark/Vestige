"""
session.py
----------
Holds everything the running desktop app knows, in MEMORY ONLY:

    * the list of email accounts (with their app passwords, if entered)
    * the database of scanned emails and results

Credentials are not stored here "in memory but also on disk" - there is no disk
copy at all. AI settings (API key, local server URL, model) live in the GUI's
form fields and are likewise never written anywhere. Closing the program forgets
all of it. There is deliberately no "remember me" / autofill feature.

WRITE MODE (off by default, opt-in)
    Keeps the *results database* in a normal file as the scan proceeds, so a
    crash or an accidental close doesn't cost you the scanning and AI calls
    already done. It saves results ONLY - never credentials, API keys or server
    URLs. The file lives in a clearly named folder next to the program
    (Vestige_DATA/session.db) where it is easy to see, move or delete.

    The file contains sensitive information (sender names, subject lines,
    short body excerpts, the addresses you used). The GUI shows a warning before
    turning this on, and offers to delete the file when you close the app.
"""

from __future__ import annotations

import os
import sqlite3

import db
import paths
from models import Account


class SessionError(Exception):
    """A problem with write mode; the message is written for the end user."""


def delete_session_files(db_path: str) -> list:
    """Delete the session database and any SQLite side files. Returns the paths removed.
    NOTE: deleting a file does not securely erase its data from the drive - see
    the 'Privacy and data handling' section of the README."""
    removed, failed = [], []
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = db_path + suffix
        if os.path.exists(candidate):
            try:
                os.remove(candidate)
                removed.append(candidate)
            except OSError as exc:
                failed.append(f"Couldn't delete {candidate}: {exc}")       # carry on: try the other files too
    if failed:
        raise SessionError("\n".join(failed))
    return removed


def _new_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


class Session:
    def __init__(self):
        self.accounts: list = []
        self.conn: sqlite3.Connection = db.open_database(None)     # in memory
        self.write_mode = False
        self.db_path: str | None = None

    # -- accounts ----------------------------------------------------------
    def upsert_account(self, account: Account, replacing: str | None = None) -> None:
        """Add an account, or replace the one called `replacing` (for renames/edits)."""
        drop = {account.name, replacing} - {None}
        self.accounts = [a for a in self.accounts if a.name not in drop]
        self.accounts.append(account)

    def remove_account(self, name: str) -> None:
        self.accounts = [a for a in self.accounts if a.name != name]

    def get_account(self, name: str):
        return next((a for a in self.accounts if a.name == name), None)

    # -- write mode --------------------------------------------------------
    def enable_write_mode(self, data_dir: str, overwrite: bool = False) -> str:
        """Copy the in-memory database into <data_dir>/session.db and keep using
        that file from now on. Raises FileExistsError if a saved session is
        already there and overwrite is False (so the caller can ask the user)."""
        if self.write_mode:
            return self.db_path
        ok, message = paths.check_writable(data_dir, create=True)
        if not ok:
            raise SessionError(message)
        path = paths.session_db_path(data_dir)
        if os.path.exists(path):
            if not overwrite:
                raise FileExistsError(path)
            delete_session_files(path)

        db.prepare_private_file(path)          # owner-only from the moment it exists
        dest = sqlite3.connect(path, check_same_thread=False)
        dest.row_factory = sqlite3.Row
        try:
            db.copy_database(self.conn, dest)
        except Exception:
            dest.close()
            try:
                delete_session_files(path)
            except SessionError:
                pass                           # the real problem is the one to report, not a leftover file
            raise
        old, self.conn = self.conn, dest
        old.close()
        self.write_mode, self.db_path = True, path
        return path

    def disable_write_mode(self, delete_file: bool) -> list:
        """Go back to memory-only. The data is copied back into memory first, so
        nothing is lost from the current session. Returns the files deleted.

        The session is switched over BEFORE the file is touched. Deleting can fail (on Windows, an
        antivirus scanner or a cloud-sync app may be holding the file), and when it did, the old
        order left the session pointing at a closed database with every result in it unreachable.
        Now the session is always left working: if the file can't be deleted, SessionError says so
        and says where the file is, and the results are still there."""
        if not self.write_mode:
            return []
        memory = _new_memory_db()
        db.copy_database(self.conn, memory)
        old, path = self.conn, self.db_path
        self.conn, self.write_mode, self.db_path = memory, False, None
        try:
            old.close()
        except sqlite3.Error:
            pass                               # the data is already safe in memory
        if not delete_file:
            return []
        try:
            return delete_session_files(path)
        except SessionError as exc:
            raise SessionError(
                "Write mode is now off and your results are safe in memory, but the saved file could not "
                f"be deleted.\n\n{exc}\n\nClose anything that may be using it (an antivirus scan, a "
                "cloud-sync app) and delete it yourself, or use \"Delete saved data\" on the Setup tab "
                "once it is free.") from None

    def resume_from(self, db_path: str) -> None:
        """Continue a previously saved session (write mode on). Raises
        db.DatabaseVersionError if the file came from an older version."""
        new_conn = db.open_database(db_path)
        old, self.conn = self.conn, new_conn
        old.close()
        self.write_mode, self.db_path = True, db_path

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error:
            pass
