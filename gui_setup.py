"""
gui_setup.py
------------
The Setup page: where you choose how Vestige stores things, which AI it uses and
which email accounts it reads. Mirrors the first page of the concept design.

Nothing on this page is ever saved to disk. Account passwords, the AI API key and
local-AI addresses live in memory only (see session.py). The only file this page
can cause to be written is the optional write-mode results file, after an explicit
warning.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk

import customtkinter as ctk

import ai_providers as ai
import db
import paths
import help_content
import ui_dialogs as dialogs
import ui_help as help_ui
import ui_theme as theme
from models import (
    CLOUD_PRESETS, DEFAULT_ANTHROPIC_MODEL, DEFAULT_FOLDERS, KIND_ANTHROPIC, KIND_OPENAI, KNOWN_IMAP_HOSTS,
    LOCAL_PRESETS, MODE_CLOUD, MODE_LOCAL, PROVIDER_CHOICES, Account, ProviderSettings, is_microsoft_mail,
)
from session import SessionError, delete_session_files
from ui_common import DELETE_NOTE, open_folder

DEFAULT_CLOUD_PRESET = "Anthropic (Claude)"
DEFAULT_LOCAL_PRESET = "Ollama"


# ---------------------------------------------------------------------------
# Add / edit account dialog
# ---------------------------------------------------------------------------

class AccountDialog(ctk.CTkToplevel):
    """Modal form. Sets self.result to an Account on Save (None if cancelled).
    The password is optional: an account without one can still be used to import
    an exported mail file (.pst, .mbox, .eml), which needs no credentials at all.
    Outlook / Hotmail / Live accounts can ONLY be used that way, because Microsoft
    rejects every password-based IMAP login; the dialog says so and keeps no password."""

    def __init__(self, parent, existing: Account | None = None):
        super().__init__(parent, fg_color=theme.BG)
        self.title("Edit account" if existing else "Add account")
        self.resizable(False, False)
        self.result = None

        form = theme.frame(self)
        form.pack(padx=24, pady=(20, 6))

        def row(r: int, text: str) -> None:
            theme.label(form, text, anchor="e").grid(row=r, column=0, sticky="e", padx=(0, 10), pady=5)

        row(0, "Nickname:")
        self.name_var = tk.StringVar(value=existing.name if existing else "")
        theme.entry(form, 300, self.name_var).grid(row=0, column=1, columnspan=2, sticky="w")

        row(1, "Provider:")
        self.provider_var = tk.StringVar(value=existing.provider if existing else "gmail")
        theme.option_menu(form, PROVIDER_CHOICES, self.provider_var, self._on_provider_change,
                          width=200).grid(row=1, column=1, columnspan=2, sticky="w")
        help_ui.button(form, self._help_topic).grid(row=1, column=3, padx=(10, 0))

        row(2, "Email address:")
        self.email_var = tk.StringVar(value=existing.email if existing else "")
        theme.entry(form, 300, self.email_var).grid(row=2, column=1, columnspan=2, sticky="w")

        row(3, "App password:")
        self.password_var = tk.StringVar(value=existing.password if existing else "")
        self.pw_entry = theme.entry(form, 230, self.password_var, show="*")
        self.pw_entry.grid(row=3, column=1, sticky="w")
        self.show_pw_var = tk.BooleanVar(value=False)
        theme.checkbox(form, "show", self.show_pw_var, self._toggle_pw, width=60).grid(row=3, column=2, padx=(10, 0))
        help_ui.button(form, self._help_topic).grid(row=3, column=3, padx=(10, 0))
        self.pw_hint = theme.paragraph(form, "", small=True, muted=True, wraplength=330)
        self.pw_hint.grid(row=4, column=1, columnspan=2, sticky="w")

        row(5, "IMAP server:")
        self.server_var = tk.StringVar(value=(existing.imap_server if existing else "")
                                       or KNOWN_IMAP_HOSTS.get(self.provider_var.get(), ""))
        theme.entry(form, 300, self.server_var).grid(row=5, column=1, columnspan=2, sticky="w")
        help_ui.button(form, self._help_topic).grid(row=5, column=3, padx=(10, 0))

        row(6, "IMAP port:")
        self.port_var = tk.StringVar(value=str(existing.imap_port) if existing else "993")
        theme.entry(form, 80, self.port_var).grid(row=6, column=1, sticky="w")

        row(7, "Folders:")
        folders_default = ", ".join(existing.folders) if existing and existing.provider == "other" else ""
        self.folders_var = tk.StringVar(value=folders_default)
        theme.entry(form, 300, self.folders_var).grid(row=7, column=1, columnspan=2, sticky="w")
        theme.label(form, "Comma-separated. Blank = the provider's default folders.", muted=True, small=True).grid(
            row=8, column=1, columnspan=2, sticky="w")

        for var in (self.provider_var, self.email_var, self.server_var):
            var.trace_add("write", lambda *_: self._refresh_microsoft_state())
        self._refresh_microsoft_state()

        theme.label(self, "Nothing here is saved to disk. It is forgotten when you close the app.",
                    muted=True, small=True).pack(padx=24, pady=(6, 0), anchor="w")
        buttons = theme.frame(self)
        buttons.pack(padx=24, pady=(10, 20), anchor="e")
        theme.button(buttons, "Save", self._on_save, width=100).pack(side="left", padx=5)
        theme.button(buttons, "Cancel", self.destroy, width=100).pack(side="left", padx=5)

        self.after(250, lambda: theme.apply_icon(self))
        self.update_idletasks()
        self.transient(parent.winfo_toplevel())
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window()

    def _help_topic(self) -> str:
        """The help page for the account being filled in: the one for the chosen provider, or the
        Outlook/Hotmail page if the address or server is a Microsoft one."""
        return help_content.topic_for_account(self.provider_var.get(), self.email_var.get(), self.server_var.get())

    def _is_microsoft(self) -> bool:
        return self.provider_var.get() == "outlook" or is_microsoft_mail(self.email_var.get(), self.server_var.get())

    def _refresh_microsoft_state(self) -> None:
        """Outlook / Hotmail / Live / MSN: Microsoft rejects passwords for IMAP, so there is no
        point asking for one. Say why, and point at file import instead."""
        if self._is_microsoft():
            self.password_var.set("")
            self.pw_entry.configure(state="disabled")
            self.pw_hint.configure(
                fg=theme.PRIVACY_COLORS["lan"],
                text="Microsoft blocks passwords for Outlook / Hotmail / Live over IMAP, so no password is "
                     "needed (or possible). Import your exported .pst file instead: Run Pipeline > "
                     "1. Import mail file.")
        else:
            self.pw_entry.configure(state="normal")
            self.pw_hint.configure(fg=theme.TEXT_MUTED,
                                   text="Leave blank if you will only import exported mail files (.pst, .mbox, .eml).")

    def _on_provider_change(self, _value=None) -> None:
        provider = self.provider_var.get()
        host = KNOWN_IMAP_HOSTS.get(provider)
        if host:
            self.server_var.set(host)
        elif provider == "outlook":
            self.server_var.set("")                    # no IMAP server can work (see above)

    def _toggle_pw(self) -> None:
        self.pw_entry.configure(show="" if self.show_pw_var.get() else "*")

    def _on_save(self) -> None:
        name, email_addr = self.name_var.get().strip(), self.email_var.get().strip()
        password, server = self.password_var.get(), self.server_var.get().strip()
        if self._is_microsoft():
            password = ""                               # can never work; and nothing useless is kept in memory
        if not name or not email_addr:
            dialogs.showerror("Missing info", "A nickname and an email address are required.")
            return
        if password and not server:
            dialogs.showerror("Missing IMAP server",
                              "Enter an IMAP server, or pick gmail/yahoo/icloud to fill it in.")
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            dialogs.showerror("Invalid port", "IMAP port must be a number (usually 993).")
            return
        provider = self.provider_var.get()
        folders = [f.strip() for f in self.folders_var.get().split(",") if f.strip()]
        self.result = Account(name=name, provider=provider, email=email_addr, imap_server=server,
                              imap_port=port, password=password,
                              folders=folders or DEFAULT_FOLDERS.get(provider, ["INBOX"]))
        self.destroy()


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

class SetupPage(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self._wrapped: list = []            # labels whose wrap width follows the window
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)         # the accounts section takes any spare height

        banner = theme.paragraph(self, "Nothing on this tab is saved. Accounts, passwords, API keys and server "
                                       "addresses are held in memory only and are forgotten when you close the app.",
                                 small=True)
        banner.grid(row=0, column=0, sticky="ew", padx=20, pady=(8, 4))
        self._wrapped.append(banner)

        self._build_storage()
        self._build_ai()
        self._build_accounts()
        self.bind("<Configure>", self._rewrap)

    def _rewrap(self, event=None) -> None:
        width = getattr(event, "width", 0) or self.winfo_width()
        for lbl in self._wrapped:
            lbl.configure(wraplength=max(300, width - 60))

    # ---- storage / write mode ---------------------------------------------------
    def _build_storage(self) -> None:
        box = theme.frame(self)
        box.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 8))
        box.grid_columnconfigure(0, weight=1)
        help_ui.heading(box, "Storage", "privacy").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.write_mode_var = tk.BooleanVar(value=False)
        theme.checkbox(box, "Write Mode: save scan progress to a file so a crash or accidental close "
                            "doesn't lose it.", self.write_mode_var, self._toggle_write_mode, width=680).grid(
            row=1, column=0, columnspan=2, sticky="w")
        self.storage_status_var = tk.StringVar()
        status = theme.paragraph(box, textvariable=self.storage_status_var, small=True)
        status.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._wrapped.append(status)
        buttons = theme.frame(box)
        buttons.grid(row=2, column=1, sticky="e", pady=(4, 0))
        theme.button(buttons, "Delete saved data", self._delete_saved, width=140).pack(side="left", padx=(0, 10))
        theme.button(buttons, "Open Data Folder", self._open_data_folder, width=140).pack(side="left")
        self.refresh_storage_status()

    def refresh_storage_status(self) -> None:
        session = self.app.session
        self.write_mode_var.set(session.write_mode)
        if session.write_mode:
            self.storage_status_var.set(f"Saving results to: {session.db_path}  (passwords, keys and server "
                                        f"addresses are never saved)")
        else:
            self.storage_status_var.set("Memory only: nothing is written to disk. Closing the app discards "
                                        "everything unless you export it.")

    def _open_data_folder(self) -> None:
        session = self.app.session
        folder = os.path.dirname(session.db_path) if session.write_mode else paths.default_data_dir()
        if os.path.isdir(folder):
            open_folder(folder)
        else:
            dialogs.showinfo("No data folder", "Nothing has been saved, so there is no data folder yet.")

    def _delete_saved(self) -> None:
        session = self.app.session
        if self.app.busy:
            dialogs.showwarning("Busy", "Wait for the running job to finish first.")
            return
        if session.write_mode:
            self._turn_off_write_mode(ask_keep=False)
            return
        path = paths.session_db_path(paths.default_data_dir())
        if not os.path.exists(path):
            dialogs.showinfo("Nothing to delete", "There is no saved session.")
            return
        if dialogs.askyesno("Delete saved data", f"Delete the saved session file?\n\n{path}"):
            self.delete_files(path)

    def delete_files(self, path: str) -> None:
        try:
            removed = delete_session_files(path)
            dialogs.showinfo("Deleted", "Deleted:\n" + "\n".join(removed) + "\n\n" + DELETE_NOTE)
        except SessionError as exc:
            dialogs.showerror("Couldn't delete", str(exc))

    def _toggle_write_mode(self) -> None:
        if self.app.busy:
            self.refresh_storage_status()
            dialogs.showwarning("Busy", "Wait for the running job to finish first.")
            return
        if self.write_mode_var.get():
            self._turn_on_write_mode()
        else:
            self._turn_off_write_mode(ask_keep=True)

    def _turn_on_write_mode(self) -> None:
        session = self.app.session
        data_dir = paths.default_data_dir()
        warning = (
            "Write mode saves your scan RESULTS to a file on this computer, so a crash or accidental close "
            "doesn't cost you the scanning and AI work already done.\n\n"
            "SENSITIVE DATA WILL BE SAVED: sender names and addresses, email subject lines, short excerpts "
            "of email text, which of your addresses you used, and the AI's results.\n\n"
            "NEVER saved: passwords, API keys or AI server addresses.\n\n"
            f"Location (next to this program, so it's easy to find and delete):\n{data_dir}\n\n"
            "If this worries you, leave write mode off, or run the program from a drive you trust and delete "
            "the folder afterwards.")
        sync = paths.cloud_sync_warning(data_dir)
        if sync:
            warning += (f"\n\nWARNING: this location looks like it is inside {sync}, so the saved file may be "
                        f"uploaded to the cloud automatically.")
        if not dialogs.askokcancel("Turn on write mode?", warning):
            self.refresh_storage_status()
            return
        try:
            session.enable_write_mode(data_dir)
        except FileExistsError:
            choice = dialogs.ask_choice(
                "A saved session already exists",
                "A saved session is already in that folder. What would you like to do?\n\n"
                "(Resuming it replaces whatever is currently loaded in this session.)",
                ["Replace it with this session", "Resume the saved one instead", "Cancel"])
            try:
                if choice == "Replace it with this session":
                    session.enable_write_mode(data_dir, overwrite=True)
                elif choice == "Resume the saved one instead":
                    session.resume_from(paths.session_db_path(data_dir))
                    self.app.after_data_replaced()
            except (SessionError, db.DatabaseVersionError) as exc:
                dialogs.showerror("Couldn't turn on write mode", str(exc))
        except SessionError as exc:
            dialogs.showerror("Couldn't turn on write mode", str(exc))
        self.refresh_storage_status()

    def _turn_off_write_mode(self, ask_keep: bool) -> None:
        session = self.app.session
        path = session.db_path
        if ask_keep:
            choice = dialogs.ask_choice(
                "Turn off write mode?",
                f"Your results will stay in memory for this session. What should happen to the saved "
                f"file?\n\n{path}", ["Keep the file", "Delete the file", "Cancel"])
            if choice in (None, "Cancel"):
                self.refresh_storage_status()
                return
            delete = choice == "Delete the file"
        else:
            delete = True
        try:
            removed = session.disable_write_mode(delete_file=delete)
        except SessionError as exc:
            dialogs.showerror("Problem", str(exc))
        else:
            if removed:
                dialogs.showinfo("Deleted", "Deleted:\n" + "\n".join(removed) + "\n\n" + DELETE_NOTE)
        self.refresh_storage_status()

    # ---- AI provider --------------------------------------------------------------
    def _build_ai(self) -> None:
        box = theme.frame(self)
        box.grid(row=2, column=0, sticky="ew", padx=20, pady=(4, 8))
        help_ui.heading(box, "AI provider (used to judge which emails are real sign-ups)", "ai").grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        self.mode_var = tk.StringVar(value=MODE_CLOUD)
        modes = theme.frame(box)
        modes.grid(row=1, column=0, sticky="w")
        theme.radio(modes, "Cloud AI (API key)", self.mode_var, MODE_CLOUD, self._on_mode_change).pack(
            side="left", padx=(0, 22))
        theme.radio(modes, "Local AI (runs on this computer)", self.mode_var, MODE_LOCAL,
                    self._on_mode_change).pack(side="left")

        form = theme.frame(box)
        form.grid(row=2, column=0, sticky="w", padx=(120, 0), pady=(8, 0))

        def label(r: int, text: str):
            lbl = theme.label(form, text, anchor="e")
            lbl.grid(row=r, column=0, sticky="e", padx=(0, 10), pady=4)
            return lbl

        label(0, "Provider:")
        self.preset_var = tk.StringVar(value=DEFAULT_CLOUD_PRESET)
        self.preset_menu = theme.option_menu(form, list(CLOUD_PRESETS), self.preset_var,
                                             lambda _v: self._apply_preset(), width=230)
        self.preset_menu.grid(row=0, column=1, sticky="w")

        label(1, "Server Address:")
        self.base_url_var = tk.StringVar()
        self.base_url_entry = theme.entry(form, 330, self.base_url_var)
        self.base_url_entry.grid(row=1, column=1, columnspan=2, sticky="w")

        label(2, "Model:")
        self.model_var = tk.StringVar(value=DEFAULT_ANTHROPIC_MODEL)
        self.model_box = theme.combo(form, [], self.model_var, width=250)
        self.model_box.grid(row=2, column=1, sticky="w")
        theme.button(form, "Find models", self._find_models, width=120).grid(row=2, column=2, sticky="w", padx=(10, 0))
        help_ui.button(form, "ai").grid(row=2, column=3, sticky="w", padx=(10, 0))

        self.key_label = label(3, "API Key:")
        self.key_var = tk.StringVar()
        self.key_entry = theme.entry(form, 330, self.key_var, show="*")
        self.key_entry.grid(row=3, column=1, sticky="w")
        self.show_key_var = tk.BooleanVar(value=False)
        self.show_key_check = theme.checkbox(
            form, "show", self.show_key_var,
            lambda: self.key_entry.configure(show="" if self.show_key_var.get() else "*"), width=60)
        self.show_key_check.grid(row=3, column=2, sticky="w", padx=(10, 0))
        self.key_help = help_ui.button(form, "ai")                 # hidden with the key box when Local AI is chosen
        self.key_help.grid(row=3, column=3, sticky="w", padx=(10, 0))

        theme.button(form, "Test connection", self._test_connection, width=130).grid(
            row=4, column=1, sticky="w", pady=(6, 0))
        self.test_status_var = tk.StringVar()
        status = theme.paragraph(form, textvariable=self.test_status_var, small=True, wraplength=420)
        status.grid(row=4, column=2, sticky="w", padx=(10, 0), pady=(6, 0))

        self.privacy_label = theme.paragraph(box, "", small=True)
        self.privacy_label.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self._wrapped.append(self.privacy_label)

        for var in (self.base_url_var, self.model_var, self.key_var):
            var.trace_add("write", lambda *_: self._update_privacy())
        self._apply_preset()

    def _presets(self) -> dict:
        return CLOUD_PRESETS if self.mode_var.get() == MODE_CLOUD else LOCAL_PRESETS

    def _on_mode_change(self) -> None:
        cloud = self.mode_var.get() == MODE_CLOUD
        self.preset_menu.configure(values=list(self._presets()))
        self.preset_var.set(DEFAULT_CLOUD_PRESET if cloud else DEFAULT_LOCAL_PRESET)
        self.key_var.set("")                       # a cloud key must never linger when switching to local
        self._apply_preset()
        run_page = getattr(self.app, "run_page", None)
        if run_page is not None:
            run_page.set_default_batch(local=not cloud)

    def _apply_preset(self) -> None:
        cloud = self.mode_var.get() == MODE_CLOUD
        preset = self._presets().get(self.preset_var.get(), {})
        anthropic = preset.get("kind") == KIND_ANTHROPIC
        self.base_url_var.set(preset.get("base_url", ""))
        self.model_var.set(preset.get("model", ""))
        self.model_box.configure(values=[])
        self.base_url_entry.configure(state="disabled" if anthropic else "normal")
        for widget in (self.key_label, self.key_entry, self.show_key_check, self.key_help):
            (widget.grid if cloud else widget.grid_remove)()
        self.test_status_var.set("")
        self._update_privacy()

    def get_provider_settings(self) -> ProviderSettings:
        preset = self._presets().get(self.preset_var.get(), {})
        cloud = self.mode_var.get() == MODE_CLOUD
        return ProviderSettings(
            mode=self.mode_var.get(), kind=preset.get("kind", KIND_OPENAI),
            base_url=self.base_url_var.get().strip(),
            api_key=self.key_var.get().strip() if cloud else "",
            model=self.model_var.get().strip())

    def _update_privacy(self) -> None:
        level, text = ai.privacy_summary(self.get_provider_settings())
        self.privacy_label.configure(text=text, fg=theme.PRIVACY_COLORS.get(level, theme.TEXT_MUTED))

    def _background(self, work, done) -> None:
        """Run `work()` off the main thread; hand its result to `done(result)` on the main thread."""
        def target():
            try:
                result = work()
            except Exception as exc:
                result = exc
            self.app.gui_queue.put(lambda: done(result))
        threading.Thread(target=target, daemon=True).start()

    def _test_connection(self) -> None:
        try:
            provider = ai.create_provider(self.get_provider_settings())
        except ai.ProviderError as exc:
            self.test_status_var.set(f"\u2717 {exc}")
            return
        self.test_status_var.set("Testing...")
        self._background(provider.test_connection,
                         lambda res: self.test_status_var.set(
                             (f"\u2713 {res[1]}" if res[0] else f"\u2717 {res[1]}")
                             if isinstance(res, tuple) else f"\u2717 {res}"))

    def _find_models(self) -> None:
        settings = self.get_provider_settings()
        if settings.kind == KIND_ANTHROPIC:
            self.test_status_var.set("For Anthropic just keep the default model name (or type another).")
            return
        try:
            provider = ai.create_provider(settings, require_model=False)
        except ai.ProviderError as exc:
            self.test_status_var.set(f"\u2717 {exc}")
            return
        self.test_status_var.set("Looking for models...")

        def done(res):
            if isinstance(res, Exception):
                self.test_status_var.set(f"\u2717 {res}")
            elif not res:
                self.test_status_var.set("The server didn't list any models.")
            else:
                self.model_box.configure(values=res)
                self.test_status_var.set(f"Found {len(res)} model(s). Pick one from the Model drop-down.")

        self._background(provider.list_models, done)

    # ---- accounts -----------------------------------------------------------------------
    def _build_accounts(self) -> None:
        box = theme.frame(self)
        box.grid(row=3, column=0, sticky="nsew", padx=20, pady=(4, 14))
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(1, weight=1)
        help_ui.heading(box, "Email Accounts (as many as you like)", "start").grid(row=0, column=0, columnspan=2,
                                                                                 sticky="w", pady=(0, 6))
        columns = ("name", "provider", "email", "access")
        container, self.tree = theme.table(
            box, columns, {"name": "Nickname", "provider": "Provider", "email": "Emails", "access": "Can fetch via"},
            {"name": 140, "provider": 90, "email": 260, "access": 170}, height=3)
        container.grid(row=1, column=0, sticky="nsew")
        side = theme.frame(box)
        side.grid(row=1, column=1, sticky="n", padx=(12, 0))
        theme.button(side, "Add account", self._add_account, width=140).pack(pady=(0, 8))
        theme.button(side, "Edit selected", self._edit_account, width=140).pack(pady=(0, 8))
        theme.button(side, "Remove selected", self._remove_account, width=140).pack(pady=(0, 14))
        theme.paragraph(side, "No Password?\nYou can still import a\n.pst, .mbox or .eml\nfile. (Outlook and\nHotmail need this.)",
                        small=True, wraplength=150).pack(anchor="w")
        self.refresh_accounts()

    def refresh_accounts(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for a in self.app.session.accounts:
            self.tree.insert("", "end", iid=a.name, values=(
                a.name, a.provider, a.email, "IMAP or file import" if a.has_password else "File import only"))

    def _after_account_change(self) -> None:
        self.refresh_accounts()
        self.app.run_page.refresh_accounts()

    def _add_account(self) -> None:
        dialog = AccountDialog(self)
        if dialog.result:
            self.app.session.upsert_account(dialog.result)
            self._after_account_change()

    def _edit_account(self) -> None:
        sel = self.tree.selection()
        if not sel:
            dialogs.showinfo("Nothing selected", "Select an account to edit first.")
            return
        existing = self.app.session.get_account(sel[0])
        dialog = AccountDialog(self, existing=existing)
        if dialog.result:
            self.app.session.upsert_account(dialog.result, replacing=sel[0])
            self._after_account_change()

    def _remove_account(self) -> None:
        sel = self.tree.selection()
        if sel and dialogs.askyesno("Remove account", f"Remove '{sel[0]}' from this session?\n\n"
                                    f"Nothing is deleted from your mailbox."):
            self.app.session.remove_account(sel[0])
            self._after_account_change()
