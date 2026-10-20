"""
models.py
---------
Plain data types and constants shared by every part of the app.

This file deliberately has NO side effects on import: it never reads files,
never touches the environment, never opens a network connection. That makes it
safe for the GUI to import without accidentally loading a stray `.env` file
(the command-line `config.py` does that, on purpose, only when asked).

Nothing defined here is ever written to disk by the app. An `Account` that
holds an app password, or a `ProviderSettings` that holds an API key, lives in
memory only (see session.py) and is forgotten when the program closes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Email accounts
# ---------------------------------------------------------------------------

# IMAP hosts for providers that still support app-password IMAP access.
# Personal Outlook / Hotmail / Live / MSN accounts are NOT here on purpose: Microsoft
# switched off password (and app-password) sign-in for them on 16 September 2024, so a
# password can never work. Those accounts are used through FILE IMPORT instead (an
# exported .pst file, see module_a_pst.py); the explanation shown to the user is
# MICROSOFT_PASSWORD_NOTE below.
KNOWN_IMAP_HOSTS = {
    "gmail": "imap.gmail.com",
    "yahoo": "imap.mail.yahoo.com",
    "icloud": "imap.mail.me.com",
}

# Which mailbox folders to scan per provider. Gmail's "All Mail" catches
# archived/labelled mail that never appears in INBOX.
DEFAULT_FOLDERS = {
    "gmail": ["INBOX", '"[Gmail]/All Mail"'],
    "yahoo": ["INBOX"],
    "icloud": ["INBOX"],
}

PROVIDER_CHOICES = ["gmail", "yahoo", "icloud", "outlook", "other"]

# ---------------------------------------------------------------------------
# Microsoft mail (Outlook.com, Hotmail, Live, MSN)
# ---------------------------------------------------------------------------
# Address domains used by Microsoft's consumer mail, in every country variant
# (outlook.com, hotmail.co.uk, live.com.au, msn.com, ...).
_MICROSOFT_DOMAIN = re.compile(r"^(?:outlook|hotmail|live|msn|passport|windowslive)\.[a-z]{2,3}(?:\.[a-z]{2})?$")
# Microsoft's own IMAP servers (personal and Microsoft 365).
MICROSOFT_IMAP_HOSTS = {"outlook.office365.com", "imap-mail.outlook.com", "imap.outlook.com", "outlook.office.com"}

MICROSOFT_PASSWORD_NOTE = (
    "Microsoft no longer lets apps sign in to Outlook.com, Hotmail, Live or MSN mail over IMAP with a "
    "password or an app password (switched off on 16 September 2024). Only OAuth sign-in is accepted, "
    "and Vestige keeps nothing on disk, so it cannot do that sign-in for you.\n\n"
    "Use an exported mail file instead. Export your mailbox to a .pst file with Outlook "
    "(File > Open & Export > Import/Export > Export to a file > Outlook Data File), then use "
    "Run Pipeline > 1. Import mail file. No password is needed.")


def is_microsoft_mail(email: str = "", server: str = "") -> bool:
    """True for an Outlook/Hotmail/Live/MSN address, or one of Microsoft's IMAP servers.
    Used only to explain WHY a password can't work and to point at file import."""
    domain = (email or "").strip().lower().rpartition("@")[2]
    host = (server or "").strip().lower()
    return bool(_MICROSOFT_DOMAIN.match(domain)) or host in MICROSOFT_IMAP_HOSTS


class ImportProblem(Exception):
    """A mail file couldn't be imported. The message is written for the user and is
    shown as-is (no traceback), e.g. "Reading .pst files needs ..."."""


