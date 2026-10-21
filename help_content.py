"""
help_content.py
---------------
EVERYTHING the Help window says, as plain data. There is no GUI code in this file on
purpose: ui_help.py turns these blocks into widgets, and tests/test_help.py checks the
content without needing a screen.

HOW THE CONTENT IS BUILT
  A help topic is a title, a one-line summary and a list of BLOCKS. A block is one of
  the small classes below (Section, Para, Steps, Bullets, Note, Link, Settings, and for
  the About page Identity and SupportButtons). The helper functions after the block
  classes (section(), para(), steps(), ...) keep the topic definitions short and readable.

WHERE THE FACTS COME FROM (the rule is: nothing is made up)
  * How Vestige itself behaves is described from the code. Wherever a number or a name
    appears in the text (how many characters are sent to the AI, the default batch
    size, the IMAP server names, the list of AI providers, the categories, the data
    folder name) it is IMPORTED from the module that actually uses it, so the help can
    never drift away from the program. tests/test_help.py also checks the button
    names against the real buttons.
  * How other companies' services work (Google, Yahoo, Apple, Microsoft, the AI
    providers, Ollama) was checked against those companies' own help pages in
    October 2026 (see VERIFIED_ON). Every link below is one of those pages. Their
    websites change, so provider topics end with a short note saying so.
  * Where something could not be confirmed, the text says "usually", "may" or "check
    the provider's help" instead of stating it as fact.

TO CHANGE THE HELP: edit the text below. To add a topic, add a Topic to TOPICS and an
entry in NAV; to point a [?] button at it, use its id (see ui_help.help_button).
"""

from __future__ import annotations

from dataclasses import dataclass

import app_info
import module_a_imap
import module_c_classify
import module_e_email_changes
import paths
from models import (
    CLOUD_PRESETS, DEFAULT_BATCH_CLOUD, DEFAULT_BATCH_LOCAL, KNOWN_IMAP_HOSTS, LOCAL_PRESETS,
    is_microsoft_mail,
)
from prompts.classification_prompt import CLASSIFY_TOOL

VERIFIED_ON = "October 2026"

CHECKED_NOTE = (f"The steps on this page were checked against the provider's own help pages in {VERIFIED_ON}. "
                f"Websites change: if a button or menu has moved, follow the provider's own help link on this page.")


# ---------------------------------------------------------------------------
# Block types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    """A heading inside a page."""
    text: str


@dataclass(frozen=True)
class Para:
    """A paragraph of normal text."""
    text: str


@dataclass(frozen=True)
class Steps:
    """A numbered list: things to do, in order."""
    items: tuple


@dataclass(frozen=True)
class Bullets:
    """A bulleted list."""
    items: tuple


@dataclass(frozen=True)
class Note:
    """A highlighted box. kind: "info" (neutral), "tip" (helpful) or "warning" (be careful)."""
    kind: str
    title: str
    text: str


@dataclass(frozen=True)
class Link:
    """A web link. It opens in the user's normal browser when clicked; the address is shown
    in full under the label so people can see where it goes."""
    label: str
    url: str


@dataclass(frozen=True)
class Settings:
    """A small table of (name, value) pairs, such as what to type into the Add account window."""
    title: str
    rows: tuple


@dataclass(frozen=True)
class Identity:
    """The card at the top of the About page: the program's name and the lines under it
    (version, copyright). Shown next to the program icon."""
    name: str
    lines: tuple


@dataclass(frozen=True)
class SupportButton:
    """One way to support the project: a button with a small picture, and a gentle line of
    explanation. `icon` is the name of a drawn icon (ui_icons.py): "star", "heart" or "coffee"."""
    icon: str
    label: str
    note: str
    url: str


@dataclass(frozen=True)
class SupportButtons:
    """A stack of SupportButton items."""
    items: tuple


@dataclass(frozen=True)
class Topic:
    id: str
    nav: str            # short name in the left-hand list
    title: str          # big heading on the page
    summary: str        # one plain sentence under the heading
    blocks: tuple
    checked: bool = False   # True: ends with CHECKED_NOTE (the page depends on other companies' websites)


def section(text): return Section(text)
def para(text): return Para(text)
def steps(*items): return Steps(tuple(items))
def bullets(*items): return Bullets(tuple(items))
def info(title, text): return Note("info", title, text)
def tip(title, text): return Note("tip", title, text)
def warn(title, text): return Note("warning", title, text)
def link(label, url): return Link(label, url)
def settings(title, *rows): return Settings(title, tuple(rows))
def identity(name, *lines): return Identity(name, tuple(lines))
def support_buttons(*items): return SupportButtons(tuple(items))


# ---------------------------------------------------------------------------
# Links. Each one is a page of the company it is about, checked in VERIFIED_ON.
# ---------------------------------------------------------------------------
URL_GOOGLE_APP_PASSWORDS = "https://myaccount.google.com/apppasswords"
URL_GOOGLE_APP_PASSWORD_HELP = "https://support.google.com/accounts/answer/185833"
URL_GOOGLE_IMAP_HELP = "https://support.google.com/mail/answer/7126229"
URL_GOOGLE_TAKEOUT = "https://takeout.google.com"
URL_YAHOO_SECURITY = "https://login.yahoo.com/account/security"
URL_YAHOO_APP_PASSWORD_HELP = "https://help.yahoo.com/kb/SLN15241.html"
URL_YAHOO_IMAP_HELP = "https://help.yahoo.com/kb/SLN4075.html"
URL_APPLE_ACCOUNT = "https://account.apple.com"
URL_APPLE_APP_PASSWORD_HELP = "https://support.apple.com/102654"
URL_APPLE_IMAP_HELP = "https://support.apple.com/102525"
URL_MS_BASIC_AUTH_NOTICE = "https://support.microsoft.com/en-us/topic/c5d65390-9676-4763-b41f-d7986499a90d"
URL_MS_EXPORT_OUTLOOKCOM = "https://support.microsoft.com/en-US/Outlook/export-your-outlook-com-mailbox"
URL_MS_EXPORT_PST = ("https://support.microsoft.com/en-US/Outlook/"
                     "export-emails-contacts-and-calendar-items-to-outlook-using-a-pst-file")
URL_MS_TRIAL = "https://www.microsoft.com/microsoft-365/try"
URL_ANTHROPIC_KEYS = "https://platform.claude.com/docs/en/get-api-key"
URL_OPENAI_KEYS = "https://platform.openai.com/api-keys"
URL_GEMINI_KEYS = "https://aistudio.google.com/app/apikey"
URL_GROQ_KEYS = "https://console.groq.com/keys"
URL_OPENROUTER_KEYS = "https://openrouter.ai/keys"
URL_MISTRAL_KEYS = "https://console.mistral.ai/api-keys"
URL_DEEPSEEK_KEYS = "https://platform.deepseek.com/api_keys"
URL_OLLAMA_DOWNLOAD = "https://ollama.com/download"
URL_OLLAMA_LIBRARY = "https://ollama.com/library"


