"""Tests for the small fixes of batch 8 of the review:

  * file names: Windows device names (con, nul, aux, COM1...) and over-long names are made safe;
  * the command line: a command that only reads no longer creates an empty database file, and the commands
    that never log in (import-mail, find-changes) no longer demand password environment variables;
  * a bare yen/yuan sign is kept as printed instead of being assumed to be yen;
  * no depth promises more text than is stored, and the Help says what Thorough does for email changes;
  * the build files: PyInstaller is a build requirement only, and the release workflow still installs it.
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import billing_math
import db
import help_content
import main
import module_a_imap
import paths
import pipeline
import run_options

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def quietly(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class SafeFilenames(unittest.TestCase):
    def test_windows_device_names_are_not_used_as_they_are(self):
        for name in ("con", "CON", "Nul", "aux", "prn", "COM1", "com9", "LPT1", "lpt9", "com\u00b9", "LPT\u00b2",
                     "con.txt", "NUL.backup", "aux.tar.gz"):
            with self.subTest(name=name):
                cleaned = paths.safe_filename(name)
                self.assertEqual(cleaned, "_" + name)
                self.assertFalse(re.match(r"(?i)^(con|prn|aux|nul|com\d|lpt\d)(\.|$)", cleaned))

    def test_names_that_only_look_similar_are_left_alone(self):
        for name in ("console", "connect", "com10", "compass", "aux2", "lpt", "com", "null", "accounts", "my con list"):
            with self.subTest(name=name):
                self.assertEqual(paths.safe_filename(name), name)

    def test_the_old_rules_still_apply(self):
        self.assertEqual(paths.safe_filename("my:/list*?"), "my__list__")
        self.assertEqual(paths.safe_filename("  report. "), "report")
        self.assertEqual(paths.safe_filename("..."), "signups")
        self.assertEqual(paths.safe_filename(""), "signups")
        self.assertEqual(paths.safe_filename(None), "signups")
        self.assertEqual(paths.safe_filename("", fallback="x"), "x")
        self.assertEqual(paths.safe_filename("Résumé de comptes"), "Résumé de comptes")

    def test_a_very_long_name_is_shortened(self):
        cleaned = paths.safe_filename("x" * 300)
        self.assertEqual(len(cleaned), paths.MAX_FILENAME_CHARS)
        self.assertEqual(paths.safe_filename("a" * 99 + " " + "b" * 50), "a" * 99)           # no trailing space left behind

    def test_a_name_that_becomes_a_device_name_once_cut_to_length_is_still_made_safe(self):
        """"con" followed by 150 spaces and more: cut to 100 characters and trimmed, what is left is exactly "con"."""
        self.assertEqual(paths.safe_filename("con" + " " * 150 + "x"), "_con")


class ExportedFileNames(unittest.TestCase):
    """An export of a name like "con" used to try to create con.csv, which Windows cannot do."""

    def setUp(self):
        from tests.test_pipeline import ExportRules
        with contextlib.redirect_stdout(io.StringIO()):
            ExportRules.setUp(self)                  # borrow its fixture: a mailbox taken through every stage

    def export(self, **kwargs):
        import module_d_export
        options = module_d_export.ExportOptions(folder=self.out, formats=("csv",), add_timestamp=False,
                                                use_subfolder=False, **kwargs)
        return module_d_export.export_files(self.conn, options)

    def test_a_device_name_is_not_used_as_the_file_name(self):
        names = sorted(os.path.basename(f) for f in self.export(base_name="con").files)
        self.assertEqual(names, ["_con.csv", "_con_possible_email_changes.csv"])
        for name in names:
            self.assertTrue(os.path.exists(os.path.join(self.out, name)))

    def test_an_ordinary_name_is_unchanged(self):
        names = sorted(os.path.basename(f) for f in self.export(base_name="my accounts").files)
        self.assertEqual(names, ["my accounts.csv", "my accounts_possible_email_changes.csv"])

    def test_a_very_long_name_still_makes_files_with_sensible_names(self):
        for file in self.export(base_name="x" * 300).files:
            self.assertLess(len(os.path.basename(file)), 140)


class CommandLineDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "accounts.db")

    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["main.py", "--db", self.path, *argv]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            main.main()
        return out.getvalue()

    def test_commands_that_only_read_do_not_create_a_database(self):
        for argv in (["status"], ["prefilter-report"], ["export", "--out-dir", self.tmp]):
            with self.subTest(command=argv[0]):
                with self.assertRaises(SystemExit) as caught:
                    self.run_main(*argv)
                self.assertIn("There is no database", str(caught.exception))
                self.assertIn(argv[0], str(caught.exception))
                self.assertFalse(os.path.exists(self.path), "an empty database file must not be left behind")

    def test_neither_do_the_steps_that_work_on_existing_data(self):
        for command in ("prefilter", "fetch-bodies", "classify", "find-changes", "check-closed", "find-subscriptions"):
            with self.subTest(command=command):
                self.assertIn("There is no database", main.missing_database_message(command, self.path))

    def test_the_commands_that_bring_mail_in_may_create_it(self):
        for command in ("fetch", "import-mail", "import-mbox", "all"):
            self.assertIsNone(main.missing_database_message(command, self.path), command)

    def test_every_command_is_decided_one_way_or_the_other(self):
        parser = main.build_parser()
        commands = next(a for a in parser._actions if a.dest == "command").choices
        for command in commands:
            main.missing_database_message(command, self.path)               # never raises, for any command that exists
        self.assertTrue(set(main.CREATES_DATABASE) <= set(commands))

    def test_an_existing_database_works_as_before(self):
        db.open_database(self.path).close()
        output = self.run_main("status")
        self.assertIn("total_emails", output)
        self.assertIn("keeps its database as a file", output)

    def test_the_message_tells_you_what_to_do_and_where_it_looked(self):
        message = main.missing_database_message("status", self.path)
        self.assertIn(os.path.abspath(self.path), message)
        self.assertIn("'fetch' or 'import-mail'", message)


@unittest.skipUnless(__import__("importlib").util.find_spec("yaml") and __import__("importlib").util.find_spec("dotenv"),
                     "PyYAML and python-dotenv are command-line extras")
class CommandsThatNeverLogIn(unittest.TestCase):
    ENV_VAR = "VESTIGE_TEST_PASSWORD_THAT_IS_NOT_SET"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, "accounts.yaml")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(f"accounts:\n  - name: gmail\n    provider: gmail\n    email: me@gmail.com\n    password_env: {self.ENV_VAR}\n")
        os.environ.pop(self.ENV_VAR, None)
        self.args = main.build_parser().parse_args(["--config", self.config, "find-changes"])

    def test_logging_in_commands_still_insist_on_the_password(self):
        with self.assertRaises(SystemExit) as caught:
            main.load_accounts(self.args)
        self.assertIn(self.ENV_VAR, str(caught.exception))

    def test_loading_without_passwords_gives_the_accounts_with_none(self):
        [account] = main.load_accounts(self.args, need_passwords=False)
        self.assertEqual((account.name, account.email, account.password), ("gmail", "me@gmail.com", ""))
        self.assertFalse(account.has_password)

    def test_find_changes_does_not_need_the_password(self):
        with mock.patch.object(main, "make_provider", return_value=object()), \
                mock.patch.object(pipeline, "stage_detect_changes") as stage:
            main.cmd_find_changes(self.args, db.open_database(None))
        [(_conn, _provider, accounts, *_rest)] = [c.args for c in stage.call_args_list]
        self.assertEqual([a.email for a in accounts], ["me@gmail.com"])

    def test_import_mail_does_not_need_the_password(self):
        args = main.build_parser().parse_args(["--config", self.config, "import-mail", "--account", "gmail", "--file", "x.mbox"])
        with mock.patch.object(pipeline, "stage_import_mail") as stage:
            main.cmd_import_mail(args, db.open_database(None))
        self.assertEqual(stage.call_args.args[1].name, "gmail")

    def test_fetching_still_needs_the_password(self):
        args = main.build_parser().parse_args(["--config", self.config, "fetch"])
        with self.assertRaises(SystemExit) as caught:
            main.cmd_fetch(args, db.open_database(None))
        self.assertIn(self.ENV_VAR, str(caught.exception))

    def test_a_password_that_is_set_is_still_used_when_asked_for(self):
        os.environ[self.ENV_VAR] = "app-password"
        self.addCleanup(os.environ.pop, self.ENV_VAR, None)
        [account] = main.load_accounts(self.args)
        self.assertEqual(account.password, "app-password")


class YenSign(unittest.TestCase):
    def currencies(self, text):
        return [(a["currency"], a["value"]) for a in billing_math.extract_amounts(text)]

    def test_a_bare_yen_sign_is_kept_as_printed(self):
        """It is the sign for the yen AND the yuan, so an email that only says it doesn't say which."""
        self.assertEqual(self.currencies("Total: \u00a51,500"), [("\u00a5", 1500.0)])
        self.assertEqual(self.currencies("Total: 1,500 \u00a5"), [("\u00a5", 1500.0)])

    def test_an_explicit_code_is_used_as_written(self):
        self.assertEqual(self.currencies("Charged JPY 1,500"), [("JPY", 1500.0)])
        self.assertEqual(self.currencies("Charged CNY 88.00"), [("CNY", 88.0)])

    def test_the_other_symbols_are_unchanged(self):
        self.assertEqual(self.currencies("\u00a39.99 \u20ac12,50 $4.00 NZ$5.00 \u20b9300"),
                         [("GBP", 9.99), ("EUR", 12.5), ("$", 4.0), ("NZD", 5.0), ("INR", 300.0)])

    def test_money_is_written_like_the_dollar_sign(self):
        self.assertEqual(billing_math.money(1500, "\u00a5"), "\u00a51,500.00")
        self.assertEqual(billing_math.money(9.99, "$"), "$9.99")
        self.assertEqual(billing_math.money(1500, "JPY"), "JPY 1,500.00")
        self.assertEqual(billing_math.money(None, "\u00a5"), "")

    def test_yen_and_yuan_are_not_added_together(self):
        """Amounts are added up per currency exactly as printed, so an ambiguous sign stays in its own total."""
        totals, skipped = billing_math.add_totals([(100.0, "\u00a5", 30.4375), (50.0, "\u00a5", 30.4375),
                                                   (88.0, "CNY", 30.4375), (1500.0, "JPY", 30.4375)])
        self.assertEqual(skipped, 0)
        self.assertEqual(set(totals), {"\u00a5", "CNY", "JPY"})
        self.assertAlmostEqual(totals["\u00a5"][0], 150.0, places=1)


