# Vestige desktop app guide

The desktop app is the privacy-first way to use Vestige: **nothing is saved unless you choose to** (see
[Privacy and data handling](README.md#privacy-and-data-handling) in the main README). To install it, see
[Installation](README.md#installation). Inside the app, **? Help** (or F1) opens help for the page you are on.

## Tour

### Setup
**Storage.** *Write Mode* is off by default: everything stays in memory and disappears when you close the app.
Turn it on to save scan progress so a crash or accidental close doesn't waste scanning time and AI cost. You get a
warning first, listing what will be saved (sensitive results; never passwords, keys or server addresses) and
where (`Vestige_DATA` next to the program). On close in write mode it asks whether to keep or delete the file;
next launch it offers to resume a saved session. Deleting is an ordinary delete, not a secure erase (the app says so).

**AI provider.** Choose **Cloud AI (API key)** or **Local AI (runs on this computer)**. Pick a provider, set the
server address and model (*Find models* lists what's available), and press *Test connection*. The coloured line
under the form always states where your data will go (green = stays on this computer). Switching to Local clears
any typed API key. Nothing here is ever written to disk.

**Email accounts.** Add as many as you like. An app password is only needed to fetch over IMAP; leave it blank to
use an account for **file import** only (.pst, .mbox or .eml; no credentials at all). Outlook / Hotmail / Live accounts can only be used this way, because Microsoft no longer accepts passwords over IMAP.

### Run Pipeline
The page has *Only mail since*, *Groups per AI request*, *Limit AI to N groups* (the cheap test-run option), the
tick-box list of accounts to fetch over IMAP (none ticked = all accounts that have an app password), a button for each
of steps 1-5 (step 1 is either **Fetch headers (IMAP)** or **Import mail file...**), **Run full pipeline**, **Stop**,
the status line and the live log. **Run full pipeline never exports.**

**The progress bar:**
* It shows the step ("Step 3 of 5: Fetching email bodies"), an overall percentage, and a detail line such as
  "412 of 1,380 emails".
* While a stage doesn't yet know how big its job is (for example a mail file that is still being indexed, or a
  mail server that hasn't said how many messages a folder holds), the bar shows a moving **"working..."**
  marker instead of a number, and switches to a real percentage as soon as the total is known.
* It never moves backwards, even if a later folder turns out to hold more mail than first thought; it simply
  pauses until the new total catches up.
* It is a **general guide, not an exact clock.** Each step of the full pipeline is given an estimated share of the
  total time (headers 25%, pre-filter 5%, bodies 15%, sign-up AI 40%, email-change AI 15%). Those weights are in
  `pipeline.py` (`STAGES`) if you want to tune them. Run a single stage on its own and the bar covers just that stage.
* If you press **Stop** it reports where it stopped and that everything finished so far is kept.

### Results
Two tables: **Sign-ups** (one row per service *and* address) and **Possible email changes**; switch between them
at the top. Click a column header to sort; double-click a row for the full detail. **Minimum confidence** filters
both tables and the export.

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
* *Batches keep failing with a local model* - lower *Groups per AI request* to 1, or use a more capable model.
* *"Login failed"* - use an **app password** (not your normal password) and make sure IMAP is enabled; or skip IMAP
  and import a mail file (.pst for Outlook/Hotmail, .mbox, or .eml).
* *A saved session from an older version* can't be opened; delete it and scan again (it only held scan progress).