@dataclass
class Account:
    """One email mailbox the user wants scanned.

    `password` may be empty. An account without a password simply can't be
    fetched over IMAP, but it can still be used for importing mail files (.pst,
    .mbox, .eml); the address is only used to work out which of the user's
    addresses each email was sent to. That makes a credential-free workflow
    possible, and it is the only way to use Outlook/Hotmail (see above).
    """

    name: str                      # nickname shown in the UI, e.g. "gmail_personal"
    provider: str                  # "gmail" | "yahoo" | "icloud" | "outlook" | "other"
    email: str                     # the mailbox's own address
    imap_server: str = ""
    imap_port: int = 993
    password: str = field(default="", repr=False)   # an APP password (hidden from repr)
    folders: list = field(default_factory=lambda: ["INBOX"])

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    @property
    def is_microsoft(self) -> bool:
        """An Outlook/Hotmail/Live/MSN account (or one pointed at a Microsoft IMAP server)."""
        return self.provider == "outlook" or is_microsoft_mail(self.email, self.imap_server)


# ---------------------------------------------------------------------------
# AI provider settings
# ---------------------------------------------------------------------------

# "cloud": the text of candidate emails is sent over the internet to whichever
#          company the user chose.
# "local": the AI runs on the user's own computer (Ollama, LM Studio, ...).
MODE_CLOUD = "cloud"
MODE_LOCAL = "local"

# "anthropic":         Anthropic's own API (forced tool use for guaranteed JSON).
# "openai_compatible": the de-facto standard chat-completions API that most
#                      other providers, and every local server, also speak.
KIND_ANTHROPIC = "anthropic"
KIND_OPENAI = "openai_compatible"

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class ProviderSettings:
    """Everything needed to talk to one AI service. Held in memory only."""

    mode: str = MODE_CLOUD
    kind: str = KIND_ANTHROPIC
    base_url: str = ""
    api_key: str = field(default="", repr=False)    # NEVER saved anywhere; hidden from repr
    model: str = DEFAULT_ANTHROPIC_MODEL
    timeout_seconds: int = 120

    @property
    def is_local(self) -> bool:
        return self.mode == MODE_LOCAL


# Presets just pre-fill the form. The user can edit every field, and "Other"
# lets them type any OpenAI-compatible base URL. Model names are left blank on
# purpose (except Anthropic): model names change often, and a stale default
# would be worse than asking the user to copy the current one from their
# provider's documentation (or to use the "Find models" button).
CLOUD_PRESETS = {
    "Anthropic (Claude)": {"kind": KIND_ANTHROPIC, "base_url": "", "model": DEFAULT_ANTHROPIC_MODEL},
    "OpenAI": {"kind": KIND_OPENAI, "base_url": "https://api.openai.com/v1", "model": ""},
    "Google Gemini": {"kind": KIND_OPENAI,
                      "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": ""},
    "Groq": {"kind": KIND_OPENAI, "base_url": "https://api.groq.com/openai/v1", "model": ""},
    "Open Router": {"kind": KIND_OPENAI, "base_url": "https://openrouter.ai/api/v1", "model": ""},
    "TogetherAI": {"kind": KIND_OPENAI, "base_url": "https://api.together.xyz/v1", "model": ""},
    "Mistral": {"kind": KIND_OPENAI, "base_url": "https://api.mistral.ai/v1", "model": ""},
    "DeepSeek": {"kind": KIND_OPENAI, "base_url": "https://api.deepseek.com", "model": ""},
    "xAI (Grok)": {"kind": KIND_OPENAI, "base_url": "https://api.x.ai/v1", "model": ""},
    "Other (OpenAI-compatible)": {"kind": KIND_OPENAI, "base_url": "", "model": ""},
}

LOCAL_PRESETS = {
    "Ollama": {"kind": KIND_OPENAI, "base_url": "http://localhost:11434/v1", "model": ""},
    "LM Studio": {"kind": KIND_OPENAI, "base_url": "http://localhost:1234/v1", "model": ""},
    "llama.cpp server": {"kind": KIND_OPENAI, "base_url": "http://localhost:8080/v1", "model": ""},
    "Other local server": {"kind": KIND_OPENAI, "base_url": "", "model": ""},
}

# How many sender-domain groups go into one AI request. Small local models
# answer more reliably with short prompts, so they get a smaller default.
DEFAULT_BATCH_CLOUD = 6
DEFAULT_BATCH_LOCAL = 2