class DepthTextLimits(unittest.TestCase):
    def test_no_depth_promises_more_text_than_is_kept(self):
        for depth in run_options.DEPTHS.values():
            for field in ("signup_chars", "change_chars", "closure_chars", "billing_chars"):
                with self.subTest(depth=depth.name, field=field):
                    self.assertLessEqual(getattr(depth, field), module_a_imap.BODY_SNIPPET_CHARS,
                                         f"{depth.name} asks for more characters than are ever stored")

    def test_deeper_never_means_less(self):
        q, b, t = run_options.QUICK, run_options.BALANCED, run_options.THOROUGH
        for field in ("signup_samples", "signup_chars", "change_chars", "closure_samples", "closure_chars",
                      "billing_samples", "billing_chars", "processor_receipts"):
            self.assertLessEqual(getattr(q, field), getattr(b, field), field)
            self.assertLessEqual(getattr(b, field), getattr(t, field), field)

    def test_email_changes_read_the_same_at_balanced_and_thorough_and_the_help_says_so(self):
        self.assertEqual(run_options.BALANCED.change_chars, run_options.THOROUGH.change_chars)
        text = " ".join(str(block) for topic in help_content.TOPICS.values() for block in topic.blocks)
        self.assertIn(f"{run_options.BALANCED.change_chars} at Balanced and at Thorough", text)
        self.assertNotIn(f"{run_options.QUICK.change_chars}, {run_options.BALANCED.change_chars} or {run_options.THOROUGH.change_chars} characters", text)


