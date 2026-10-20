"""Tests for the Help system: help_content.py (what it says) and ui_help.py (how it is shown).

Two groups:

  1. CONTENT tests. They check the help text against the real program, so the help can't drift
     away from it: every button name and field name quoted in the help must exist in the
     interface source, the numbers and names quoted must match the code's constants, every [?]
     button in the interface must point at a real topic, every link must be https, and no
     sentence may be so long that it is hard to read.

  2. WINDOW tests. They run ui_help.py against a stand-in for Tk (tests/fake_tk.py) so the logic
     can be checked on a machine with no screen: every topic renders, text wraps to the window
     width, links open the right address, the [?] buttons switch the open window instead of
     opening another. They can't show how it looks on a real screen.
"""
import ast
import os
import re
import unittest
from unittest import mock

import help_content as content
from models import DEFAULT_BATCH_CLOUD, DEFAULT_BATCH_LOCAL, KNOWN_IMAP_HOSTS
from tests import fake_tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


GUI_SOURCES = ("gui_app.py", "gui_setup.py", "gui_run.py", "gui_results.py")


def block_strings(topic):
    """Every piece of text in a topic, one string each."""
    out = [topic.title, topic.summary]
    for block in topic.blocks:
        if isinstance(block, (content.Para, content.Section)):
            out.append(block.text)
        elif isinstance(block, (content.Steps, content.Bullets)):
            out.extend(block.items)
        elif isinstance(block, content.Note):
            out.extend([block.title, block.text])
        elif isinstance(block, content.Link):
            out.append(block.label)
        elif isinstance(block, content.Settings):
            out.append(block.title)
            for name, value in block.rows:
                out.extend([name, value])
    return out


class ContentStructure(unittest.TestCase):
    REQUIRED = ["start", "gmail", "yahoo", "icloud", "microsoft", "other", "ai", "pipeline", "results", "privacy"]

    def test_every_requested_page_exists(self):
        self.assertEqual(sorted(content.TOPICS), sorted(self.REQUIRED))

    def test_each_topic_is_listed_once_in_the_navigation(self):
        listed = [tid for _group, ids in content.NAV for tid in ids]
        self.assertEqual(sorted(listed), sorted(content.TOPICS))
        self.assertEqual(len(listed), len(set(listed)))

    def test_topics_are_complete(self):
        for topic in content.TOPICS.values():
            self.assertTrue(topic.nav and topic.title and topic.summary, topic.id)
            self.assertGreaterEqual(len(topic.blocks), 5, topic.id)
            for text in block_strings(topic):
                self.assertTrue(text.strip(), f"empty text in {topic.id}")
                self.assertNotIn("TODO", text)
                self.assertNotIn("  ", text, f"double space in {topic.id}: {text[:50]}")

    def test_note_kinds_are_known(self):
        for topic in content.TOPICS.values():
            for block in topic.blocks:
                if isinstance(block, content.Note):
                    self.assertIn(block.kind, ("info", "tip", "warning"))

    def test_every_link_is_https_and_has_a_label(self):
        links = content.all_links()
        self.assertGreater(len(links), 15)
        for topic_id, block in links:
            self.assertTrue(block.url.startswith("https://"), block.url)
            self.assertNotIn(" ", block.url)
            self.assertTrue(block.label.strip())
        for topic in content.TOPICS.values():
            urls = [b.url for b in topic.blocks if isinstance(b, content.Link)]
            self.assertEqual(len(urls), len(set(urls)), f"duplicate link in {topic.id}")

    def test_provider_pages_end_with_the_checked_note(self):
        for topic_id in ("gmail", "yahoo", "icloud", "microsoft", "ai"):
            self.assertTrue(content.TOPICS[topic_id].checked, topic_id)
        self.assertIn(content.VERIFIED_ON, content.CHECKED_NOTE)

    def test_sentences_are_short_enough_to_read_easily(self):
        for topic in content.TOPICS.values():
            for text in block_strings(topic):
                for sentence in re.split(r"(?<=[.!?])\s+", text):
                    self.assertLessEqual(len(sentence.split()), 48, f"{topic.id}: {sentence[:70]}...")

    def test_unknown_topic_falls_back_to_getting_started(self):
        self.assertEqual(content.get_topic("nonsense").id, "start")


