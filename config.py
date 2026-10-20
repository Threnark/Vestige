"""
config.py
---------
COMMAND-LINE ONLY. The desktop app never imports this file: it keeps accounts
and keys in memory and never reads or writes config files.

For the CLI, accounts are listed in accounts.yaml (no secrets) and each account
points at an environment variable that holds its app password; the variables
come from your shell or from a .env file YOU created and manage. This tool
never writes either file. (That is still "credentials stored on disk" - by you,
deliberately. If that isn't acceptable, use the desktop app, which keeps
everything in memory.)

`.env` is only read when load_accounts() is called, never merely by importing
this module.
"""

from __future__ import annotations

import os
import sys

from models import DEFAULT_FOLDERS, KNOWN_IMAP_HOSTS, Account, is_microsoft_mail


def load_accounts(config_path: str = "accounts.yaml") -> list:
    import yaml                              # imported here: only the CLI needs these two
    from dotenv import load_dotenv

    load_dotenv()
    if not os.path.exists(config_path):
        sys.exit(f"Config file not found: {config_path}\n"
                 f"Copy accounts.example.yaml to {config_path} and fill it in.")
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    accounts = []
    for entry in raw.get("accounts", []):
        name = entry["name"]
        provider = entry.get("provider", "other")

        # password_env is optional: an account used only for file import (.pst, .mbox,
        # .eml) needs none. Outlook/Hotmail/Live accounts can ONLY be used that way:
        # Microsoft rejects every password-based IMAP login, so any password is ignored.
        password = ""
        password_env = entry.get("password_env")
        if password_env and (provider == "outlook" or is_microsoft_mail(entry.get("email", ""),
                                                                         entry.get("imap_server", ""))):
            print(f"Account '{name}': Microsoft doesn't allow password sign-in for Outlook/Hotmail IMAP, "
                  f"so password_env is ignored. Use `import-mail --account {name} --file your.pst`.",
                  file=sys.stderr)
        elif password_env:
            password = os.environ.get(password_env, "")
            if not password:
                sys.exit(f"Account '{name}' expects the environment variable {password_env}, "
                         f"but it isn't set.")

        imap_server = entry.get("imap_server") or KNOWN_IMAP_HOSTS.get(provider, "")
        if password and not imap_server:
            sys.exit(f"Account '{name}': set imap_server (provider '{provider}' has no default).")

        accounts.append(Account(
            name=name, provider=provider, email=entry["email"], imap_server=imap_server,
            imap_port=int(entry.get("imap_port", 993)), password=password,
            folders=entry.get("folders") or DEFAULT_FOLDERS.get(provider, ["INBOX"]),
        ))
    if not accounts:
        sys.exit(f"No accounts defined in {config_path}")
    return accounts