# ---------------------------------------------------------------------------
# Facts read from the program itself
# ---------------------------------------------------------------------------

def _find_enum(node, key):
    """Dig the list of allowed values for `key` out of the AI's answer format
    (prompts/classification_prompt.py), so the Results help always matches it."""
    if isinstance(node, dict):
        if isinstance(node.get(key), dict) and "enum" in node[key]:
            return list(node[key]["enum"])
        for value in node.values():
            found = _find_enum(value, key)
            if found:
                return found
    elif isinstance(node, (list, tuple)):
        for value in node:
            found = _find_enum(value, key)
            if found:
                return found
    return []


CATEGORIES = _find_enum(CLASSIFY_TOOL, "category")
SIGNUP_METHODS = _find_enum(CLASSIFY_TOOL, "signup_method")

CLOUD_PROVIDER_NAMES = [name for name in CLOUD_PRESETS if not name.startswith("Other")]
LOCAL_PROVIDER_NAMES = [name for name in LOCAL_PRESETS if not name.startswith("Other")]

BODY_CHARS_KEPT = module_a_imap.BODY_SNIPPET_CHARS                    # kept per candidate email
BODY_KB_DOWNLOADED = module_a_imap.BODY_FETCH_BYTES // 1000           # downloaded per candidate email
SIGNUP_SAMPLES = module_c_classify.MAX_SAMPLES_PER_GROUP              # emails shown to the AI per sender group
SIGNUP_CHARS = module_c_classify.SNIPPET_CHARS_SENT                   # body characters per sample
CHANGE_CHARS = module_e_email_changes.SNIPPET_CHARS_SENT              # body characters per change notice
DATA_FOLDER = f"{paths.DATA_DIR_NAME}/{paths.SESSION_DB_NAME}"        # where Write Mode saves

# The exact names on the Run Pipeline buttons (tests check these against the real buttons).
BTN_FETCH = "1. Fetch headers (IMAP)"
BTN_IMPORT = "1. Import mail file..."
BTN_PREFILTER = "2. Pre-filter (free)"
BTN_BODIES = "3. Fetch email bodies"
BTN_CLASSIFY = "4. AI: classify sign-ups"
BTN_CHANGES = "5. AI: find email changes"
BTN_FULL = "Run full pipeline (1-5)"
BTN_STOP = "Stop"


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

START = Topic(
    id="start", nav="Getting started", title="Getting started",
    summary="Everything you need for your first scan. Setting up takes about five minutes.",
    blocks=(
        para("Vestige looks through the emails in your mailboxes and works out which online services you have "
             "signed up to. For each one it lists the service, the email address you used and the sign-up date, "
             "so you can find accounts you have forgotten about."),
        section("What you need"),
        bullets(
            "At least one email account. Vestige can read it directly (using an app password) or from a mail "
            "file that you export.",
            "An AI to judge which emails are real sign-ups. This is either an API key from an AI company, or an "
            "AI running on your own computer.",
            "Some patience. A large mailbox can take a while to scan.",
        ),
        section("Setting up, step by step"),
        steps(
            "Choose your AI. On the Setup tab, under “AI provider”, pick “Cloud AI (API key)” and paste a key, "
            "or pick “Local AI (runs on this computer)” and point Vestige at a program such as Ollama. The "
            "“AI: cloud or local” help page explains both.",
            "Add your email accounts. Still on the Setup tab, click “Add account”. Enter a nickname and your "
            "email address. Then either enter an app password (so Vestige can read the mailbox), or leave the "
            "password empty and import an exported file later. Each email provider has its own page in the list "
            "on the left.",
            f"Run the pipeline. Go to the “Run Pipeline” tab and click “{BTN_FULL}”. Or run the steps one at a "
            f"time with the buttons described below.",
            f"Wait for it to finish. The progress bar and the log show what is happening. You can click "
            f"“{BTN_STOP}” at any time. Everything that was finished is kept.",
            "Review and export. Open the “Results” tab to look through what was found. Click “Export now” if you "
            "want to save it as a CSV or Markdown file.",
        ),
        tip("Do a small test run first",
            "On the Run Pipeline tab, type a small number such as 5 into “Limit AI to N groups”. This makes a "
            "quick, cheap test so you can check that everything works before you scan everything. Empty the box "
            "again for the real run."),
        section("What each button on the Run Pipeline tab does"),
        para("These are short descriptions. The “Run Pipeline” help page has the details."),
        bullets(
            f"{BTN_FETCH}: connects to your mailbox with the app password and reads the sender, subject and date "
            f"of each email. It only reads. It never changes or deletes anything.",
            f"{BTN_IMPORT}: does the same job from an exported file (.pst, .mbox or .eml) instead of connecting. "
            f"No password is needed. Outlook and Hotmail accounts are used this way.",
            f"{BTN_PREFILTER}: uses built-in rules on the subject lines to pick out emails that look like "
            f"sign-ups. No AI and no cost.",
            f"{BTN_BODIES}: downloads a short piece of text for only the emails that passed the pre-filter. "
            f"(Emails from an imported file already have it.)",
            f"{BTN_CLASSIFY}: shows the AI a few examples from each sender and asks whether they prove that you "
            f"created an account.",
            f"{BTN_CHANGES}: looks only at emails saying an account's email address was changed, to find clues "
            f"about accounts that moved to another address.",
            f"{BTN_FULL}: runs steps 2 to 5 in order, and starts with step 1 (the IMAP fetch) unless you untick "
            f"“Full pipeline starts with an IMAP fetch”. Untick it when you imported a file.",
            f"{BTN_STOP}: stops after the step it is on. Everything finished so far is kept.",
        ),
        section("What to expect from the results"),
        para("The Results tab has two tables. “Sign-ups” has one row for each service and email address: the "
             "service name, the address you used, the earliest sign-up date found, a category and a confidence "
             "score. “Possible email changes” shows clues that an account's email address was changed."),
        para("Treat the results as leads to check, not as proof. The AI can make mistakes, and Vestige can only "
             "find accounts that left an email behind. If you deleted a service's emails, it may not show up."),
        info("Nothing is saved unless you choose to",
             "Results stay in memory and are forgotten when you close Vestige. Click “Export now” on the Results "
             "tab to keep them. Write Mode on the Setup tab can also save your progress to a file. The "
             "“Privacy & security” page explains both."),
        section("Plain-English glossary"),
        bullets(
            "IMAP: the standard way a program reads the mail stored on your email provider's server.",
            "App password: a special password made by your email provider. It lets one program (here, Vestige) "
            "read your mailbox without knowing your real password. You can delete it later.",
            "API key: a secret code from an AI company that lets Vestige use their AI. Treat it like a password.",
            "Local AI: an AI that runs on your own computer, so nothing is sent over the internet.",
            "Header: the sender, subject, date and similar details at the top of an email.",
            "Mail file: a copy of your mail saved as a file. Vestige reads .pst (Outlook), .mbox and .eml files.",
            "Group: all the sign-up-looking emails from one sender. Vestige asks the AI about a group at a "
            "time, not about every email.",
        ),
    ),
)


