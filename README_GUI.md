# Vestige desktop app guide

The desktop app is the privacy-first way to use Vestige: **nothing is saved unless you choose to** (see
[Privacy and data handling](README.md#privacy-and-data-handling) in the main README). To install it, see
[Installation](README.md#installation). Inside the app, **? Help** (or F1) opens help for the page you are on.

## Tour

### Setup
The page **scrolls**, and each section is its own card. A line at the top says what to do next (add an account, set up the AI, import mail, or go and run) and turns green when you are ready. Top to bottom: Email accounts, AI provider, then Storage.

**Email accounts.** Add as many as you like. An app password is only needed to fetch over IMAP; leave it blank to
use an account for **file import** only (.pst, .mbox or .eml; no credentials at all). Outlook / Hotmail / Live accounts can only be used this way, because Microsoft no longer accepts passwords over IMAP. **Import mail file...**, beside the account buttons, asks which account the mail belongs to (the one highlighted in the list is offered first) and which file(s) or folder to read. The app then switches to Run Pipeline so you can watch the import.

**AI provider.** Choose **Cloud AI (API key)** or **Local AI (runs on this computer)**. Pick a provider, set the
server address and model (*Find models* lists what's available), and press *Test connection*. The coloured line
under the form always states where your data will go (green = stays on this computer). Switching to Local clears
any typed API key. Nothing here is ever written to disk.

**Storage (optional).** *Write Mode* is off by default: everything stays in memory and disappears when you close the app.
Turn it on to save scan progress so a crash or accidental close doesn't waste scanning time and AI cost. You get a
warning first, listing what will be saved (sensitive results; never passwords, keys or server addresses) and
where (`Vestige_DATA` next to the program). On close in write mode it asks whether to keep or delete the file;
next launch it offers to resume a saved session. Deleting is an ordinary delete, not a secure erase (the app says so).

### Run Pipeline
The page **scrolls**, so everything stays reachable in a small window, and each section is its own card. From the
top:

* **Fetch mail from your accounts.** A tick-box list of accounts to fetch over IMAP (none ticked = all accounts
  that have an app password), *Only mail since*, and a switch (**Fetch new mail first**) for whether the full run
  fetches mail before it starts. Mail files are imported on Setup instead.
* **What to run.** Tick the pipelines to include: **Sign-ups**, **Email changes**, **Closed accounts**,
  **Subscriptions** (all four at first). Under the boxes a line says which will run and which are skipped; a
  skipped pipeline costs nothing, because its emails aren't downloaded or sent to the AI. **Depth** is a
  three-way switch: **Quick** (cheapest and fastest), **Balanced** (the default) or **Thorough** (most careful). It sets
  how many sample emails per sender, and how many characters of each, the AI reads.
* **AI requests.** *Senders per AI request* and *Test run: check only N senders* (the cheap test-run option; it
  does not limit the Email changes pipeline).
  The first time you run the AI in a session, if it is not on this computer, a window says where the email text
  will go and shows these settings; agreeing once covers the rest of the session.
* **Individual steps.** Folded away until you click **Show steps**. A button for each of steps 1-7 (steps 1-3 are
  the free ones, step 1 being **Scan mailbox (IMAP)**; steps 4-7 are the four AI pipelines), for running one
  thing at a time. They use the same depth.
* **Status and log.** The counters, one row for each pipeline, and the live log. **Drag the bar under the log** up
  or down to make it taller or shorter; double-click it to reset. The mouse wheel over the log scrolls the log, not
  the page.
* **Progress, Run selected pipelines and Stop** are pinned to the bottom of the page, so they stay in view wherever
  you have scrolled. **Run selected pipelines** runs the free steps, then the ticked pipelines. It never exports.

**The progress bar:**
* It shows the step ("Step 3 of 5: Downloading email text"; the number of steps depends on what you ticked), an overall percentage, and a detail line such as
  "412 of 1,380 emails".
* While a stage doesn't yet know how big its job is (for example a mail file that is still being indexed, or a
  mail server that hasn't said how many messages a folder holds), the bar shows a moving **"working..."**
  marker instead of a number, and switches to a real percentage as soon as the total is known.
* It never moves backwards, even if a later folder turns out to hold more mail than first thought; it simply
  pauses until the new total catches up.
* It is a **general guide, not an exact clock.** Each step of the full pipeline is given an estimated share of the
  total time (scanning the mailbox 25%, shortlisting 5%, downloading text 15%, sign-up AI 40%, email-change AI 15%, closed-account AI 10%,
  subscription AI 15%; the weights are shared out over the steps you ticked). Those weights are in
  `pipeline.py` (`STAGES`) if you want to tune them. Run a single stage on its own and the bar covers just that stage.
* If you press **Stop** it reports where it stopped and that everything finished so far is kept.

### Results
Four tables; switch between them at the top:

* **Sign-ups** - one row per service *and* address. **Account check** notes a confirmed closure notice for that
  service ("Likely closed" / "Possibly closed"); **Activity hint** is a weaker, separate clue ("No mail since
  2022-03-14") that is never called "closed". The service always stays listed.
* **Possible email changes** - clues that an account's address was changed.
* **Closed accounts** - confirmed closure notices, the address each was sent to (**Email Address Used**), and
  whether the service has emailed since.
* **Subscriptions** - the address(es) its billing emails reached (**Email Address Used**), plan, status (Active,
  Possibly stopped, Cancelled, Trial, Unclear), last charge, amount, cycle, next expected charge and cost per
  month. A line under the table adds up the monthly and yearly cost of the Active ones, per currency.

Click a column header to sort; click a row to see the rest of its detail under the table (for a subscription, the reason for its
status and the charges for each address). A table wider than the window gets a scroll bar along its bottom edge. **Minimum AI confidence** filters all
four tables and the export.

**Export:** *Browse...* to a folder (there is no default, so nothing is written until you choose), tick CSV /
Markdown, optionally add a date/time to file names or use a new dated sub-folder, then **Export now**. Files are
never overwritten. If you close with results you haven't exported (and write mode is off), the app asks first.

## Getting a double-click version

Most people should download a ready-made build from the
[Releases page](https://github.com/Threnark/Vestige/releases); the install steps for Windows, macOS and Linux are in
the [main README](README.md#installation). Build your own if you have changed the code, or if there is no release
for your system (for example an Intel Mac).

### Building it yourself

PyInstaller bundles Python and the app into a folder, with the Vestige icon. Build **on the operating system you
want to run it on**; you can't build a Windows program from a Mac.

```bat
:: Windows - from the project folder, with your virtual environment ACTIVE
build_windows.bat
```
```bash
# macOS / Linux
./build_mac_linux.sh
```

Result: a **folder** (`dist\Vestige\` on Windows, `dist/Vestige/` on Linux) or an app bundle (`dist/Vestige.app` on
macOS). Zip or tar the whole thing to share it. It is deliberately a folder and not a single `.exe`: a single-file
build unpacks itself into a temporary directory on every launch, which doesn't fit an app that tries to leave
nothing behind. Write mode's `Vestige_DATA` folder is created next to the program: beside `Vestige.exe` on Windows,
beside the `Vestige` program on Linux, and in the folder that contains `Vestige.app` on macOS.

### If the build misbehaves
* **Check which Python is building.** The scripts print it. With a virtual environment active, `python -m
  PyInstaller` uses the environment's Python. The Windows `py` launcher can pick a *different* Python than your
  environment (which can make PyInstaller look for packages in the wrong place), so the scripts avoid it.
* **Very new Python versions** can lag behind PyInstaller and some libraries. If the build fails or `dist` ends up
  empty, try a current but established version (for example 3.11 or 3.12; the release builds use 3.11) in a fresh
  virtual environment.
* **Antivirus / SmartScreen.** Freshly built, unsigned programs are often flagged or quarantined, and a quarantined
  build can look like an empty `dist` folder. Add an exclusion for the project folder while building, or check your
  antivirus's quarantine list. "More info -> Run anyway" on first launch is normal for an unsigned app you built.
* **The icon doesn't show** in the title bar: the app sets it from `assets/`. Make sure the build scripts' `--add-data`
  line ran (the folder `dist\Vestige\_internal\assets` should exist).

### Publishing a release (maintainers)

Releases are built by GitHub Actions (`.github/workflows/build-all.yml`). Set `VERSION` in `app_info.py`, then push a
tag that matches it:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The workflow builds the Windows, macOS and Linux versions with PyInstaller and attaches `Vestige-Windows.zip`,
`Vestige-macOS.tar.gz` and `Vestige-Linux.tar.gz` to a new GitHub release. It warns if the tag and `VERSION` disagree.
Running the workflow by hand from the **Actions** tab builds the three files without publishing a release; they can be
downloaded from the run's summary page for 14 days.

## Troubleshooting
* *"Vestige could not verify the identity of the mail server"* - Vestige checks the mail server's certificate before it
  sends your password, and refused this server. Check the IMAP server name in Setup. On public or shared Wi-Fi,
  try another network. If it is your own server with a self-signed certificate, add its certificate authority to your
  computer's trusted certificates (there is deliberately no option to skip the check), or import a mail file instead.
* *The program won't open* (Windows "protected your PC", macOS "can't check for malicious software", Linux "GLIBC
  not found") - see the install steps for your system in the [main README](README.md#installation).
* *Text looks cut off, overlaps, or spacing looks wrong* - this can depend on your display scaling and fonts. Please
  [open an issue](https://github.com/Threnark/Vestige/issues) with a screenshot and your scaling (for example 150%).
  Layout values are in the `gui_*.py` files; look and feel is in `ui_theme.py`.
* *"The 'anthropic' package isn't installed"* - when running from source, `pip install anthropic` (the release
  downloads include it), or choose another provider / a local AI.
* *Local AI: "Couldn't connect"* - the server isn't running (open the Ollama app or run `ollama serve`), or the
  address/port is wrong. *"wasn't found"* - the model name doesn't exist on that server (`ollama list`, or *Find models*).
* *Batches keep failing with a local model* - lower *Senders per AI request* to 1, or use a more capable model.
* *"Login failed"* - use an **app password** (not your normal password) and make sure IMAP is enabled; or skip IMAP
  and import a mail file (.pst for Outlook/Hotmail, .mbox, or .eml).
* *A saved session from an older version* is upgraded when it opens, if it can be. If Vestige says it can't open
  one, delete it and scan again (it only held scan progress).
