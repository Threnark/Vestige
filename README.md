# Vestige

Vestige finds the online accounts hiding in your email. It reads the mail you give it, works out
which messages are evidence of a **real account sign-up** (as opposed to newsletters,
receipts and noise), and produces a table of **Service → email address you used → date →
category**. It can also spot **possible email changes**: notices that the address on an
account was changed, so you know where an old account may live now.

It runs on your own computer. Use a **local AI** and nothing at all leaves it.

## Token Usage & Efficiency

> **Designed for Minimal API Cost**  
> Vestige uses local pre-filtering and character limits to keep processing efficient. The AI never reads your entire inbox: only short excerpts of filtered candidate emails are sent to the model.

* **How it works:** Your mail is scanned and filtered locally on your computer, for free. The AI is only called for likely account sign-ups and email-change notices. For sign-up classification it receives up to three sample emails per sender, each limited to the first 400 characters of the body; for email-change notices it receives the first 800 characters. Several senders are judged per request (6 by default for a cloud AI, 2 for a local AI).
* **Estimated usage** (sign-up classification):
  * **~100 candidate groups** (usually one per sender domain): roughly 75,000–110,000 total tokens.
  * **~500 candidate groups:** roughly 380,000–550,000 total tokens.
* **Cost note:** These figures are estimates. Actual token counts vary with email length and the AI model you select, and email-change notices are processed in a separate step that adds to the total. See [Choosing an AI](#choosing-an-ai-cloud-or-local).

---

## Contents
1. [Privacy and data handling](#privacy-and-data-handling) - read this first
2. [Installation](#installation)
3. [Quick start](#quick-start)
4. [Getting your email in](#getting-your-email-in)
5. [Choosing an AI](#choosing-an-ai-cloud-or-local)
6. [Possible email changes](#possible-email-changes)
7. [Exporting](#exporting)
8. [How it works](#how-it-works)
9. [Command line](#command-line-advanced)
10. [Tests](#tests)
11. [Limitations](#limitations)
12. [Project layout](#project-layout)

---

## Privacy and data handling

This tool handles an unusually sensitive thing: a list of every service you have an account
with, and the addresses you used for them. Here is exactly what it does with it.

### What is saved to your computer
**Desktop app, default: nothing.** Accounts, app passwords, your AI API key, local-AI
addresses and all scan results are held **in memory only**. Closing the app discards them.
There is deliberately **no autofill and no "remember me"**; even an encrypted copy of a
credential would still be a sensitive file on your drive, so none is kept.

Files are written only if **you** choose:

| You choose | What is written | Where |
|---|---|---|
| **Export** | CSV / Markdown of the results | The folder you pick. There is no default location. Existing files are never overwritten. |
| **Write mode** (opt-in checkbox) | The results database, updated as the scan runs, so a crash or accidental close doesn't waste scanning time and AI cost | `Vestige_DATA/session.db`, in the same folder as the program, where it is easy to see, move and delete |

Write mode saves **results only**. It never saves passwords, API keys or AI server addresses.
It does save sensitive information: sender names and addresses, email subject lines, short
excerpts (up to 800 characters) of the text of emails that look like sign-ups, which of your
addresses each email was sent to, and the AI's answers. The app warns you before turning it
on and offers to delete the file when you close the app. If that worries you, leave write mode
off, or run the program from a drive you trust and delete the folder afterwards.

**The command-line tool is different:** every command is a separate process, so it keeps its
database in a **file** (`accounts.db` by default) and reads credentials from environment
variables / a `.env` file that *you* create. Use the desktop app if you want the
memory-only behaviour.

### What leaves your computer
* **Your mail provider**, when you fetch over IMAP (an encrypted connection to the server you
  configured). Importing a mail file (`.pst`, `.mbox`, `.eml`) needs no network at all.
* **The AI you chose**, and only that:
  * *Cloud AI:* the text below is sent over the internet to that provider, along with your API
    key. You decide whether you trust them; check their data policy. Use *Local AI* to avoid it.
  * *Local AI:* sent to the server on your own computer (or your own network). Nothing leaves.

  For sign-up classification, per candidate sender: the sender's domain and, for up to three
  sample emails, the date, the subject and the first 400 characters of the body. For email-change
  notices: the sender, date, subject and the first 800 characters of the body. **Not sent:** your
  email addresses, your list of accounts, the full emails, or the rest of your mail. Be aware
  that an excerpt of an email's text can itself contain personal details (a greeting with your
  name, an address printed in the message).
* **Nothing else.** No telemetry, analytics, crash reporting or update checks. The app's only
  network connections are the two above. The Setup tab states, in plain words, where your data
  will go for the AI settings currently selected.

### Memory (RAM)
Anything held in memory stays in RAM until it is overwritten. Closing the app releases that
memory but does **not** guarantee it is erased; the data can remain until something else
overwrites it. To be sure RAM is cleared, **restart or fully shut down your computer (do not
hibernate)**. Hibernation writes memory to a file on the drive, and Windows "Fast Startup" does a
partial version of the same when you choose *Shut down*, while *Restart* does a full clear.
The operating system may also copy parts of memory to a swap/page file while the app runs. Full-disk
encryption (BitLocker, FileVault, LUKS) is the real protection against that.

### Deleting files does not erase them
Deleting a file removes its entry but does **not** overwrite its contents; the bytes can remain on
the drive until they are overwritten, and on SSDs they can survive longer still. The app's
**Delete** buttons are ordinary deletes and say so. For real protection, keep sensitive data on an
**encrypted** drive or folder. This applies to your exported files too: they are plain text.

### Other things worth knowing
* **Cloud-synced folders.** If the program's folder or your export folder is inside OneDrive,
  Dropbox, iCloud Drive, Google Drive, etc., saved files may be uploaded automatically. The app
  warns when a folder looks synced (a heuristic, not a guarantee).
* **Exports are a map of your online life** in plain text. Store them somewhere you trust.
* **Spreadsheet safety.** Service names come from email content, which a sender controls. In CSV
  files, any cell starting with `=`, `+`, `-` or `@` is prefixed with `'` so Excel can't run it
  as a formula.
* **Reading only.** Mail is only ever read. IMAP mailboxes are opened read-only with
  `BODY.PEEK`, so nothing is marked as read, moved or deleted; MBOX files are read, never modified.
* **Use it on your own accounts.** It needs a mailbox's credentials or export file, so it only
  ever sees what someone with that access could already see; it is meant for auditing your own mail.

---

## Installation

There are two ways to get Vestige. Most people should use the first.

### Option 1: Download a release (no Python needed)

Go to the [Releases page](https://github.com/Threnark/Vestige/releases) and download the file for your system from the latest release:

| System | File |
|---|---|
| Windows 10 or 11 (64-bit) | `Vestige-Windows.zip` |
| macOS on Apple Silicon (M1 or newer) | `Vestige-macOS.tar.gz` |
| Linux (64-bit x86) | `Vestige-Linux.tar.gz` |

These files are built automatically by GitHub Actions from the source code in this repository (see
`.github/workflows/build-all.yml`). The programs are not code-signed, so Windows and macOS warn you the
first time you open them; the steps below show how to continue. Intel Macs have no release download: use
[Option 2](#option-2-run-from-source).

#### Windows

1. Download `Vestige-Windows.zip`.
2. Right-click the zip and choose **Extract All...**. Don't run the program from inside the zip.
3. Open the extracted `Vestige` folder and double-click `Vestige.exe`. Keep the folder's contents together:
   `Vestige.exe` needs the files beside it.
4. If Windows shows **Windows protected your PC**, click **More info**, then **Run anyway**.

If you use write mode, the `Vestige_DATA` folder is created next to `Vestige.exe`, so extract the folder
somewhere you can write to (for example inside Documents) rather than `C:\Program Files`.

#### macOS

Requires an Apple Silicon Mac (M1 or newer).

1. Download `Vestige-macOS.tar.gz` and double-click it to extract `Vestige.app` (or run
   `tar -xzf Vestige-macOS.tar.gz` in Terminal).
2. Move `Vestige.app` somewhere you are happy to keep it, such as a folder in Documents.
3. Double-click `Vestige.app`. If macOS refuses to open it because it can't check the app for malicious software
   (the app is not signed or notarized by Apple), allow it once:
   * Open **System Settings -> Privacy & Security**, scroll down to the *Security* section, click
     **Open Anyway** next to the message about Vestige, and confirm. (On macOS 14 and earlier you can instead
     Control-click `Vestige.app`, choose **Open**, then **Open** again.)
   * Or, in Terminal: `xattr -dr com.apple.quarantine /path/to/Vestige.app`, then open the app normally.

If you use write mode, the `Vestige_DATA` folder is created in the folder that contains `Vestige.app`.

#### Linux

1. Download `Vestige-Linux.tar.gz`.
2. Extract it and start the program from a terminal:

   ```bash
   tar -xzf Vestige-Linux.tar.gz
   cd Vestige
   ./Vestige
   ```

   You can also double-click the `Vestige` file in the extracted folder if your file manager runs executables.

Notes:
* Python and the Tk interface library are included; you don't need to install them. You do need a graphical
  desktop session.
* The download is built on a recent Ubuntu release, so it needs a reasonably recent distribution. On an older
  one you may see an error about a missing `GLIBC` version; run from source instead.
* If you use write mode, the `Vestige_DATA` folder is created inside the extracted `Vestige` folder.

### Option 2: Run from source

Requires **Python 3.10 or newer** (the release builds use Python 3.11).

```bash
git clone https://github.com/Threnark/Vestige.git
cd Vestige
python -m venv .venv
# Activate the virtual environment:
#   Windows (Command Prompt):  .venv\Scripts\activate
#   Windows (PowerShell):      .venv\Scripts\Activate.ps1
#   macOS / Linux:             source .venv/bin/activate
pip install -r requirements.txt
python gui_app.py
```

On macOS and Linux, use `python3` in place of `python` if `python` isn't found. If you don't use git, choose
**Code -> Download ZIP** on the repository page, unzip it, and continue from `python -m venv .venv`.

* `requirements.txt` includes CustomTkinter, which the interface needs. PyYAML and python-dotenv are only for the
  command-line tool, and PyInstaller is only needed to build a program yourself.
* On Linux you may need Tkinter first: `sudo apt install python3-tk` (Debian/Ubuntu) or
  `sudo dnf install python3-tkinter` (Fedora).
* `libpff-python` (for `.pst` files) is the only optional package. If `pip` can't install it for your Python
  version or platform, delete its line from `requirements.txt` and run `pip install -r requirements.txt` again.
  Everything except `.pst` import still works.

To make your own double-click program from the source, see [`README_GUI.md`](README_GUI.md#getting-a-double-click-version).

---

## Quick start

Open Vestige (the program from a release download, or `python gui_app.py` if you run from source). Then in the app:
1. **Setup** - add your email account(s), pick an AI (cloud with an API key, or local), optionally
   turn on write mode.
2. **Run Pipeline** - import a mail file (`.pst`, `.mbox`, `.eml`) or fetch over IMAP, then **Run full pipeline**
   (try *Limit AI to N groups = 10* first to see real results cheaply). A **progress bar** shows the
   current step and an overall percentage, with a moving "working..." marker while a total isn't known yet
   (for example while a large mail file is being indexed).
3. **Results** - review the tables, then **Export** into a folder you choose.

Click **? Help** (or press F1) in the app for help on the current page, including how to get your mail out of
Gmail, Yahoo, iCloud and Outlook. See [`README_GUI.md`](README_GUI.md) for a walk-through of each page.

---

## Getting your email in

**Easiest and most private: an exported mail file.** No password, no internet connection, and the file
is only ever read. Vestige reads three kinds directly:

| You have | File | Notes |
|---|---|---|
| Outlook / Hotmail / Live / MSN | `.pst` | Needs the optional `libpff-python` package (see below) |
| Gmail Takeout, Thunderbird | `.mbox` | Nothing extra needed |
| Individual emails | `.eml` files, or a **folder** of them | Folders are searched recursively |

In the app: add an account with the right email address (no password), then
*Run Pipeline -> 1. Import mail file...*, pick the account, and choose the file(s) or folder. You can pick
several files at once.

* **Gmail:** [Google Takeout](https://takeout.google.com) -> select only *Mail* -> download the `.mbox`
  (it arrives inside a zip; unzip it first).
* **Outlook / Hotmail / Live / MSN (personal):** see the next section.

### Outlook, Hotmail, Live and MSN

**Why you can't just type a password.** Microsoft switched off password sign-in (including app
passwords) for IMAP/POP/SMTP on personal Outlook.com, Hotmail, Live and MSN accounts on 16 September
2024. Only OAuth sign-in is accepted now, so any password gets a "LOGIN failed" however correct it is.
Vestige keeps nothing on disk and has no Microsoft app registration of its own, so it doesn't attempt
that sign-in. (Microsoft 365 work and school mailboxes have moved to OAuth as well.) When you pick the
*outlook* provider, or type an Outlook/Hotmail/Live/MSN address, the Add account window says this and
doesn't ask for a password.

**What works instead: export your mailbox to a `.pst` file and import that.** This is the export Microsoft
documents for Outlook.com mailboxes (support.microsoft.com -> "Export your Outlook.com mailbox"):

1. Install or open **Outlook for Windows** (a free 30-day Microsoft 365 trial is enough) and add your
   Outlook/Hotmail account to it.
2. **Classic Outlook:** *File -> Open & Export -> Import/Export -> Export to a file -> Outlook Data File
   (.pst)*, choose the account's top folder and tick *Include subfolders*. **New Outlook:** Microsoft's
   article says it can export a `.pst` too (third-party guides put it under *Settings -> Files -> Export*;
   menus change, so follow Microsoft's "Export your Outlook.com mailbox" page if yours differs).
3. In Vestige: *Run Pipeline -> 1. Import mail file...* and choose the `.pst`.

Notes:
* **Which folders are read:** everything except Sent Items, Drafts, Outbox, Calendar, Contacts, Tasks,
  Notes and similar. *Deleted Items* and *Junk* **are** read, because people often delete the welcome
  email from a service they signed up to. The log lists every folder read and skipped.
* Mail you sent from your own address is ignored.
* Outlook for **Mac** exports `.olm`, which Vestige can't read: add the account to Outlook for Windows
  and export a `.pst` there, or use the Thunderbird route below.
* **No Outlook available? Thunderbird route:** add the account to Thunderbird (it supports Microsoft's
  OAuth sign-in), let it download the mail, and import its local mail files: Thunderbird stores each
  folder as an MBOX file with no extension (*Help -> Troubleshooting Information -> Profile Folder ->
  Open Folder*, then `ImapMail/...`) by default. Make sure the account is set to keep its messages on
  this computer, otherwise Thunderbird only stores headers. Vestige recognises these files by their
  contents, so the missing extension doesn't matter.
* **PST support.** `.pst` reading uses libpff through the `libpff-python` package, the only
  optional dependency. `pip install -r requirements.txt` installs it, and the release builds and the build
  scripts include it when it can be installed at build time. If it isn't available in your copy of Vestige,
  a `.pst` import stops with a message that says so and everything else keeps working: use an `.mbox` or
  `.eml` export instead, or run from source with the package installed.
* **Testing the PST import.** The automated tests check Vestige's own logic against a stand-in for the
  library; they can't prove how libpff behaves on your particular export. After your first import, check
  the log line `N messages read, M new` against what you expect, and please
  [open an issue](https://github.com/Threnark/Vestige/issues) about any folder or message type that comes
  through empty.

**IMAP with an app password** (Gmail, Yahoo, iCloud, and others). Use an **app password**, never your
normal password:
* **Gmail:** turn on 2-Step Verification, then create one at `myaccount.google.com/apppasswords`. Google
  Workspace (company) accounts generally can't - use Takeout.
* **Yahoo:** Account security -> *Generate app password*.
* **iCloud:** `appleid.apple.com` -> Sign-In and Security -> *App-Specific Passwords*.

**Forwarded and old addresses.** If an old address forwards into a mailbox you scan, the app records
the address each email was actually sent to (including `you+tag@gmail.com` aliases). That is what makes
the table show *which address you used* and what lets email-change detection see old addresses.

---

## Choosing an AI: cloud or local

The AI judges which senders are genuine sign-ups. Pick one on the **Setup** tab; the privacy line under
it always says where your data will go.

**Cloud AI (an API key).** Anthropic (Claude) is built in, using forced tool-use so the reply is
guaranteed-structured JSON. Everything else goes through one generic **OpenAI-compatible** adapter
(presets provided for OpenAI, Google Gemini, Groq, OpenRouter, Together, Mistral, DeepSeek, xAI, or
"Other" with any base URL). You supply the key and the model name; **Find models** lists what your provider
offers. Cloud providers must use `https://`.

**Local AI (nothing leaves your computer).** Run your own model server and point the app at it:
* **Ollama** (`http://localhost:11434/v1`) - install Ollama, then e.g. `ollama pull llama3.1:8b`.
* **LM Studio** (`http://localhost:1234/v1`) - start its local server.
* **llama.cpp server** (`http://localhost:8080/v1`), or any other OpenAI-compatible server.

You install and host the model yourself; the app can't bundle one. Expect: roughly 8-16 GB of RAM/VRAM for a
7-14B model, slower runs than the cloud, and more mistakes on borderline cases (identity-provider notices
such as "you signed in to Notion with Google" are the hardest). Local mode uses smaller batches by default
(2 sign-up groups per request). The app warns if the "local" address isn't actually on this computer.

How it copes with different models: it asks for the strictest structured format the server supports and
steps down if it's rejected, extracts JSON even from chatty or "thinking" replies, validates against the
schema, and automatically re-asks (up to twice) when the answer is unusable. A batch that still fails is
skipped, never fatal, and retried on the next run. Use *Test connection* and a small
*Limit AI to N groups* run before a full scan, and always eyeball the confidence scores: a weaker model is
less reliable, not equally reliable.

Cost (cloud): classification is per sender domain, not per email, with several domains per request, and
already-judged senders are skipped on re-runs. See [Token Usage & Efficiency](#token-usage--efficiency)
for estimates, and check your provider's current pricing.

---

## Possible email changes

If you're hunting for an account you've forgotten, finding "Service X exists" is only half the answer: if
you changed the address on it years ago, logging in with the original address won't work. So the app also
looks for **notices that an account's email address changed** ("your email was changed", "confirm your new
email", ...) and reports them in their own table.

* **Trigger:** only email-change notices. Having two accounts on one service never triggers it.
* **The AI extracts; plain code matches.** The AI copies out what the notice literally says: the service,
  whether it went to the old or new address, and any address printed (often masked, like `j***@gmail.com`).
  It is told never to guess or complete an address. Ordinary code then decides which of **your own, already
  seen addresses** could fit the mask. The AI is never asked to invent an address and never sees your list
  of addresses.
* **Match quality:** *Strong* (one known address fits), *Possible* (several fit), *Hint only* (something is
  printed but nothing you scanned fits; you get the printed hint, never an invented address), *No address
  shown*.
* Each row also shows the address(es) you originally signed up with, and a plain-English explanation.

**Limits:** a notice has to exist, still be in the mail you scanned, and use words the pre-filter recognises
(rules are in `module_b_prefilter.py`). Many services send none. If the account moved to an address you
didn't scan, you only get the printed hint. Treat every row as a lead to check, not a fact.

---

## Exporting

Nothing is exported unless you ask, and **Run full pipeline never exports**. On the **Results** tab choose a
folder (for example a drive you trust), tick CSV and/or Markdown, and click **Export now**:
* `signups.csv` - one row per **service and address** (two addresses on Spotify = two rows)
* `signups_possible_email_changes.csv` - the possible email changes (only if any were found)
* `signups.md` - both tables as a readable document

Options: add a date/time to the file names (on by default), and/or put each export in a new dated sub-folder.
Existing files are never overwritten: a name clash becomes `name (2).csv`. If the drive isn't connected or
isn't writable, you get a plain message and nothing is half-written.

---

## How it works

```
  Module A            Module B             Module C / E              Module D
  Retrieval     ->    Pre-filter     ->    AI (cloud or local)  ->   Tables + export
  IMAP headers        free, rule-based     sign-ups (C)              one row per
  or MBOX file        scoring, no AI       email changes (E)         service+address
  (bodies only        (sign-up score and
   for candidates)     email-change score)
        |                                          |
        +--------- one database (memory, or a file in write mode) ---------+
```

* **A** fetches headers only (fast, read-only), then short body snippets only for candidates.
* **B** scores subject lines with editable rules; most mail is discarded here, for free.
* **C** judges candidates per sender domain (identity providers such as `accounts.google.com`, which send
  notices about many different apps, are judged per notice so each resolves to its own service).
* **E** examines email-change notices (see above).
* **D** builds the tables and writes files only when asked.

Every stage is resumable and only does work that hasn't been done: re-running after adding a mailbox only
processes what's new, and (in write mode) a crash costs you nothing already finished.

---

## Command line (advanced)

Same pipeline, one stage at a time. It is not part of the release downloads, so run it from source
(see [Installation](#option-2-run-from-source)). It keeps a database **file** between commands (see
[Privacy](#what-is-saved-to-your-computer)). Copy `accounts.example.yaml` -> `accounts.yaml` and
`.env.example` -> `.env`. API keys are read from environment variables, never from arguments.

```bash
python main.py import-mail --account gmail_personal --file mail.mbox   # or a .pst / .eml / folder of .eml; or:  python main.py fetch
python main.py import-mail --account my_hotmail --file export.pst       # Outlook / Hotmail (import-mbox still works too)
python main.py prefilter
python main.py prefilter-report            # sanity check, no AI used
python main.py fetch-bodies
python main.py classify --ai anthropic --max-domains 10     # a cheap test run first
python main.py find-changes --ai anthropic
python main.py export --out-dir "D:\Safe\Exports" --subfolder

# local AI instead:
python main.py classify --local --model llama3.1:8b
# another cloud provider:
python main.py classify --ai openai-compatible --base-url https://api.groq.com/openai/v1 \
       --model <model> --api-key-env GROQ_API_KEY
```

`python main.py all` runs fetch -> prefilter -> bodies -> classify -> find-changes (it does **not** export);
add `--skip-fetch` to skip the IMAP step, for example after importing a file. `python main.py status` shows
how many rows each stage has produced. Delete `accounts.db` when you're finished with it.

---

## Tests

```bash
python -m unittest -v
```

Standard library only; no network, no API key, no real mail (the tests that build the real interface run only when
CustomTkinter and a display are available, and are skipped otherwise). AI services are replaced by fakes (the
OpenAI-compatible adapter is tested against a small server on localhost), mail by synthetic MBOX/EML files (and, for `.pst`, a stand-in for the libpff library).
They cover the matching of masked addresses, the AI adapters' fallback/retry behaviour, the pipeline end to
end, export rules (no overwriting, spreadsheet safety), write-mode save/resume, and the guarantee that
credentials never reach any file.

---

## Limitations

* **It can only find what your email still remembers.** Deleted welcome emails, services that never emailed
  you, and mail older than your mailbox won't appear.
* **Accuracy varies by AI model.** The pre-filter rules and prompts may need tuning for the model you use, and weaker models make more mistakes, so check the confidence scores. It has been tested with `qwen2.5:14b`, with results that looked good.
* **IMAP has only been tested live on a few providers:** `@gmail.com`, `@proton.me`, and a custom domain hosted by PorkBun with email set up through PurelyMail. Other providers that support IMAP with an app password should work but haven't been tried.
* **Email-change detection** depends on notices existing and being recognised, and can't resolve an address you
  didn't scan.
* Python can't guarantee it wipes secrets from RAM (see [Memory](#memory-ram)).
* **The interface depends on CustomTkinter**, a third-party library that has had few updates in recent years,
  so check that it is still maintained before relying on it long-term. Its look lives in one file
  (`ui_theme.py`), and tables use a restyled standard Tk table because CustomTkinter has none. The operating
  system's own file/folder pickers can't be themed, so they appear in the system style.

---

## Project layout

```
gui_app.py                 desktop app (run this)
  gui_setup.py             Setup page
  gui_run.py               Run Pipeline page + progress
  gui_results.py           Results page + export panel
  ui_theme.py              the dark-purple look (colours, fonts, widget factories) - restyle here
  ui_dialogs.py            themed pop-up dialogs
  ui_help.py               Help window and the [?] buttons
  help_content.py          all Help text, as plain data
  ui_footer.py             the footer line (version, support link)
  ui_common.py  ui_icons.py  ui_metrics.py     small shared helpers (icons, table sizing)
main.py                    command-line tool
pipeline.py                stages shared by the app and the command line
session.py                 in-memory state + write mode
app_info.py                name, version, licence and links (change VERSION here to release)
assets/                    app icon (SVG + generated PNG/ICO/ICNS)
tools/render_icon.py       regenerates the icon files from the SVG
ai_providers.py            Anthropic + OpenAI-compatible adapters, local/cloud privacy summary
module_a_imap.py           Module A: IMAP headers + body snippets
module_a_files.py          Module A: picks the right reader for each imported file
module_a_pst.py            Module A: Outlook .pst import
module_a_mbox.py           Module A: MBOX import (streamed)
module_a_eml.py            Module A: .eml files and folders of them
module_b_prefilter.py      Module B: rule-based scoring, grouping
module_c_classify.py       Module C: sign-up classification
module_e_email_changes.py  Module E: possible email changes
migration_matcher.py       masked-address matching (pure code)
module_d_export.py         Module D: tables + file export
db.py  models.py  paths.py  jobcontrol.py (stop + progress)  config.py (CLI only)
prompts/                   the exact AI prompts and JSON schemas
tests/                     unit tests
requirements.txt           Python packages
accounts.example.yaml  .env.example                  command-line tool settings (copy and fill in)
build_windows.bat  build_mac_linux.sh                package the desktop app with PyInstaller
.github/workflows/build-all.yml                      builds the Windows, macOS and Linux releases
```