GMAIL = Topic(
    id="gmail", nav="Gmail", title="Gmail (@gmail.com)",
    summary="Gmail works with an app password. You need 2-Step Verification switched on first.",
    checked=True,
    blocks=(
        settings("What to enter in Vestige (Add account)",
                 ("Provider", "gmail"),
                 ("Email address", "Your full Gmail address"),
                 ("App password", "The 16-character code from Google (not your normal Gmail password)"),
                 ("IMAP server", f"{KNOWN_IMAP_HOSTS['gmail']} (filled in for you)"),
                 ("IMAP port", "993 (filled in for you)")),
        section("Step 1: Turn on 2-Step Verification"),
        para("Google only offers app passwords once 2-Step Verification is on. If it is already on, go to Step 2."),
        steps(
            "Sign in to your Google Account and open “Security”.",
            "Under “How you sign in to Google”, choose “2-Step Verification” and follow the prompts.",
        ),
        section("Step 2: Create an app password"),
        steps(
            "Open the App passwords page with the first link below. Google may ask you to sign in again.",
            "Type a name so you remember what it is for, for example “Vestige”.",
            "Click “Create”.",
            "Copy the 16-character password that Google shows you.",
            "In Vestige, click “Add account” on the Setup tab, choose “gmail”, enter your address and paste the "
            "password into “App password”.",
        ),
        link("Open Google's App passwords page", URL_GOOGLE_APP_PASSWORDS),
        link("Google's help: Sign in with app passwords", URL_GOOGLE_APP_PASSWORD_HELP),
        tip("If sign-in fails",
            "Try pasting the app password again without any spaces in it."),
        section("Step 3: IMAP"),
        para("IMAP is how Vestige reads your mailbox. For personal Gmail accounts, Google says IMAP access is "
             "always turned on, so there is nothing to switch on. Older guides that tell you to “Enable IMAP” in "
             "Gmail's settings are out of date."),
        link("Google's help: Add Gmail to another email client", URL_GOOGLE_IMAP_HELP),
        section("If you can't find “App passwords”"),
        para("Google does not offer the App passwords page in these cases:"),
        bullets(
            "2-Step Verification is not turned on.",
            "Your 2-Step Verification uses only security keys.",
            "Your account is in Google's Advanced Protection Program.",
            "Your account is a work or school account run by an organisation. Ask your administrator, or use "
            "Google Takeout (below).",
        ),
        section("Good to know"),
        bullets(
            "Changing your Google password cancels all of your app passwords. If Vestige stops signing in after "
            "that, create a new one.",
            "Vestige reads your Inbox and the “All Mail” folder, so archived emails are included. It never marks "
            "emails as read and never changes, moves or deletes anything.",
            "When you have finished scanning, you can delete the app password on the same Google page.",
        ),
        section("No app password? Use Google Takeout instead"),
        para("Google Takeout lets you download a copy of your Gmail, which Vestige can read. No password is needed."),
        steps(
            "Open Google Takeout with the link below and sign in.",
            "Click “Deselect all”, then tick only “Mail”.",
            "Follow the prompts to create the export, and download it when Google says it is ready.",
            "The download is a .zip file. Unzip it. Inside is a .mbox file.",
            "In Vestige, open the Run Pipeline tab, click “1. Import mail file...”, choose the account, then "
            "choose the .mbox file.",
        ),
        link("Open Google Takeout", URL_GOOGLE_TAKEOUT),
    ),
)


YAHOO = Topic(
    id="yahoo", nav="Yahoo Mail", title="Yahoo Mail (@yahoo.com)",
    summary="Yahoo Mail works with an app password that you create on Yahoo's Account Security page.",
    checked=True,
    blocks=(
        settings("What to enter in Vestige (Add account)",
                 ("Provider", "yahoo"),
                 ("Email address", "Your full Yahoo address"),
                 ("App password", "The code Yahoo creates (not your normal Yahoo password)"),
                 ("IMAP server", f"{KNOWN_IMAP_HOSTS['yahoo']} (filled in for you)"),
                 ("IMAP port", "993 (filled in for you)")),
        section("Step 1: Create an app password"),
        steps(
            "Open Yahoo's Account Security page with the first link below and sign in.",
            "Under “External connections”, click “Create app password”.",
            "Type a name for it, for example “Vestige”.",
            "Click “Generate password”.",
            "Copy the password that Yahoo shows you, then click “Done”.",
            "In Vestige, click “Add account” on the Setup tab, choose “yahoo”, enter your address and paste the "
            "password into “App password”.",
        ),
        link("Open Yahoo's Account Security page", URL_YAHOO_SECURITY),
        link("Yahoo's help: Generate and manage 3rd-party app passwords", URL_YAHOO_APP_PASSWORD_HELP),
        tip("If Yahoo won't let you create one",
            "Yahoo's help says to create the app password in a browser you have used to sign in to Yahoo for "
            "several days in a row, and to avoid private (Incognito) windows."),
        section("Step 2: IMAP"),
        para(f"Yahoo's help page for IMAP lists the server ({KNOWN_IMAP_HOSTS['yahoo']}), port 993 with SSL, your "
             f"full email address and an app password as what an app needs. Vestige fills in the server and the "
             f"port when you choose “yahoo”."),
        link("Yahoo's help: IMAP server settings", URL_YAHOO_IMAP_HELP),
        section("Good to know"),
        bullets(
            "Yahoo says app passwords stay active even if you change your main Yahoo password. To cancel one, "
            "delete it on the Account Security page.",
            "Vestige scans your Inbox. If you want other folders too, type their exact names into the "
            "“Folders” box, separated by commas.",
            "Vestige never marks emails as read and never changes, moves or deletes anything.",
            "Prefer not to use an app password? Any mail program that can save your mail as .mbox or .eml files "
            "will do. Then use “1. Import mail file...”.",
        ),
    ),
)


