# Security Policy

Vestige reads your email to find the accounts you have signed up for, so security and privacy are
central to it. Please report problems responsibly.

## Reporting a vulnerability

**Please don't report security problems in a public issue.**

Use GitHub's private reporting instead: open the repository's **Security** tab and choose
**Report a vulnerability** ([direct link](https://github.com/Threnark/Vestige/security/advisories/new)).
Please include what you found, the steps to reproduce it, and the Vestige version (shown in the app's footer).

If you can't use that page, open a normal issue that says only that you have a security report, with no
details, and a private way to continue will be arranged.

Vestige is a small project, so replies are best effort. Reports are taken seriously, and you will be
credited in the fix unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes. Please update before reporting.

## What Vestige does to protect you

* **Verified mail connections.** IMAP connections use TLS, and the server's certificate and host name are
  verified before your password is sent. A server that fails the check is refused, and there is no setting
  to switch the check off.
* **Read-only access to your mail.** Mailboxes are opened read-only, and import files are never modified.
* **Credentials stay in memory.** Vestige never writes app passwords or API keys to disk. (The command-line
  tool reads them from environment variables or a `.env` file that you create.) Cloud AI providers must use
  HTTPS. For OpenAI-compatible providers, the API key is not forwarded if the server redirects elsewhere, and
  it is removed from error messages.
* **Nothing is saved unless you choose.** Files are written only if you turn on write mode or export. On
  macOS and Linux the write-mode database, and the `Vestige_DATA` folder when Vestige creates it, are readable
  only by your own user account.
* **No other network traffic.** The only connections are to your mail server and to the AI you choose. There
  is no telemetry, analytics or update check.
* **Safe exports.** Spreadsheet formula injection is neutralised, because service names come from email content
  that a sender controls.

## Known limitations

These are inherent to what the tool does, or not yet addressed. They are listed so you can decide
what is acceptable for you.

* **Saved and exported files are not encrypted.** Write-mode databases and exports are plain files (on
  Windows they follow the folder's normal permissions). Deleting a file does not securely erase it. Keep
  them on an encrypted drive, and avoid cloud-synced folders.
* **A cloud AI receives excerpts of your email** (the sender, date, subject, and the first part of the body). Use a
  local AI if nothing should leave your computer. See the Privacy section of the [README](README.md).
* **A local AI is contacted over plain HTTP.** That is fine on your own computer, and acceptable on a network
  you trust. But if you enter an address that is neither this computer nor your local network, email excerpts
  would travel unencrypted across the internet. The Setup page shows a warning in that case. Use an `https://`
  address for any server that is not on your own network.
* **Email content is untrusted.** A crafted email can try to mislead the AI's classification, which can
  cause wrong or missing results. The AI is not given any tools or access, and its output is validated
  before it is used.
* **`.pst` files are parsed by a native library (libpff).** A malformed file could crash it or, in the worst case,
  exploit a bug in it. Only import files you trust, such as your own exports.
* **Release downloads are not code-signed**, so Windows and macOS show a warning on first launch. Check the
  download against the `SHA256SUMS` file published with each release from 0.2.0 onwards (see "Verifying your download" in the
  [README](README.md#verifying-your-download)), or build from source.
* **Dependencies are not version-pinned.** Release builds use the newest compatible versions of their
  dependencies at build time.
* **Servers with self-signed certificates are refused.** That is deliberate, and it includes some local mail
  bridges (such as Proton Mail Bridge). To use such a server, add its certificate authority to your computer's
  trusted certificates, or export your mail and import the file (`.mbox`, `.eml` or `.pst`) instead.