class ContentMatchesTheProgram(unittest.TestCase):
    def test_run_pipeline_buttons_exist_with_these_names(self):
        run = source("gui_run.py")
        for label in (content.BTN_FETCH, content.BTN_IMPORT, content.BTN_PREFILTER, content.BTN_BODIES,
                      content.BTN_CLASSIFY, content.BTN_CHANGES, content.BTN_FULL, content.BTN_STOP):
            self.assertIn(f'"{label}"', run.replace("\\u25b6  ", "").replace("\\u25a0  ", ""),
                          f"button {label!r} not found in gui_run.py")

    def test_interface_names_quoted_in_the_help_exist(self):
        """Every field, button and option the help mentions by name is really in the interface."""
        wanted = {
            "gui_run.py": ["Limit AI to N groups", "Full pipeline starts with an IMAP fetch", "Only mail since",
                           "Groups per AI request", "Select all", "Refresh status", "Choose file(s)...",
                           "Choose a folder of .eml..."],
            "gui_setup.py": ["Add account", "Test connection", "Find models", "API Key", "App password",
                             "Cloud AI (API key)", "Local AI (runs on this computer)", "AI provider",
                             "Write Mode", "Folders", "IMAP server", "IMAP port"],
            "gui_results.py": ["Export now", "Minimum confidence", "Refresh", "Browse...",
                               "Add date & time to file names", "Put files in a new dated sub-folder"],
        }
        for filename, phrases in wanted.items():
            text = source(filename)
            for phrase in phrases:
                self.assertIn(phrase, text, f"{phrase!r} not found in {filename}")

    def test_help_text_mentions_the_names_it_depends_on(self):
        everything = " ".join(" ".join(block_strings(t)) for t in content.TOPICS.values())
        for phrase in ("Limit AI to N groups", "Full pipeline starts with an IMAP fetch", "Only mail since",
                       "Groups per AI request", "Test connection", "Find models", "Export now",
                       "Minimum confidence"):
            self.assertIn(phrase, everything)

    def test_numbers_and_names_come_from_the_code(self):
        ai = " ".join(block_strings(content.AI))
        pipeline = " ".join(block_strings(content.PIPELINE))
        self.assertIn(f"{DEFAULT_BATCH_LOCAL} groups per request instead of {DEFAULT_BATCH_CLOUD}", ai)
        self.assertIn(f"{DEFAULT_BATCH_CLOUD} for cloud AI and {DEFAULT_BATCH_LOCAL} for local AI", pipeline)
        for name in content.CLOUD_PROVIDER_NAMES + content.LOCAL_PROVIDER_NAMES:
            self.assertIn(name, ai)
        self.assertIn("Ollama", ai)

    def test_imap_server_names_match_what_the_app_fills_in(self):
        for topic_id in ("gmail", "yahoo", "icloud"):
            self.assertIn(KNOWN_IMAP_HOSTS[topic_id], " ".join(block_strings(content.TOPICS[topic_id])))

    def test_results_help_lists_the_real_categories_and_methods(self):
        results = " ".join(block_strings(content.RESULTS))
        self.assertGreaterEqual(len(content.CATEGORIES), 10)
        for value in content.CATEGORIES + content.SIGNUP_METHODS:
            self.assertIn(value, results)

    def test_privacy_help_names_the_real_save_location(self):
        privacy = " ".join(block_strings(content.PRIVACY))
        self.assertIn(content.DATA_FOLDER, privacy)
        self.assertIn(str(content.BODY_CHARS_KEPT), privacy)
        self.assertIn(str(content.SIGNUP_CHARS), privacy)

    def test_outlook_page_says_what_the_program_really_does(self):
        text = " ".join(block_strings(content.MICROSOFT))
        for fact in ("16 September 2024", ".pst", "OAuth", "Thunderbird", "Sent Items", "Deleted Items", "Junk"):
            self.assertIn(fact, text)
        import module_a_pst
        self.assertIn("sent items", module_a_pst.SKIP_FOLDER_NAMES)
        self.assertNotIn("deleted items", module_a_pst.SKIP_FOLDER_NAMES)       # the help says they ARE read
        self.assertNotIn("junk email", module_a_pst.SKIP_FOLDER_NAMES)

    def test_account_pages_follow_the_provider(self):
        self.assertEqual(content.topic_for_account("gmail", "a@gmail.com"), "gmail")
        self.assertEqual(content.topic_for_account("yahoo"), "yahoo")
        self.assertEqual(content.topic_for_account("icloud"), "icloud")
        self.assertEqual(content.topic_for_account("outlook"), "microsoft")
        self.assertEqual(content.topic_for_account("other", "a@fastmail.com"), "other")
        self.assertEqual(content.topic_for_account("other", "me@hotmail.co.nz"), "microsoft")
        self.assertEqual(content.topic_for_account("gmail", "x@y.com", "outlook.office365.com"), "microsoft")
        self.assertEqual(content.topic_for_account("unknown-provider"), "other")