ICLOUD = Topic(
    id="icloud", nav="iCloud Mail", title="iCloud Mail (@icloud.com)",
    summary="iCloud Mail works with an app-specific password. Your Apple Account needs two-factor authentication.",
    checked=True,
    blocks=(
        settings("What to enter in Vestige (Add account)",
                 ("Provider", "icloud"),
                 ("Email address", "Your full iCloud Mail address"),
                 ("App password", "The app-specific password from Apple (not your Apple Account password)"),
                 ("IMAP server", f"{KNOWN_IMAP_HOSTS['icloud']} (filled in for you)"),
                 ("IMAP port", "993 (filled in for you)")),
        section("Step 1: Check that two-factor authentication is on"),
        para("Apple only lets you create app-specific passwords if your Apple Account is protected with "
             "two-factor authentication. Most accounts already are. If yours is not, turn it on in your Apple "
             "Account settings first."),
        section("Step 2: Create an app-specific password"),
        steps(
            "Open your Apple Account with the first link below and sign in.",
            "In the “Sign-In and Security” section, choose “App-Specific Passwords”.",
            "Follow the on-screen steps to generate a new password. Give it a name such as “Vestige”.",
            "Copy the password.",
            "In Vestige, click “Add account” on the Setup tab, choose “icloud”, enter your iCloud email address "
            "and paste the password into “App password”.",
        ),
        link("Open your Apple Account", URL_APPLE_ACCOUNT),
        link("Apple's help: app-specific passwords", URL_APPLE_APP_PASSWORD_HELP),
        section("Step 3: IMAP"),
        para(f"iCloud Mail uses IMAP. Apple's page of server settings lists the incoming server "
             f"({KNOWN_IMAP_HOSTS['icloud']}), port 993 and SSL required, together with an app-specific password, "
             f"as what an app needs. Vestige fills in the server and the port when you choose “icloud”."),
        link("Apple's help: iCloud Mail server settings", URL_APPLE_IMAP_HELP),
        warn("About the username",
             "Apple's page says the IMAP username is usually just the first part of your iCloud address (for "
             "example “johnappleseed”, not “johnappleseed@icloud.com”), and says to try the full address if that "
             "does not work. Vestige always signs in with the full address from the “Email address” box. If you "
             "are sure the app-specific password is right and sign-in still fails, this difference may be the "
             "reason. In that case, use a mail program that can save your mail as .mbox or .eml files and import "
             "those instead."),
        section("Good to know"),
        bullets(
            "You can have up to 25 active app-specific passwords.",
            "Apple cancels all of your app-specific passwords whenever you change or reset your main Apple "
            "Account password. If Vestige stops signing in after that, create a new one.",
            "Vestige scans your Inbox. If you want other folders too, type their exact names into the "
            "“Folders” box, separated by commas.",
            "Vestige never marks emails as read and never changes, moves or deletes anything.",
            "When you have finished scanning, you can revoke the password in the same section of your Apple "
            "Account.",
        ),
    ),
)


MICROSOFT = Topic(
    id="microsoft", nav="Outlook & Hotmail", title="Outlook, Hotmail, Live and MSN",
    summary="Microsoft no longer allows the password sign-in Vestige would need, so these accounts are used by "
            "importing a mailbox file.",
    checked=True,
    blocks=(
        section("Why IMAP doesn't work with these accounts"),
        para("IMAP is the standard way for a program to sign in to a mailbox and read it. For years that worked "
             "with a password or an app password. On 16 September 2024, Microsoft stopped accepting that for "
             "personal Outlook.com, Hotmail, Live and MSN accounts. These accounts now only accept a newer "
             "sign-in method called OAuth, which Microsoft calls “modern authentication”."),
        para("With OAuth, the program is registered with Microsoft and opens a Microsoft sign-in window. Vestige "
             "does not currently support that sign-in. So a password or app password will always be refused for "
             "these accounts, even a correct one. This is why Vestige doesn't ask you for a password when you "
             "add an Outlook or Hotmail address."),
        para("Microsoft 365 work and school mailboxes have also moved to this newer sign-in for IMAP."),
        link("Microsoft's notice about this change", URL_MS_BASIC_AUTH_NOTICE),
        section("What to do instead: export your mailbox, then import it"),
        para("Microsoft documents a way to export an Outlook.com mailbox to a .pst file using the Outlook desktop "
             "app. Vestige reads .pst files directly, so there is nothing to convert. Microsoft says the file "
             "holds a copy of your mail up to the day you create it."),
        steps(
            "Get the Outlook desktop app for Windows. If you don't have it, Microsoft says you can sign up for a "
            "free 30-day trial of Microsoft 365.",
            "Open Outlook and add your Outlook.com, Hotmail, Live or MSN account. Wait for your mail to finish "
            "downloading. A big mailbox can take a long time.",
            "Export the mailbox to a .pst file. In new Outlook for Windows, go to Settings > Files > Export, "
            "click “Get started” and follow the wizard to choose the mailbox or folders. In classic Outlook, "
            "go to File > Open & Export > Import/Export > “Export to a file” > “Outlook Data File (.pst)”.",
            "Save the .pst file somewhere you trust. It contains your emails, so an encrypted drive is best.",
            "In Vestige, click “Add account” on the Setup tab. Enter a nickname and your Outlook or Hotmail "
            "address, and leave the password empty. Vestige turns the password box off for these accounts.",
            f"Go to the Run Pipeline tab, click “{BTN_IMPORT}”, choose that account, then choose your .pst file.",
            f"Carry on with the pipeline. Untick “Full pipeline starts with an IMAP fetch”, then click "
            f"“{BTN_FULL}” (or use buttons 2 to 5 one at a time).",
        ),
        link("Microsoft's help: Export your Outlook.com mailbox", URL_MS_EXPORT_OUTLOOKCOM),
        link("Microsoft's help: Export emails to a .pst file", URL_MS_EXPORT_PST),
        link("Microsoft 365 free trial (for the Outlook desktop app)", URL_MS_TRIAL),
        info("What Vestige reads from a .pst file",
             "Vestige only reads the file and never changes it. From each message it takes the sender, "
             "recipient, subject, date and a short piece of text. It skips Sent Items, Drafts, Outbox and "
             "similar folders (also Calendar, Contacts, Tasks and Notes), and any message sent from your own "
             "address. Deleted Items and Junk are read, because people often delete a welcome email after "
             "signing up. The log lists every folder that was read or skipped."),
        tip("If Vestige says .pst support is missing",
            "Your copy of Vestige was built without it. If you run Vestige from its source code, install the "
            "free package that reads .pst files by typing “pip install libpff-python” in a terminal."),
        section("Another way: Thunderbird"),
        para("Mozilla Thunderbird is a free mail program. Microsoft's own help lists it as supporting the newer "
             "sign-in for Outlook.com accounts. You can add your account to Thunderbird, let it download your "
             "mail, and then import the mail files it saves."),
        steps(
            "Install Thunderbird and add your Outlook or Hotmail account. Sign in in the Microsoft window it "
            "opens.",
            "Make sure the account is set to keep its messages on your computer, then wait for the download "
            "to finish.",
            "In Thunderbird, choose Help > Troubleshooting Information. Next to “Profile Folder”, click "
            "“Open Folder”.",
            "Open the “ImapMail” folder, then the folder for your account. By default Thunderbird saves each "
            "mail folder as one MBOX file with no file extension. “Inbox” is the one to start with.",
            f"In Vestige, click “{BTN_IMPORT}” and choose those files. Choose “All files” in the file window "
            f"so they are listed. Vestige recognises MBOX files by what is inside them, so the missing "
            f"extension does not matter.",
        ),
        section("Other files you may have"),
        bullets(
            ".olm files (from Outlook for Mac): Vestige can't read these. Add the account to Outlook for Windows "
            "and export a .pst there, or use Thunderbird.",
            ".msg files (single Outlook messages): not supported. Export the whole mailbox as a .pst instead.",
            ".csv files from Outlook: not supported. An Outlook CSV export does not normally include the sending "
            "date or message ID that Vestige needs.",
            ".zip files: unzip them first, then choose the files inside.",
        ),
    ),
)


