"""
app_info.py
-----------
Who Vestige is and who made it: the name, version, copyright holder, licence and the
public links. Plain data with no GUI code, so any part of the program (and the tests) can
use it and the same facts are never typed in two places.

WHERE IT IS USED
  * The "About Vestige" page of the Help window (help_content.py -> ui_help.py).
  * The small line at the bottom left of the main window (ui_footer.py).

TO RELEASE A NEW VERSION: change VERSION below. Everything that shows the version follows.
(The version of the PyInstaller build and of any GitHub release are set separately, by you.)
"""

APP_NAME = "Vestige"
VERSION = "0.2.0"
VERSION_LABEL = f"v{VERSION}"                    # how it is written on screen: v0.1.0

AUTHOR = "THRENARK"                              # the copyright holder
COPYRIGHT_YEAR = 2026
COPYRIGHT = f"Copyright \u00a9 {COPYRIGHT_YEAR} {AUTHOR}"

LICENSE_NAME = "GNU General Public License v3.0"
LICENSE_SHORT = "GPLv3"
LICENSE_FULL = f"{LICENSE_NAME} ({LICENSE_SHORT})"

# Public links. The first three are the author's; they match .github/FUNDING.yml.
REPO_URL = "https://github.com/Threnark/Vestige"
LICENSE_URL = "https://github.com/Threnark/Vestige/blob/main/LICENSE"
KOFI_URL = "https://ko-fi.com/threnark"
SPONSORS_URL = "https://github.com/sponsors/Threnark"

# The quiet line at the bottom of the main window, e.g. "Vestige v0.1.0  ·  Copyright © 2026 THRENARK  ·  GPLv3"
FOOTER_TEXT = f"{APP_NAME} {VERSION_LABEL}  \u00b7  {COPYRIGHT}  \u00b7  {LICENSE_SHORT}"
FOOTER_LINK_TEXT = "Support the dev"
