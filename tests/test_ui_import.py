"""UI smoke tests. They only run where CustomTkinter AND a display are available; elsewhere they are skipped
(the rest of the suite needs neither)."""
import importlib.util
import re
import unittest

HAVE_CTK = importlib.util.find_spec("customtkinter") is not None


@unittest.skipUnless(HAVE_CTK, "customtkinter is not installed")
class ThemeConstants(unittest.TestCase):
    def test_palette_values_are_valid_hex_colours(self):
        import ui_theme
        for name in ("BG", "PANEL", "BUTTON", "BUTTON_HOVER", "SELECTED", "FIELD", "TABLE_BG", "TABLE_HEADER",
                     "SCROLL", "TEXT", "TEXT_MUTED", "ACCENT", "CHECK_BORDER"):
            self.assertRegex(getattr(ui_theme, name), r"^#[0-9a-fA-F]{6}$", name)
        self.assertEqual(set(ui_theme.PRIVACY_COLORS), {"local", "lan", "cloud", "remote"})

    def test_font_pick_prefers_the_first_installed_and_never_double_bolds(self):
        import ui_theme
        self.assertEqual(ui_theme._pick_family({"Segoe UI Semibold", "DejaVu Sans"}, ["Nope", "Segoe UI Semibold", "DejaVu Sans"]),
                         ("Segoe UI Semibold", True))
        self.assertEqual(ui_theme._pick_family({"DejaVu Sans"}, ["Nope", "DejaVu Sans"]), ("DejaVu Sans", False))
        self.assertEqual(ui_theme._pick_family(set(), ["Nope"]), ("TkDefaultFont", False))

    def test_all_icon_assets_exist(self):
        import os
        import ui_theme
        for name in ("vestige.ico", "vestige.icns", "vestige_icon.svg", "vestige_icon_256.png"):
            self.assertTrue(os.path.exists(ui_theme.asset_path(name)), name)


class NoOldBranding(unittest.TestCase):
    def test_project_files_use_the_new_name(self):
        import glob
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for path in glob.glob(os.path.join(root, "*.py")) + glob.glob(os.path.join(root, "*.bat")) + glob.glob(os.path.join(root, "*.sh")):
            text = open(path, encoding="utf-8").read()
            self.assertFalse(re.search(r"Email Account Auditor|EmailAccountAuditor", text), path)


if __name__ == "__main__":
    unittest.main()