OTHER = Topic(
    id="other", nav="Other email", title="Other email providers",
    summary="Most email providers support IMAP, but each one sets it up a little differently. "
            "This page shows you what to look for.",
    blocks=(
        para("IMAP is a standard way for programs to read mail from a provider's server. Providers that are not in "
             "Vestige's “Provider” list use the “other” option, where you type in the settings yourself. Every "
             "provider does this differently, so Vestige can't give you exact steps for yours. You need to look "
             "up how your own provider handles IMAP."),
        section("What you need to find out"),
        bullets(
            "The IMAP server address. It usually looks like imap.yourprovider.com.",
            "The port number. For a secure connection this is almost always 993.",
            "Whether you have to switch IMAP on in your account settings. Some providers have it on already, "
            "some don't.",
            "Which password to use. Many providers need a special “app password” instead of your normal "
            "password, especially if you use two-step verification. You usually create it in the provider's "
            "security settings.",
        ),
        section("How to find these details"),
        steps(
            "Search the web for your provider's name followed by “IMAP settings”. Use your provider's own help "
            "pages if you can. They are the most reliable.",
            "Write down the incoming (IMAP) server address and the port. Ignore the outgoing (SMTP) details. "
            "Vestige never sends email.",
            "Check the provider's help to see whether IMAP has to be turned on. If it does, turn it on.",
            "Check whether you need an app password. If you do, create one in your account's security settings.",
            "In Vestige, click “Add account” on the Setup tab and choose “other”. Fill in the nickname, your "
            "email address, the password, the IMAP server and the IMAP port.",
            "Leave “Folders” empty to scan the Inbox only, or list more folders separated by commas.",
        ),
        section("What Vestige can and can't connect to"),
        bullets(
            "Secure connections only. Vestige uses an encrypted IMAP connection (SSL/TLS, normally port 993). "
            "It can't connect on the older unencrypted port 143, or to servers that start unencrypted and then "
            "upgrade (STARTTLS).",
            "Username. Vestige signs in with your full email address as the username. If your provider needs a "
            "different username, IMAP sign-in won't work for that account.",
            "Folders. Names must match exactly how your provider writes them. A name with spaces needs double "
            "quotes around it. Folders that don't exist are skipped, and the log says so.",
        ),
        warn("Outlook, Hotmail, Live and MSN addresses",
             "These don't work with IMAP and a password. See the “Outlook & Hotmail” page instead."),
        section("If it doesn't work"),
        bullets(
            "“Login failed” means the provider refused the sign-in. Check the app password, the email address, "
            "the server and the port. Vestige reports a failed sign-in once and does not retry. This avoids "
            "repeated wrong attempts that could get an account locked.",
            "Look at the log at the bottom of the Run Pipeline tab. It shows the message the server sent back.",
            f"If your provider doesn't offer IMAP, or you can't get it working, export your mail to a .mbox or "
            f".eml file (many desktop mail programs can) and use “{BTN_IMPORT}”. No password is needed.",
        ),
    ),
)