class BuildFiles(unittest.TestCase):
    def read(self, *parts) -> str:
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
            return handle.read().replace("\r\n", "\n")

    def requirement_names(self, filename) -> list:
        return [re.split(r"[<>=!~ ;\[]", line.split("#")[0].strip())[0].lower()
                for line in self.read(filename).splitlines() if line.split("#")[0].strip()]

    def test_pyinstaller_is_a_build_requirement_not_a_runtime_one(self):
        self.assertNotIn("pyinstaller", self.requirement_names("requirements.txt"))
        self.assertEqual(self.requirement_names("requirements-build.txt"), ["pyinstaller"])

    def test_the_runtime_requirements_the_program_imports_are_still_listed(self):
        names = self.requirement_names("requirements.txt")
        for needed in ("customtkinter", "pillow", "anthropic", "certifi", "pyyaml", "python-dotenv"):
            self.assertIn(needed, names)

    def test_the_release_workflow_still_installs_pyinstaller_and_runs_the_tests(self):
        workflow = self.read(".github", "workflows", "build-all.yml")
        self.assertIn("-r requirements-build.txt", workflow)
        self.assertIn("python -m unittest tests.test_security", workflow)
        self.assertIn("python -m unittest discover -s tests -t .", workflow)
        self.assertLess(workflow.index("Run all the tests"), workflow.index("Build with PyInstaller"),
                        "the tests must run before anything is built")

    def test_the_local_build_scripts_install_their_own_tools(self):
        for script in ("build_windows.bat", "build_mac_linux.sh"):
            self.assertIn("pyinstaller", self.read(script).lower(), script)

    def test_the_stray_spec_file_is_gone_and_stays_ignored(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "Vestige.spec")))
        self.assertIn("*.spec", self.read(".gitignore"))

    def test_the_readme_points_to_the_build_requirements(self):
        self.assertIn("requirements-build.txt", self.read("README.md"))


if __name__ == "__main__":
    unittest.main()
