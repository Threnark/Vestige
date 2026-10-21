"""Tests for the About page of the Help window, the quiet footer at the bottom of the main window,
the drawn icons, and app_info.py (the one place the name, version, author, licence and links live).

What they protect:
  * the facts: name, version, copyright, licence name and every link are exactly what the author gave;
  * the tone: the support section stays gentle (no pressure words, the free option comes first);
  * the wiring: the footer link opens the About page, the support buttons open the right addresses,
    the footer is packed so it can't take space from the pages;
  * the icons really draw (they are made with Pillow, so they are checked as pictures).

Like the other interface tests they run against the stand-in for Tk (tests/fake_tk.py), so they can't
show how anything looks on a real screen."""
import ast
import importlib
import os
import re
import sys
import unittest
from unittest import mock

import app_info
import help_content as content
from tests import fake_tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from PIL import Image
    import ui_icons
except ImportError:                                  # Pillow is a requirement of Vestige, but don't fail on its absence here
    ui_icons = None


def source(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def about_text() -> str:
    topic = content.TOPICS["about"]
    parts = [topic.title, topic.summary]
    for block in topic.blocks:
        if isinstance(block, (content.Para, content.Section)):
            parts.append(block.text)
        elif isinstance(block, content.Identity):
            parts.extend([block.name, *block.lines])
        elif isinstance(block, content.SupportButtons):
            for item in block.items:
                parts.extend([item.label, item.note])
        elif isinstance(block, content.Link):
            parts.append(block.label)
    return " ".join(parts)


class AppInfo(unittest.TestCase):
    def test_the_facts_are_exactly_what_the_author_gave(self):
        self.assertEqual(app_info.APP_NAME, "Vestige")
        self.assertEqual(app_info.VERSION_LABEL, "v0.1.0")
        self.assertEqual(app_info.COPYRIGHT, "Copyright \u00a9 2026 THRENARK")
        self.assertEqual(app_info.LICENSE_FULL, "GNU General Public License v3.0 (GPLv3)")
        self.assertEqual(app_info.LICENSE_URL, "https://github.com/Threnark/Vestige/blob/main/LICENSE")
        self.assertEqual(app_info.KOFI_URL, "https://ko-fi.com/threnark")
        self.assertEqual(app_info.SPONSORS_URL, "https://github.com/sponsors/Threnark")
        self.assertEqual(app_info.REPO_URL, "https://github.com/Threnark/Vestige")

    def test_version_is_a_plain_three_part_number(self):
        self.assertRegex(app_info.VERSION, r"^\d+\.\d+\.\d+$")
        self.assertEqual(app_info.VERSION_LABEL, "v" + app_info.VERSION)

    def test_footer_text_has_name_version_holder_and_licence(self):
        for part in ("Vestige v0.1.0", "THRENARK", "2026", "GPLv3"):
            self.assertIn(part, app_info.FOOTER_TEXT)
        self.assertNotIn("\n", app_info.FOOTER_TEXT)                 # one quiet line

    def test_links_match_the_funding_file_if_it_is_present(self):
        path = os.path.join(ROOT, ".github", "FUNDING.yml")
        if not os.path.exists(path):
            self.skipTest("no .github/FUNDING.yml in this copy")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("ko_fi: threnark", text)
        self.assertIn("github: Threnark", text)


class AboutContent(unittest.TestCase):
    def blocks(self, kind):
        return [b for b in content.TOPICS["about"].blocks if isinstance(b, kind)]

    def test_it_is_inside_the_help_window_navigation(self):
        self.assertIn("about", content.TOPICS)
        self.assertIn("about", [tid for _group, ids in content.NAV for tid in ids])
        self.assertEqual(content.get_topic("about").nav, "About Vestige")

    def test_it_lists_name_version_and_copyright(self):
        (card,) = self.blocks(content.Identity)
        self.assertEqual(card.name, "Vestige")
        self.assertEqual(card.lines, ("Version v0.1.0", "Copyright \u00a9 2026 THRENARK"))

    def test_it_describes_the_application(self):
        text = about_text()
        for phrase in ("forgotten", "sign-up", "email address you used", "runs entirely on your own computer"):
            self.assertIn(phrase, text)

    def test_licence_notice_names_the_licence_and_links_to_it(self):
        text = about_text()
        self.assertIn("GNU General Public License v3.0 (GPLv3)", text)
        self.assertIn("no warranty", text)
        links = {b.url: b.label for b in self.blocks(content.Link)}
        self.assertIn(app_info.LICENSE_URL, links)

    def test_support_buttons_are_the_three_given_links(self):
        (group,) = self.blocks(content.SupportButtons)
        self.assertEqual({item.url for item in group.items},
                         {app_info.REPO_URL, app_info.KOFI_URL, app_info.SPONSORS_URL})
        self.assertEqual({item.icon for item in group.items}, {"star", "coffee", "heart"})
        for item in group.items:
            self.assertTrue(item.label and item.note)

    def test_the_free_way_to_help_comes_first_and_says_it_is_free(self):
        (group,) = self.blocks(content.SupportButtons)
        self.assertEqual(group.items[0].url, app_info.REPO_URL)
        self.assertIn("free", group.items[0].note.lower())

    def test_the_tone_is_gentle(self):
        """No pressure words, no exclamation marks; and it says plainly that nothing is expected."""
        text = about_text().lower()
        for word in ("must ", "should ", "please", "need to", "donate", "don't forget", "urgent", "only way", "!"):
            self.assertNotIn(word, text, f"pressure word {word!r} on the About page")
        self.assertIn("no pressure at all", text)
        self.assertIn("whether or not", text)
        self.assertIn("thank you", text)

    def test_every_address_is_https(self):
        for _topic, url in content.all_urls():
            self.assertTrue(url.startswith("https://"))


@unittest.skipIf(ui_icons is None, "Pillow is not installed")
class Icons(unittest.TestCase):
    def test_each_icon_draws_a_visible_shape_in_its_colour(self):
        for name, colour in (("star", "#e0b04b"), ("heart", "#e58fa8"), ("coffee", "#e0b98c")):
            picture = ui_icons.make(name, 44, colour)
            self.assertEqual(picture.size, (44, 44))
            self.assertEqual(picture.mode, "RGBA")
            alpha = picture.getchannel("A")
            solid = sum(alpha.histogram()[201:])                                  # pixels that are (nearly) fully opaque
            self.assertGreater(solid, 150, f"{name} is nearly empty")            # a real shape
            self.assertLess(solid, 44 * 44 * 0.8, f"{name} fills the whole square")
            self.assertEqual(alpha.getpixel((0, 0)), 0)                          # transparent corners
            wanted = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
            inside = {"star": (22, 24), "heart": (22, 20), "coffee": (17, 26)}[name]   # a point well inside each shape
            red, green, blue, opacity = picture.getpixel(inside)
            self.assertEqual(opacity, 255, f"{name} is not solid in the middle")
            self.assertEqual((red, green, blue), wanted, f"{name} is the wrong colour")

    def test_the_shapes_are_centred(self):
        for name in ui_icons.DRAWERS:
            box = ui_icons.make(name, 64, "#ffffff").getchannel("A").getbbox()
            left, top, right, bottom = box
            self.assertLess(abs(left - (64 - right)), 8, name)
            self.assertLess(abs(top - (64 - bottom)), 8, name)

    def test_unknown_icon_names_raise_key_error(self):
        with self.assertRaises(KeyError):
            ui_icons.make("no-such-icon", 22, "#fff")


@unittest.skipIf(ui_icons is None, "Pillow is not installed")
class FooterAndAboutWindow(unittest.TestCase):
    def setUp(self):
        manager = fake_tk.installed()
        self.env = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        sys.modules.pop("ui_footer", None)
        self.ui_footer = importlib.import_module("ui_footer")

    def labels(self, widget):
        return {w.kw["text"]: w for w in widget.walk() if isinstance(w.kw.get("text"), str)}

    def test_footer_shows_the_link_and_the_quiet_line(self):
        footer = self.ui_footer.Footer(self.env.root, on_support=lambda: None)
        shown = self.labels(footer)
        self.assertIn("Support the dev", shown)
        self.assertIn(app_info.FOOTER_TEXT, shown)
        self.assertEqual(shown[app_info.FOOTER_TEXT].kw["fg"], self.env.theme.FAINT)        # greyed out
        self.assertEqual(shown["Support the dev"].kw["cursor"], "hand2")

    def test_footer_text_is_small(self):
        footer = self.ui_footer.Footer(self.env.root, on_support=lambda: None)
        self.assertIs(footer.info.kw["font"], self.env.theme.F.tk_small)
        self.assertIs(footer.link.kw["font"], self.env.theme.F.tk_small_link)

    def test_footer_link_only_reacts_to_a_click(self):
        calls = []
        footer = self.ui_footer.Footer(self.env.root, on_support=lambda: calls.append("clicked"))
        self.assertEqual(calls, [])
        footer.link.bindings["<Button-1>"](None)
        self.assertEqual(calls, ["clicked"])
        footer.link.bindings["<Enter>"](None)                                   # hovering is only a colour change
        self.assertEqual(footer.link.kw["fg"], self.env.theme.TEXT)
        footer.link.bindings["<Leave>"](None)
        self.assertEqual(footer.link.kw["fg"], self.env.theme.LINK)
        self.assertEqual(calls, ["clicked"])

    def test_clicking_the_footer_link_opens_the_about_page(self):
        help_ui = self.env.help_ui
        footer = self.ui_footer.Footer(self.env.root, on_support=lambda: help_ui.open_help(self.env.root, "about"))
        footer.link.bindings["<Button-1>"](None)
        self.assertEqual(help_ui._window.topic_id, "about")

    def test_about_page_shows_everything(self):
        win = self.env.help_ui.open_help(self.env.root, "about")
        shown = " ".join(win.texts())
        self.assertIn("Version v0.1.0", shown)
        self.assertIn("Copyright \u00a9 2026 THRENARK", shown)
        self.assertIn("GNU General Public License v3.0 (GPLv3)", shown)
        self.assertEqual(win._nav_buttons["about"].kw["fg_color"], self.env.theme.SELECTED)

    def test_support_buttons_have_pictures_and_open_the_right_addresses(self):
        win = self.env.help_ui.open_help(self.env.root, "about")
        (group,) = [b for b in content.TOPICS["about"].blocks if isinstance(b, content.SupportButtons)]
        for item in group.items:
            button = next(w for w in win._page.walk() if w.kw.get("text") == item.label)
            self.assertIsNotNone(button.kw["image"], f"{item.label} has no illustration")
            self.assertEqual(button.kw["compound"], "left")
            with mock.patch.object(self.env.help_ui.webbrowser, "open") as opener:
                button.kw["command"]()
                opener.assert_called_once_with(item.url)
            self.assertIn(item.url, win.texts())                                  # the address is shown too
            self.assertIn(item.note, win.texts())

    def test_the_program_icon_and_the_drawn_icons_are_cached(self):
        theme = self.env.theme
        self.assertIs(theme.icon("heart", 22), theme.icon("heart", 22))
        self.assertIsNotNone(theme.app_icon(72))
        self.assertIsNone(theme.icon("no-such-icon", 22))                         # never raises

    def test_a_missing_picture_does_not_break_the_page(self):
        theme = self.env.theme
        with mock.patch.object(theme, "app_icon", return_value=None), \
                mock.patch.object(theme, "icon", return_value=None):
            win = self.env.help_ui.open_help(self.env.root, "about")
            win.show_topic("about")
        self.assertIn("Vestige", win.texts())


class MainWindowWiring(unittest.TestCase):
    """gui_app.py can't be built without a screen, so its source is read instead."""

    def test_footer_opens_the_about_page(self):
        tree = ast.parse(source("gui_app.py"))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "Footer" and getattr(node.func.value, "id", "") == "ui_footer":
                handler = next(k.value for k in node.keywords if k.arg == "on_support")
                call = handler.body
                self.assertEqual(call.func.attr, "open_help")
                self.assertEqual(call.args[-1].value, "about")
                found = True
        self.assertTrue(found, "gui_app.py does not create the footer")

    def test_footer_is_packed_at_the_bottom_before_the_pages(self):
        text = source("gui_app.py")
        footer_at = text.index("self.footer.pack(side=\"bottom\"")
        pages_at = text.index("self.host.pack(fill=\"both\", expand=True)")
        self.assertLess(footer_at, pages_at)            # packed first, so it keeps its strip and the pages take the rest


if __name__ == "__main__":
    unittest.main()