AI = Topic(
    id="ai", nav="AI: cloud or local", title="Choosing your AI",
    summary="Vestige uses an AI to decide which emails are real account sign-ups. You choose where that AI runs.",
    checked=True,
    blocks=(
        para("On the Setup tab, under “AI provider”, there are two choices. The difference that matters most is "
             "where your email text goes."),
        section("Cloud AI (API key)"),
        para("The AI runs on a company's servers. Vestige sends it a small amount of text from emails that look "
             "like sign-ups, over the internet, together with your API key."),
        bullets(
            "Easy to set up, and it works on any computer.",
            "The AI company receives the text Vestige sends. Each company has its own rules for how it handles "
            "that data, so read the company's policy before you use it.",
            "Companies usually charge for API use (some offer limited free use). API billing is separate from a "
            "chat subscription such as ChatGPT Plus or Claude Pro. Check the company's pricing page.",
            f"Providers in Vestige's list: {', '.join(CLOUD_PROVIDER_NAMES)}. “Other (OpenAI-compatible)” lets "
            f"you type the server address of any service that works like OpenAI's.",
        ),
        section("Local AI (runs on this computer)"),
        para("The AI runs on your own computer, using a free program such as Ollama. The email text never leaves "
             "your computer, and there are no API fees."),
        bullets(
            "The best choice for privacy. Nothing is sent over the internet.",
            "It needs a reasonably capable computer. Bigger models need more memory and run slowly without a "
            "graphics card.",
            f"Small local models are less dependable than large cloud models at giving the structured answers "
            f"Vestige needs, so results can be less accurate. That is why Vestige's default for local AI is "
            f"{DEFAULT_BATCH_LOCAL} groups per request instead of {DEFAULT_BATCH_CLOUD}.",
            f"Programs in Vestige's list: {', '.join(LOCAL_PROVIDER_NAMES)}. “Other local server” works with any "
            f"program that offers an OpenAI-style address.",
        ),
        section("What is sent to the AI"),
        para(f"For each group of emails from one sender, Vestige sends the sender's domain and up to "
             f"{SIGNUP_SAMPLES} sample emails. Each sample is the date, the subject and the first "
             f"{SIGNUP_CHARS} characters of the body text. For email-change notices it sends the sender, date, "
             f"subject and the first {CHANGE_CHARS} characters of the body."),
        para("Vestige does not send your list of addresses, the “To” field or the rest of your mail. But the text "
             "of an email can contain anything, such as your name or your address, and that text is part of the "
             "sample. “Test connection” sends the AI a tiny test question, and “Find models” asks the server which "
             "models it offers."),
        info("The coloured line on the Setup tab",
             "Under the AI settings, Vestige shows a line that says exactly where your email text will go. "
             "Green: it stays on this computer. Amber: it goes to another device on your network, or to the "
             "company you chose over the internet. Red: a warning that the “local” address is actually a server "
             "elsewhere on the internet."),
        section("How to get an API key (cloud AI)"),
        para("An API key is a long secret code that tells the AI company the requests come from your account. "
             "Treat it like a password. The steps are similar for every company:"),
        steps(
            "Create an account on the company's developer website. This is often a different website from the "
            "company's chat website.",
            "Add a payment method or credit if the company asks for one.",
            "Open the “API keys” page and create a new key.",
            "Copy the key straight away. Most companies show it only once.",
            "In Vestige, choose the provider, paste the key into “API Key”, and click “Test connection”.",
        ),
        para("These pages create keys for the providers in Vestige's list. For TogetherAI, xAI (Grok) and other "
             "companies, sign in to the company's developer website and look for “API keys”."),
        link("Anthropic (Claude): get your API key", URL_ANTHROPIC_KEYS),
        link("OpenAI: API keys", URL_OPENAI_KEYS),
        link("Google Gemini: API keys (Google AI Studio)", URL_GEMINI_KEYS),
        link("Groq: API keys", URL_GROQ_KEYS),
        link("OpenRouter: API keys", URL_OPENROUTER_KEYS),
        link("Mistral: API keys", URL_MISTRAL_KEYS),
        link("DeepSeek: API keys", URL_DEEPSEEK_KEYS),
        bullets(
            "Anthropic's model name is filled in for you. For most other providers the “Model” box starts empty, "
            "because model names change often. Click “Find models” and pick one, or copy a current name from "
            "the company's documentation.",
            "Vestige never saves your key. It is kept in memory and forgotten when you close the app. If a key "
            "is ever exposed, delete it on the company's website and create a new one.",
        ),
        section("How to use Ollama (local AI)"),
        steps(
            "Download Ollama from its website (link below) and install it. Versions exist for Windows, macOS and "
            "Linux.",
            "Download a model. Open a terminal (Command Prompt or PowerShell on Windows) and type "
            "“ollama pull” followed by a model name, for example “ollama pull llama3.2”. You can browse "
            "model names in Ollama's library (link below).",
            "Make sure Ollama is running. After you install it, it normally runs in the background.",
            f"In Vestige, go to Setup, choose “Local AI (runs on this computer)” and the provider “Ollama”. "
            f"The server address fills in as {LOCAL_PRESETS['Ollama']['base_url']}.",
            "Click “Find models” and pick the model you downloaded (or type its exact name), then click "
            "“Test connection”.",
        ),
        link("Download Ollama", URL_OLLAMA_DOWNLOAD),
        link("Browse Ollama's model library", URL_OLLAMA_LIBRARY),
        bullets(
            "Models are large downloads, often a few gigabytes, and need disk space and memory. Model names "
            "change, so use one that is listed in Ollama's library.",
            "Vestige warns you in red if the “local” address you typed is not on this computer or your home "
            "network, because your email text would then travel over the internet.",
        ),
    ),
)


PIPELINE = Topic(
    id="pipeline", nav="Run Pipeline", title="The Run Pipeline tab",
    summary="What each option, button and counter does, and in what order.",
    blocks=(
        para("The pipeline turns your mailbox into a list of accounts. It works in stages. You can run the whole "
             "thing with one button, or one stage at a time. Every stage is safe to run again: Vestige skips "
             "work it has already done."),
        section("Options (top of the tab)"),
        bullets(
            "Only mail since: limits the IMAP fetch to emails from that date onward. Write the date like "
            "01-Jan-2024. Leave it empty for all mail. It does not apply to imported files.",
            f"Groups per AI request: how many sender groups go into one request to the AI. The default is "
            f"{DEFAULT_BATCH_CLOUD} for cloud AI and {DEFAULT_BATCH_LOCAL} for local AI. Larger numbers mean fewer "
            f"requests but are harder for small models. For the email-change stage the number is capped at 5.",
            "Limit AI to N groups: stops the AI stage after N groups. Use it for a cheap test run. Empty means "
            "no limit.",
            "Full pipeline starts with an IMAP fetch: untick this when you imported a file and don't want the "
            "full run to try an IMAP fetch first.",
        ),
        section("Accounts to fetch over IMAP"),
        para("Tick the accounts you want “Fetch headers”, “Fetch email bodies” and the full run to use. If you "
             "tick none, Vestige uses every account that has an app password. Accounts without a password are "
             "used for file import only. “Select all” ticks every account."),
        section("The stages"),
        bullets(
            f"{BTN_FETCH}: connects to each account over IMAP and reads the sender, recipient, subject, date, "
            f"message ID and unsubscribe header of every email in the scanned folders. It uses read-only access "
            f"and never marks emails as read. Emails already stored are skipped, so running it again only adds "
            f"new mail. A failed sign-in is reported once and is not retried. A big mailbox can take a long time.",
            f"{BTN_IMPORT}: does the same job from a file. You pick which account the mail belongs to (this is "
            f"used only to work out which of your addresses each email was sent to), then choose files or a "
            f"folder of .eml files. It reads .pst (Outlook, Hotmail), .mbox and .eml. Imported emails already "
            f"include their text snippet, so stage 3 isn't needed for them.",
            f"{BTN_PREFILTER}: scores every subject line with built-in rules. A subject such as “Welcome to…” or "
            f"“Verify your email” scores high. A subject such as “50% off” or “Your order” scores low. Emails "
            f"that score high enough become “candidates”. It also flags emails that look like “your email "
            f"address was changed” notices. The rules are generous, so a borderline email stays a candidate for "
            f"the AI to judge. This stage is free and uses no AI.",
            f"{BTN_BODIES}: for candidates only, downloads up to the first {BODY_KB_DOWNLOADED} KB of each email "
            f"and keeps the first {BODY_CHARS_KEPT} characters of its text. This is what the AI will read.",
            f"{BTN_CLASSIFY}: groups the candidates by sender. For each group the AI sees up to "
            f"{SIGNUP_SAMPLES} samples and answers: is this a real account sign-up, what is the service called, "
            f"its category, how the account was created, and how confident it is. Groups that were already "
            f"judged are skipped. Answers are kept after every request, so if something fails you can just run "
            f"it again. If requests keep failing, Vestige stops and tells you.",
            f"{BTN_CHANGES}: looks only at the emails flagged as email-change notices. The AI copies out what "
            f"each notice literally says: the service, whether it went to the old or the new address, and any "
            f"address printed in it (often partly hidden, like j***@gmail.com). Then plain code, not the AI, "
            f"checks which of your real addresses could fit. Nothing is invented.",
            f"{BTN_FULL}: runs the stages in order: step 1 (unless you unticked it), then 2, 3, 4 and 5. It "
            f"does not export anything.",
            f"{BTN_STOP}: asks the running stage to stop. It finishes the step it is on, and everything finished "
            f"so far is kept.",
        ),
        section("Progress, status and log"),
        bullets(
            "Progress bar: shows how far the whole run has got. It shows a moving “working…” bar while the "
            "total is not known yet.",
            "Status line: Emails scanned is every email stored. Sign-up candidates passed the pre-filter. Sender "
            "groups classified is how many groups the AI has judged. Confirmed sign-ups is how many of those "
            "groups it judged to be real sign-ups. Email-change notices is how many emails look like change "
            "notices. Possible email changes is how many of those the AI judged to be real. “Refresh status” "
            "updates the line.",
            "Log: lists what is happening, including warnings and any problems. It is kept in memory only. At the "
            "start of each AI stage it prints a line saying where your email text is going.",
        ),
        tip("A sensible first run",
            "Add an account, choose your AI, type 5 into “Limit AI to N groups”, then click the full run. If the "
            "results look right, clear the box and run it again. Work already done is skipped, so the second "
            "run only does what is left."),
    ),
)