class HelpButtonsPointAtRealPages(unittest.TestCase):
    """Every [?] in the interface must open a page that exists."""

    @staticmethod
    def topics_used(filename: str):
        """The topic id given to each help_ui.button / help_ui.heading call (the last argument), plus
        every topic id returned by a _help_topic function. Function-valued topics are skipped."""
        tree = ast.parse(source(filename))
        used = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "help_ui" \
                    and node.func.attr in ("button", "heading") and node.args:
                last = node.args[-1]
                if isinstance(last, ast.Constant) and isinstance(last.value, str):
                    used.append(last.value)
        return used

    def test_every_help_button_uses_a_real_topic(self):
        total = 0
        for filename in GUI_SOURCES:
            for topic_id in self.topics_used(filename):
                total += 1
                self.assertIn(topic_id, content.TOPICS, f"{filename}: [?] points at {topic_id!r}")
        self.assertGreaterEqual(total, 12)                       # the [?] boxes are really there

    def test_the_main_window_help_button_pages_exist(self):
        tree = ast.parse(source("gui_app.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PAGE_HELP_TOPICS" for t in node.targets):
                mapping = ast.literal_eval(node.value)
                self.assertEqual(set(mapping), {"Setup", "Run Pipeline", "Results"})
                for topic_id in mapping.values():
                    self.assertIn(topic_id, content.TOPICS)
                return
        self.fail("PAGE_HELP_TOPICS not found in gui_app.py")

    def test_dialog_topic_functions_return_real_topics(self):
        # gui_run.ImportDialog._help_topic returns one of these two literals
        run = source("gui_run.py")
        for topic_id in re.findall(r'return "(\w+)" if chosen', run) + re.findall(r'else "(\w+)"\n', run):
            self.assertIn(topic_id, content.TOPICS)
        self.assertIn('"microsoft" if chosen is not None and chosen.is_microsoft else "pipeline"', run)


class HelpWindow(unittest.TestCase):
    def setUp(self):
        manager = fake_tk.installed()
        self.env = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)

    def window(self, topic="start", owner=None):
        return self.env.help_ui.open_help(owner or self.env.root, topic)

    def test_every_topic_renders_all_of_its_text(self):
        win = self.window()
        for topic in content.TOPICS.values():
            win.show_topic(topic.id)
            shown = " ".join(win.texts())
            self.assertIn(topic.title, win.title_label.kw["text"])
            self.assertEqual(win.summary_label.kw["text"], topic.summary)
            for text in block_strings(topic)[2:]:
                if isinstance(text, str):
                    self.assertIn(text, shown, f"{topic.id}: {text[:60]!r} was not shown")

    def test_checked_note_is_shown_on_provider_pages_only(self):
        win = self.window("gmail")
        self.assertIn(content.CHECKED_NOTE, win.texts())
        win.show_topic("privacy")
        self.assertNotIn(content.CHECKED_NOTE, win.texts())

    def test_unknown_topic_shows_getting_started(self):
        win = self.window("does-not-exist")
        self.assertEqual(win.topic_id, "start")

    def test_navigation_lists_every_topic_and_highlights_the_current_one(self):
        win = self.window("ai")
        self.assertEqual(set(win._nav_buttons), set(content.TOPICS))
        theme = self.env.theme
        self.assertEqual(win._nav_buttons["ai"].kw["fg_color"], theme.SELECTED)
        self.assertTrue(all(b.kw["fg_color"] == "transparent" for t, b in win._nav_buttons.items() if t != "ai"))
        win._nav_buttons["gmail"].kw["command"]()                       # clicking a nav button switches page
        self.assertEqual(win.topic_id, "gmail")
        self.assertEqual(win._nav_buttons["gmail"].kw["fg_color"], theme.SELECTED)
        self.assertEqual(win._nav_buttons["ai"].kw["fg_color"], "transparent")

    def test_old_page_is_removed_when_switching(self):
        win = self.window("gmail")
        first_page = win._page
        win.show_topic("yahoo")
        self.assertTrue(first_page.destroyed)
        self.assertNotIn("Gmail (@gmail.com)", " ".join(win.texts()))

    def test_text_wraps_to_the_window_width(self):
        win = self.window("start")
        win.fake_width = 1200
        win._apply_wrap()
        wide = [label.kw["wraplength"] for label, _ in win._wrapped]
        win.fake_width = 800
        win._apply_wrap()
        narrow = [label.kw["wraplength"] for label, _ in win._wrapped]
        self.assertTrue(all(n < w for n, w in zip(narrow, wide)))
        self.assertTrue(all(n >= 220 for n in narrow))                  # never squeezed to nothing

    def test_wrap_leaves_room_for_the_indent(self):
        win = self.window("start")
        self.assertLess(win._wrap_for(52), win._wrap_for(0))

    def test_wrap_scales_with_the_display_scale(self):
        win = self.window("start")
        win.fake_width = 1500
        base = win._wrap_for(0)
        with mock.patch.object(self.env.theme.F, "scale", 1.5):
            self.assertLess(win._wrap_for(0), base)                      # more of the window is chrome at 150%

    def test_resize_events_are_debounced_and_children_are_ignored(self):
        win = self.window("start")
        win._on_configure(mock.Mock(widget=object()))                    # a child's event: ignored
        self.assertIsNone(win._resize_job)
        win._on_configure(mock.Mock(widget=win))
        self.assertIsNotNone(win._resize_job)

    def test_links_open_the_browser_and_only_for_https(self):
        win = self.window("gmail")
        link = next(b for b in content.TOPICS["gmail"].blocks if isinstance(b, content.Link))
        label = next(w for w in win._page.walk() if w.kw.get("text") == link.label and w.kw.get("cursor") == "hand2")
        with mock.patch.object(self.env.help_ui.webbrowser, "open") as opener:
            label.bindings["<Button-1>"](None)
            opener.assert_called_once_with(link.url)
            opener.reset_mock()
            win._open("http://insecure.example.com")
            win._open("javascript:alert(1)")
            opener.assert_not_called()

    def test_a_missing_browser_falls_back_to_the_clipboard(self):
        win = self.window("gmail")
        with mock.patch.object(self.env.help_ui.webbrowser, "open", side_effect=RuntimeError("no browser")):
            win._open("https://example.com/x")
        self.assertEqual(fake_tk.FakeToplevel.clipboard, "https://example.com/x")

    def test_every_link_shows_its_address_and_has_a_copy_button(self):
        win = self.window("microsoft")
        links = [b for b in content.TOPICS["microsoft"].blocks if isinstance(b, content.Link)]
        shown = win.texts()
        for block in links:
            self.assertIn(block.url, shown)
        buttons = [w for w in win._page.walk() if w.kw.get("text") == "Copy link"]
        self.assertEqual(len(buttons), len(links))
        buttons[0].kw["command"]()
        self.assertEqual(fake_tk.FakeToplevel.clipboard, links[0].url)
        self.assertEqual(buttons[0].kw["text"], "Copied")

    def test_only_one_help_window_exists(self):
        first = self.window("gmail")
        second = self.window("yahoo")
        self.assertIs(first, second)
        self.assertEqual(first.topic_id, "yahoo")

    def test_a_different_owner_gets_a_fresh_window_and_the_old_one_closes(self):
        env = self.env
        dialog = fake_tk.FakeToplevel(env.root)                          # e.g. the modal Add account window
        first = self.window("start")
        second = self.window("gmail", owner=dialog)
        self.assertIsNot(first, second)
        self.assertTrue(first.destroyed)
        self.assertIs(second.owner, dialog)
        self.assertIs(env.help_ui._window, second)

    def test_closing_forgets_the_window(self):
        win = self.window("start")
        win.close()
        self.assertTrue(win.destroyed)
        self.assertIsNone(self.env.help_ui._window)
        again = self.window("ai")
        self.assertIsNot(again, win)

    def test_a_question_mark_button_opens_its_page(self):
        parent = fake_tk.FakeWidget(self.env.root)
        box = self.env.help_ui.button(parent, "privacy")
        box.kw["command"]()
        self.assertEqual(self.env.help_ui._window.topic_id, "privacy")

    def test_a_question_mark_button_can_choose_its_page_when_clicked(self):
        parent = fake_tk.FakeWidget(self.env.root)
        choice = {"topic": "gmail"}
        box = self.env.help_ui.button(parent, lambda: choice["topic"])
        choice["topic"] = "icloud"                                       # the user changed the provider before clicking
        box.kw["command"]()
        self.assertEqual(self.env.help_ui._window.topic_id, "icloud")

    def test_heading_with_a_question_mark_has_both(self):
        parent = fake_tk.FakeWidget(self.env.root)
        row = self.env.help_ui.heading(parent, "Storage", "privacy")
        self.assertIn("Storage", row.texts())
        self.assertIn("?", row.texts())

    def test_window_fits_a_small_screen(self):
        win = self.window("start")
        width, height = (int(n) for n in win.kw["geometry"].split("x"))
        self.assertLessEqual(width, win.MAX_SIZE[0])
        self.assertGreaterEqual(width, win.MIN_SIZE[0])
        self.assertLessEqual(height, win.MAX_SIZE[1])

    def test_scroll_reset_never_raises(self):
        win = self.window("start")
        win._scroll_to_top()                                             # the fake has no inner canvas: must not raise


if __name__ == "__main__":
    unittest.main()