def _results_text() -> tuple:
    return (", ".join(CATEGORIES), ", ".join(SIGNUP_METHODS))


_CATEGORY_TEXT, _METHOD_TEXT = _results_text()

RESULTS = Topic(
    id="results", nav="Results", title="Understanding your results",
    summary="What the tables show, how sure each row is, and how to save them.",
    blocks=(
        para("The Results tab has two tables. Use the switch at the top to move between them. Click a column "
             "heading to sort by it. Double-click a row to see every detail about it."),
        section("Minimum confidence"),
        para("Each row has a confidence score from 0 to 1, given by the AI. The box at the top (it starts at 0.5) "
             "hides rows below that score. Raise it to see only the strongest results. Lower it to see weaker "
             "guesses too. Click “Refresh” after changing it. The same number is used when you export."),
        section("The “Sign-ups” table"),
        para("One row for each service and email address you used with it. If you used two addresses for the "
             "same service, you get two rows."),
        bullets(
            "Service Name: the service the AI found.",
            "Email Address Used: which of your addresses received that service's emails.",
            "Sign-up Date: the date of the earliest sign-up-looking email Vestige found from that sender to that "
            "address. If older emails were deleted, the real sign-up may be earlier.",
            f"Category: the kind of service. The AI picks one of: {_CATEGORY_TEXT}.",
            f"Sign-up Method: how the account was most likely made. “native_password” means with an email "
            f"address and password. The others mean “Sign in with…” through another company. The possible values "
            f"are: {_METHOD_TEXT}.",
            "Confidence: how sure the AI is, from 0 to 1.",
            "Source Domain(s): the website domain(s) the emails came from.",
        ),
        section("The “Possible email changes” table"),
        para("Some services send a notice when the email address on an account is changed. A notice can be a clue "
             "that an account you can't log in to now uses a different address. These are clues, not proof. A "
             "hidden address like j***@gmail.com is only matched against addresses found in the mail you "
             "scanned."),
        bullets(
            "Service and Notice date: which service sent the notice, and when.",
            "Notice sent to: the address the notice was delivered to. Vestige knows this for certain.",
            "Previous address and Account may now use: the old and the new address. The side the notice was sent "
            "to is certain. The other side is worked out from what the notice prints.",
            "Match: Strong means exactly one of your known addresses fits. Possible (several fit) means more than "
            "one fits. Hint only means the notice printed something but none of your scanned addresses fits. "
            "No address shown means the notice doesn't say.",
            "Confidence: how sure the AI is about its reading of the notice, from 0 to 1.",
        ),
        section("Using the results sensibly"),
        bullets(
            "The AI can be wrong. Check anything important on the service's own website.",
            "Vestige can only find accounts that left an email behind. If you deleted the emails or the service "
            "never sent any, it can't see them.",
            "Click “Refresh” if the tables look out of date.",
        ),
        section("Exporting"),
        para("Nothing is written to your computer until you click “Export now”. There is no default folder: you "
             "choose where the files go."),
        steps(
            "Click “Browse...” and choose a folder. A drive you trust is best, because the files list your "
            "services and addresses in plain text.",
            "Set the file name. It starts as “signups”.",
            "Tick CSV (opens in a spreadsheet), Markdown (a plain readable text file), or both.",
            "Optional: “Add date & time to file names” keeps repeated exports apart. “Put files in a new dated "
            "sub-folder” puts each export in its own folder.",
            "Click “Export now”.",
        ),
        bullets(
            "CSV gives one file for the sign-ups, and a second one with “_possible_email_changes” in its name if "
            "any changes were found. Markdown gives one file that holds both tables.",
            "Existing files are never overwritten. If a name is taken, Vestige adds (2), (3) and so on.",
            "In the CSV, any cell that starts with =, +, - or @ gets a ' put in front, so a spreadsheet can't "
            "treat it as a formula.",
            "If the folder looks like it is inside a cloud-sync folder such as OneDrive, Vestige warns you before "
            "it exports.",
        ),
    ),
)


PRIVACY = Topic(
    id="privacy", nav="Privacy & security", title="Privacy and security",
    summary="Exactly what Vestige reads, what leaves your computer, and what is saved.",
    blocks=(
        section("The short version"),
        bullets(
            "Vestige runs on your computer and does not contact any server of its own. It has no accounts, "
            "no tracking and no analytics.",
            "By default nothing is saved to your drive. Accounts, passwords, API keys, server addresses and "
            "results are held in memory and forgotten when you close Vestige.",
            "Vestige only ever sends data to two places: your own mail provider (if you use IMAP) and the AI "
            "you choose.",
            "The only files Vestige can write are the exports you choose to make, and the optional Write Mode "
            "file.",
        ),
        section("What Vestige reads from your mail"),
        bullets(
            "At first only the headers: sender, recipient, subject, date, message ID and unsubscribe header.",
            f"Later, for emails that look like sign-ups only, the first part of the text. About {BODY_KB_DOWNLOADED} KB "
            f"is downloaded and the first {BODY_CHARS_KEPT} characters are kept.",
            "It only reads. It never sends, moves, flags or deletes mail, and it never marks mail as read. IMAP "
            "folders are opened read-only.",
            "Mail files (.pst, .mbox, .eml) are only read. The original file is never changed.",
        ),
        section("What leaves your computer"),
        bullets(
            "Your mail provider: when you use IMAP, Vestige connects over an encrypted (SSL/TLS) connection and "
            "signs in with your address and app password. Nothing else goes there.",
            f"Your AI: with a cloud AI, the sample text described on the “AI: cloud or local” page (up to "
            f"{SIGNUP_SAMPLES} samples per sender, each with the date, the subject and the first {SIGNUP_CHARS} "
            f"characters of text) and your API key are sent over the internet to that company. With a local AI "
            f"on this computer, nothing leaves it.",
            "Links in this Help window: if you click one, it opens in your normal web browser. Vestige itself "
            "never contacts those websites.",
            "Nothing else. There is no update check, no analytics and no crash reporting.",
        ),
        section("What is stored, and where"),
        bullets(
            "Memory only (the default): everything is forgotten when you close Vestige. If you have results you "
            "haven't exported, Vestige warns you before it closes.",
            f"Write Mode (optional, off by default): saves your results to a file called {DATA_FOLDER}, next to the "
            f"program, so a crash doesn't lose your work. It contains sender names and addresses, subject lines, "
            f"short pieces of email text, which of your addresses you used, and the AI's results. It never "
            f"contains passwords, API keys or AI server addresses. The file is not encrypted. Vestige shows a "
            f"warning before turning it on, warns you if the folder looks cloud-synced, and asks on close whether "
            f"to keep or delete the file.",
            "Exports (only when you click “Export now”): plain text CSV and Markdown files in a folder you pick. "
            "They list your services and the addresses you used.",
        ),
        info("Deleting a file does not erase it",
             "When you delete a file, the computer removes it from view but doesn't wipe its contents from the "
             "drive straight away. For real protection, keep Vestige and its saved files on an encrypted drive."),
        section("Your passwords and keys"),
        bullets(
            "App passwords and API keys are held in memory only. They are never written to a file, not even in "
            "Write Mode. Vestige has no “remember me” feature.",
            "An app password is sent only to your mail provider. An API key is sent only to the AI company you "
            "chose. The password and key boxes hide what you type unless you tick “show”.",
            "Switching to Local AI clears the API key box.",
            "An app password gives access to your whole mailbox and an API key can run up charges on your "
            "account. When you have finished, delete the app password on your email provider's website, and "
            "delete the API key on the AI company's website if you no longer need it.",
        ),
        section("Choosing how private to be"),
        bullets(
            "Most private: a local AI and a mail file you exported. Nothing leaves your computer at all.",
            "A local AI with IMAP: your mail provider is contacted, but no email text goes to an AI company.",
            "A cloud AI with a mail file: no mail provider is contacted, but sample text goes to the AI company.",
            "A cloud AI with IMAP: both happen.",
            "Whichever you pick: leave Write Mode off unless you need it, export to an encrypted drive, and "
            "delete your exports when you no longer need them.",
        ),
        section("What Vestige can't protect"),
        bullets(
            "How an AI company stores and uses the text you send is decided by that company, not by Vestige. "
            "Read its policy.",
            "Other programs already running on your computer. If your computer is not safe, nothing you type into "
            "any program is.",
            "Information inside exported files once you move or share them.",
        ),
    ),
)


ABOUT = Topic(
    id="about", nav="About Vestige", title="About Vestige",
    summary="Who makes it, how it is licensed, and some friendly ways to say thanks.",
    blocks=(
        identity(app_info.APP_NAME, f"Version {app_info.VERSION_LABEL}", app_info.COPYRIGHT),
        section("What Vestige does"),
        para("Vestige helps you find online accounts you may have forgotten about. It looks through the emails in "
             "your mailboxes, either directly or from a mail file you export, and works out which messages are "
             "sign-up confirmations. Then it lists each service with the email address you used and the date "
             "you signed up."),
        para("An AI of your choice does the judging, including one that runs entirely on your own computer. "
             "Vestige keeps nothing on your drive unless you ask it to."),
        section("License"),
        para(f"Vestige is open-source software, released under the {app_info.LICENSE_FULL}. In plain terms, you "
             f"are free to use it, study how it works, share it and change it. If you share a copy, the same "
             f"license has to come with it, and the source code has to be available."),
        para("Vestige comes with no warranty. The full license text explains your rights and the terms in detail."),
        link("Read the full license on GitHub", app_info.LICENSE_URL),
        section("Say thank you, if you like"),
        para("Vestige is an independent project. If it has helped you and you would like to say thanks, here are "
             "a few friendly ways to do it. There is no pressure at all. Vestige works the same for everyone, "
             "whether or not you ever do any of these."),
        support_buttons(
            SupportButton("star", "Star Vestige on GitHub",
                          "A free way to help. A star can make it easier for other people to find the project.",
                          app_info.REPO_URL),
            SupportButton("coffee", "Tip on Ko-fi",
                          "A small, one-off thank-you, if you feel like it.",
                          app_info.KOFI_URL),
            SupportButton("heart", "Sponsor on GitHub",
                          "Support the project through GitHub Sponsors.",
                          app_info.SPONSORS_URL),
        ),
        para("However you choose to support it, or not at all, thank you for using Vestige."),
    ),
)


# ---------------------------------------------------------------------------
# Registry and navigation
# ---------------------------------------------------------------------------

TOPICS = {t.id: t for t in (START, GMAIL, YAHOO, ICLOUD, MICROSOFT, OTHER, AI, PIPELINE, RESULTS, PRIVACY, ABOUT)}

# The left-hand list: (group heading, topic ids in order).
NAV = (
    ("Start here", ("start",)),
    ("Email accounts", ("gmail", "yahoo", "icloud", "microsoft", "other")),
    ("Using Vestige", ("ai", "pipeline", "results")),
    ("Your data", ("privacy",)),
    ("About", ("about",)),
)

DEFAULT_TOPIC = "start"

# Which help page goes with which choice in the Add account window's "Provider" list.
PROVIDER_TOPICS = {"gmail": "gmail", "yahoo": "yahoo", "icloud": "icloud", "outlook": "microsoft", "other": "other"}


def get_topic(topic_id: str) -> Topic:
    """The topic for an id; an unknown id gives the Getting started page."""
    return TOPICS.get(topic_id) or TOPICS[DEFAULT_TOPIC]


def topic_for_account(provider: str, email: str = "", server: str = "") -> str:
    """Which help page fits an account being added: an Outlook/Hotmail/Live/MSN address (or Microsoft
    server) always gets the Microsoft page, whatever the provider drop-down says."""
    if is_microsoft_mail(email, server):
        return "microsoft"
    return PROVIDER_TOPICS.get(provider, "other")


def all_urls() -> list:
    """Every web address a topic can open, as (topic_id, url): Link blocks and support buttons. Used by the tests."""
    found = [(topic_id, block.url) for topic_id, block in all_links()]
    for topic in TOPICS.values():
        for block in topic.blocks:
            if isinstance(block, SupportButtons):
                found.extend((topic.id, item.url) for item in block.items)
    return found


def all_links() -> list:
    """Every Link in every topic, as (topic_id, Link). Used by the tests."""
    return [(t.id, b) for t in TOPICS.values() for b in t.blocks if isinstance(b, Link)]
