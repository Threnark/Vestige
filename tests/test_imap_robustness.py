"""Tests of how the IMAP code copes when the mail server misbehaves (batch 1 of the review fixes):

  * a NO answer is never mistaken for "this message was deleted" (it used to be, which stored an empty
    snippet for ever);
  * a batch the server refuses is split, so one bad message costs only itself;
  * a dropped connection is replaced (reconnect, re-open the folder) instead of being asked again;
  * a BAD answer is not retried;
  * the "only mail since" date is checked and tidied before anything connects.

No real server is used: `Server` below hands out stand-ins for imaplib.IMAP4_SSL whose answers each
test chooses. `time.sleep` is replaced, so nothing really waits.
"""
import contextlib
import imaplib
import io
import socket
import threading
import types
import unittest
from unittest import mock

import db
import main
import module_a_imap as imap_mod
from models import Account
from tests import fake_tk
from tests.test_gui_new import GuiCase

ACCT = Account("gm", "gmail", "me@gmail.com", "imap.gmail.com", password="x")
GOOD = (b"From: a@acme.com\r\nSubject: Welcome\r\nContent-Type: text/plain\r\n\r\n"
        b"Thanks for joining Acme. Your account is ready.")
NO_FETCH = ("NO", [b"Some messages could not be FETCHed (Failure)"])


class Connection:
    """Just enough of imaplib.IMAP4_SSL."""

    def __init__(self, server, number):
        self.server, self.number, self.folder, self.closed = server, number, None, False

    def select(self, folder, readonly=False):
        assert readonly, "a mailbox must only ever be opened read-only"
        reply = self.server.on_select(self, folder)
        if reply[0] == "OK":
            self.folder = folder
        return reply

    def uid(self, command, *args):
        self.server.calls.append((self.number, command, args))
        return self.server.on_uid(self, command, *args)

    def logout(self):
        self.closed = True

    def shutdown(self):
        self.closed = True


class Server:
    """Hands out connections and records every command. A test sets `on_uid` / `on_select`."""

    def __init__(self):
        self.connections, self.calls = [], []
        self.on_uid = lambda conn, command, *args: ("OK", [])
        self.on_select = lambda conn, folder: ("OK", [b"1"])

    def connect(self, account):
        conn = Connection(self, len(self.connections) + 1)
        self.connections.append(conn)
        return conn

    def fetches(self) -> list:
        return [args[0] for _n, command, args in self.calls if command == "fetch"]


def body_reply(uid_set, have):
    """An OK answer to a body fetch that contains only the messages in `have` ({uid: raw message})."""
    out = []
    for uid in uid_set.split(","):
        if uid in have:
            out.append((f"1 (UID {uid} BODY[]<0> {{99}}".encode(), have[uid]))
            out.append(b")")
    return "OK", out


def header_reply(uid_set):
    out = []
    for uid in uid_set.split(","):
        raw = (f"From: Acme <a@acme.com>\r\nTo: me@gmail.com\r\nSubject: Hello {uid}\r\n"
               f"Date: Mon, 05 Jun 2017 09:00:00 +0000\r\nMessage-ID: <{uid}@acme>\r\n\r\n").encode()
        out.append((f"{uid} (UID {uid} BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] {{{len(raw)}}}".encode(), raw))
        out.append(b")")
    return "OK", out


class ImapCase(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.conn = db.open_database(None)
        self.sleeps = []
        for patcher in (mock.patch.object(imap_mod, "connect", self.server.connect),
                        mock.patch.object(imap_mod.time, "sleep", self.sleeps.append)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def quietly(self, fn, *args, **kwargs):
        """Run fn, returning (its result, everything it printed)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = fn(*args, **kwargs)
        return result, out.getvalue()

    def add_candidates(self, uids, folder="INBOX"):
        for uid in uids:
            db.insert_email(self.conn, imap_mod.build_header_row(
                ACCT, folder, uid, "Acme <a@acme.com>", "me@gmail.com", "Welcome to Acme",
                "Mon, 05 Jun 2017 09:00:00 +0000", f"<{uid}@acme>", None))
        self.conn.execute("UPDATE emails SET prefilter_label='candidate'")
        self.conn.commit()

    def fetch_bodies(self):
        rows = db.emails_missing_body(self.conn)
        return self.quietly(imap_mod.fetch_body_snippets, ACCT, self.conn, rows)

    def snippets(self) -> dict:
        return {r["uid"]: (r["body_snippet"], r["body_fetched"])
                for r in self.conn.execute("SELECT uid, body_snippet, body_fetched FROM emails")}


class RefusedBodyFetches(ImapCase):
    """A NO answer used to be read as "the message was deleted", so the email was stored with an empty
    snippet and never fetched again. Now it is left alone to be tried again."""

    def test_a_refusal_that_clears_up_on_the_second_try_loses_nothing(self):
        self.add_candidates(["10", "11", "12"])
        answers = iter([NO_FETCH])
        self.server.on_uid = lambda c, cmd, uid_set, items: next(answers, None) or body_reply(
            uid_set, {"10": GOOD, "11": GOOD, "12": GOOD})
        stored, _ = self.fetch_bodies()
        self.assertEqual(stored, 3)
        for uid, (text, fetched) in self.snippets().items():
            self.assertIn("account is ready", text, uid)
            self.assertEqual(fetched, 1)
        self.assertEqual(self.sleeps, [2])                       # it waited once, then asked again

    def test_a_refusal_that_never_clears_leaves_every_email_waiting(self):
        self.add_candidates(["10", "11", "12"])
        self.server.on_uid = lambda c, cmd, uid_set, items: NO_FETCH
        stored, output = self.fetch_bodies()
        self.assertEqual(stored, 0)
        self.assertEqual(len(db.emails_missing_body(self.conn)), 3)           # still pending, NOT marked done
        for uid, (_text, fetched) in self.snippets().items():
            self.assertEqual(fetched, 0, f"uid {uid} was marked done although the server never answered")
        self.assertIn("tried again", output)

    def test_one_bad_message_costs_only_itself(self):
        uids = [str(n) for n in range(10, 18)]
        self.add_candidates(uids)
        have = {uid: GOOD for uid in uids}
        self.server.on_uid = lambda c, cmd, uid_set, items: (
            NO_FETCH if "13" in uid_set.split(",") else body_reply(uid_set, have))
        stored, _ = self.fetch_bodies()
        self.assertEqual(stored, 7)
        got = self.snippets()
        self.assertEqual(got["13"], (None, 0))                                # the bad one waits for next time
        for uid in uids:
            if uid != "13":
                self.assertIn("account is ready", got[uid][0], uid)
        self.assertLess(len(self.server.fetches()), 30)                       # splitting is bounded

    def test_an_OK_answer_that_leaves_a_message_out_still_means_deleted(self):
        self.add_candidates(["10", "11"])
        self.server.on_uid = lambda c, cmd, uid_set, items: body_reply(uid_set, {"10": GOOD})
        self.fetch_bodies()
        got = self.snippets()
        self.assertIn("account is ready", got["10"][0])
        self.assertEqual(got["11"], ("", 1))                                  # gone from the server: not asked again

    def test_a_folder_that_cannot_be_opened_leaves_its_emails_waiting(self):
        self.add_candidates(["10", "11"])
        self.server.on_select = lambda c, folder: ("NO", [b"no such folder"])
        stored, output = self.fetch_bodies()
        self.assertEqual(stored, 0)
        self.assertEqual(len(db.emails_missing_body(self.conn)), 2)
        self.assertEqual(self.server.fetches(), [])
        self.assertIn("cannot select", output)

    def test_a_bad_answer_is_not_retried(self):
        self.add_candidates(["10", "11", "12"])

        def reject(conn, cmd, uid_set, items):
            raise imaplib.IMAP4.error("UID command error: BAD Could not parse command")
        self.server.on_uid = reject
        stored, _ = self.fetch_bodies()
        self.assertEqual(stored, 0)
        self.assertEqual(self.sleeps, [], "a BAD answer cannot be cured by waiting")
        self.assertEqual(len(db.emails_missing_body(self.conn)), 3)


class DroppedConnections(ImapCase):
    def test_the_scan_reconnects_reopens_the_folder_and_carries_on(self):
        self.add_candidates(["10", "11"])
        dropped = []

        def flaky(conn, cmd, uid_set, items):
            if not dropped:
                dropped.append(True)
                raise imaplib.IMAP4.abort("socket error: EOF")
            return body_reply(uid_set, {"10": GOOD, "11": GOOD})
        self.server.on_uid = flaky
        stored, output = self.fetch_bodies()
        self.assertEqual(stored, 2)
        self.assertEqual(len(self.server.connections), 2, "a NEW connection must be made")
        self.assertTrue(self.server.connections[0].closed)
        self.assertEqual(self.server.connections[1].folder, "INBOX", "the folder must be opened again")
        self.assertIn("reconnecting", output)

    def test_a_connection_that_keeps_dropping_gives_up_without_a_pointless_last_wait(self):
        self.add_candidates(["10"])

        def always_drops(conn, cmd, uid_set, items):
            raise imaplib.IMAP4.abort("socket error: EOF")
        self.server.on_uid = always_drops
        with self.assertRaises(imaplib.IMAP4.abort):
            self.fetch_bodies()
        self.assertEqual(len(self.server.connections), imap_mod.MAX_RETRIES)
        self.assertEqual(self.sleeps, [2, 4])                                 # not 2, 4, 8 and then giving up
        self.assertEqual(len(db.emails_missing_body(self.conn)), 1)           # nothing was marked done

    def test_a_timeout_is_treated_like_a_dropped_connection(self):
        self.add_candidates(["10"])
        slow = []

        def times_out_once(conn, cmd, uid_set, items):
            if not slow:
                slow.append(True)
                raise TimeoutError("timed out")
            return body_reply(uid_set, {"10": GOOD})
        self.server.on_uid = times_out_once
        stored, _ = self.fetch_bodies()
        self.assertEqual(stored, 1)
        self.assertEqual(len(self.server.connections), 2)

    def test_retry_repeats_a_dropped_connection_but_not_a_bad_answer(self):
        calls = []

        def drops_twice():
            calls.append(1)
            if len(calls) < 3:
                raise imaplib.IMAP4.abort("socket error: EOF")
            return "worked"
        self.assertEqual(self.quietly(imap_mod._retry, drops_twice)[0], "worked")
        self.assertEqual(self.sleeps, [2, 4])

        calls.clear()

        def bad():
            calls.append(1)
            raise imaplib.IMAP4.error("BAD nonsense")
        with self.assertRaises(imaplib.IMAP4.error):
            self.quietly(imap_mod._retry, bad)
        self.assertEqual(len(calls), 1)


class HeaderScan(ImapCase):
    def serve(self, uids_by_folder, refuse=lambda folder, uid_set: False):
        def on_uid(conn, command, *args):
            if command == "search":
                return "OK", [" ".join(uids_by_folder[conn.folder]).encode()]
            if refuse(conn.folder, args[0]):
                return NO_FETCH
            return header_reply(args[0])
        self.server.on_uid = on_uid

    def scan(self, **kwargs):
        return self.quietly(imap_mod.fetch_headers_for_account, ACCT, self.conn, **kwargs)

    def emails(self) -> list:
        return [r["uid"] for r in self.conn.execute("SELECT uid FROM emails ORDER BY uid")]

    def test_the_since_date_reaches_the_server_in_the_form_it_understands(self):
        self.serve({"INBOX": []})
        self.scan(since="2024-01-31")
        searches = [args for _n, command, args in self.server.calls if command == "search"]
        self.assertEqual(searches, [(None, "SINCE 31-Jan-2024")])

    def test_without_a_date_everything_is_searched(self):
        self.serve({"INBOX": []})
        self.scan()
        self.assertEqual([a for _n, c, a in self.server.calls if c == "search"], [(None, "ALL")])

    def test_a_date_the_server_could_not_read_is_refused_before_connecting(self):
        with self.assertRaises(ValueError):
            self.scan(since="31/01/2024")
        self.assertEqual(self.server.connections, [], "nothing should have connected")

    def test_a_refused_batch_is_skipped_and_the_rest_still_arrives(self):
        self.serve({"INBOX": ["1", "2", "3", "4"]}, refuse=lambda folder, uid_set: "1" in uid_set.split(","))
        with mock.patch.object(imap_mod, "IMAP_BATCH_SIZE", 2):
            inserted, output = self.scan()
        self.assertEqual(inserted, 3)                          # 2, 3 and 4: only message 1 was lost
        self.assertEqual(self.emails(), ["2", "3", "4"])
        self.assertIn("refused", output)
        self.assertIn("Scan again", output)

    def test_scanning_again_picks_up_what_was_refused_without_duplicating_the_rest(self):
        self.serve({"INBOX": ["1", "2"]}, refuse=lambda folder, uid_set: "1" in uid_set.split(","))
        self.scan()
        self.assertEqual(self.emails(), ["2"])
        self.serve({"INBOX": ["1", "2"]})                      # the server has recovered
        inserted, _ = self.scan()
        self.assertEqual(inserted, 1)
        self.assertEqual(self.emails(), ["1", "2"])

    def test_a_refused_search_skips_that_folder_and_carries_on(self):
        acct = Account("gm", "gmail", "me@gmail.com", "imap.gmail.com", password="x", folders=["Broken", "INBOX"])

        def on_uid(conn, command, *args):
            if command == "search":
                return NO_FETCH if conn.folder == "Broken" else ("OK", [b"5 6"])
            return header_reply(args[0])
        self.server.on_uid = on_uid
        inserted, output = self.quietly(imap_mod.fetch_headers_for_account, acct, self.conn)
        self.assertEqual(inserted, 2)
        self.assertIn("refused the search", output)

    def test_a_missing_folder_is_skipped(self):
        acct = Account("gm", "gmail", "me@gmail.com", "imap.gmail.com", password="x", folders=["Nope", "INBOX"])

        def on_select(conn, folder):
            if folder == "Nope":
                raise imaplib.IMAP4.error("SELECT command error: BAD [NONEXISTENT]")
            return "OK", [b"1"]
        self.server.on_select = on_select
        self.server.on_uid = lambda conn, command, *args: (
            ("OK", [b"7"]) if command == "search" else header_reply(args[0]))
        inserted, output = self.quietly(imap_mod.fetch_headers_for_account, acct, self.conn)
        self.assertEqual(inserted, 1)
        self.assertIn("skipping folder Nope", output)

    def test_a_connection_that_drops_part_way_through_a_folder_carries_on(self):
        dropped = []

        def on_uid(conn, command, *args):
            if command == "search":
                return "OK", [b"1 2 3 4"]
            if not dropped and "3" in args[0].split(","):
                dropped.append(True)
                raise imaplib.IMAP4.abort("socket error: EOF")
            return header_reply(args[0])
        self.server.on_uid = on_uid
        with mock.patch.object(imap_mod, "IMAP_BATCH_SIZE", 2):
            inserted, _ = self.scan()
        self.assertEqual(inserted, 4)
        self.assertEqual(len(self.server.connections), 2)
        self.assertEqual(self.server.connections[1].folder, "INBOX")
        self.assertEqual(self.sleeps, [2])

    def test_a_server_that_refuses_everything_is_given_up_on_quickly(self):
        uids = [str(n) for n in range(1, 11)]
        self.serve({"INBOX": uids}, refuse=lambda folder, uid_set: True)
        with mock.patch.object(imap_mod, "IMAP_BATCH_SIZE", 1):
            inserted, output = self.scan()
        self.assertEqual(inserted, 0)
        self.assertEqual(len(set(self.server.fetches())), imap_mod.MAX_REFUSED_BATCHES_IN_A_ROW)
        self.assertIn("keeps refusing", output)


class MiniImapServer:
    """A tiny IMAP server on this computer, just enough for Python's real `imaplib` to talk to. The fake
    connections above test Vestige's logic; this checks the ASSUMPTIONS behind them against the real
    library: a NO comes back as an ordinary answer, a BAD raises IMAP4.error, and a hang-up or a reply cut
    off part-way raises IMAP4.abort.

    `behave(connection_number, command_text, tag)` returns (bytes to send, hang up afterwards?), or None
    to hang up without answering."""

    def __init__(self, behave):
        self.behave, self.connections, self._stop = behave, 0, threading.Event()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop.set()
        self._sock.close()

    def _accept(self):
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except OSError:
                continue
            self.connections += 1
            threading.Thread(target=self._serve, args=(client, self.connections), daemon=True).start()

    def _serve(self, client, number):
        with client, client.makefile("rb") as lines:
            client.sendall(b"* OK mini IMAP ready\r\n")
            for raw in lines:
                tag, _, rest = raw.decode().strip().partition(" ")
                word = rest.split(" ")[0].upper()
                if word == "CAPABILITY":
                    reply, hang_up = b"* CAPABILITY IMAP4rev1\r\n" + f"{tag} OK done\r\n".encode(), False
                elif word == "LOGIN":
                    reply, hang_up = f"{tag} OK logged in\r\n".encode(), False
                elif word in ("EXAMINE", "SELECT"):
                    reply, hang_up = b"* 2 EXISTS\r\n" + f"{tag} OK [READ-ONLY] done\r\n".encode(), False
                elif word == "LOGOUT":
                    reply, hang_up = b"* BYE bye\r\n" + f"{tag} OK bye\r\n".encode(), False
                elif word == "UID":
                    answer = self.behave(number, rest, tag)
                    if answer is None:
                        return
                    reply, hang_up = answer
                else:
                    reply, hang_up = f"{tag} BAD unknown command\r\n".encode(), False
                client.sendall(reply)
                if hang_up:
                    return


def _good_fetch(rest, tag):
    """A proper answer to a body fetch, for the UIDs named in the command."""
    out = b""
    for n, uid in enumerate(rest.split(" ")[2].split(","), start=1):
        out += f"* {n} FETCH (UID {uid} BODY[]<0> {{{len(GOOD)}}}\r\n".encode() + GOOD + b")\r\n"
    return out + f"{tag} OK UID FETCH completed\r\n".encode(), False


class RealImaplib(ImapCase):
    """The same behaviours, through the real imaplib talking to MiniImapServer over a local socket."""

    def start(self, behave):
        self.mini = MiniImapServer(behave)
        self.addCleanup(self.mini.close)

        def connect(account):
            client = imaplib.IMAP4("127.0.0.1", self.mini.port, timeout=5)
            client.login("user", "password")
            return client
        patcher = mock.patch.object(imap_mod, "connect", connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_NO_answer_leaves_the_emails_waiting(self):
        self.add_candidates(["10", "11"])
        self.start(lambda n, rest, tag: (f"{tag} NO Some messages could not be FETCHed (Failure)\r\n".encode(), False))
        stored, _ = self.fetch_bodies()
        self.assertEqual(stored, 0)
        self.assertEqual(len(db.emails_missing_body(self.conn)), 2)
        for uid, (_text, fetched) in self.snippets().items():
            self.assertEqual(fetched, 0, uid)

    def test_a_BAD_answer_is_not_retried(self):
        self.add_candidates(["10"])
        self.start(lambda n, rest, tag: (f"{tag} BAD Could not parse command\r\n".encode(), False))
        self.fetch_bodies()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(db.emails_missing_body(self.conn)), 1)

    def test_a_hang_up_is_survived_by_reconnecting(self):
        self.add_candidates(["10", "11"])
        self.start(lambda n, rest, tag: None if n == 1 else _good_fetch(rest, tag))
        stored, _ = self.fetch_bodies()
        self.assertEqual(stored, 2)
        self.assertEqual(self.mini.connections, 2)

    def test_a_reply_cut_off_part_way_is_never_stored_as_a_message(self):
        self.add_candidates(["10"])
        cut_off = (b"* 1 FETCH (UID 10 BODY[]<0> {500}\r\n0123456789", True)
        self.start(lambda n, rest, tag: cut_off if n == 1 else _good_fetch(rest, tag))
        stored, _ = self.fetch_bodies()
        self.assertEqual(stored, 1)
        text = self.snippets()["10"][0]
        self.assertIn("account is ready", text)           # the complete message from the second connection
        self.assertNotIn("0123456789", text)


class SinceDate(unittest.TestCase):
    def test_accepted_forms_become_the_form_the_server_reads(self):
        for typed, expected in (("01-Jan-2024", "01-Jan-2024"), ("1-jan-2024", "01-Jan-2024"),
                                ("2024-01-31", "31-Jan-2024"), ("2024-1-5", "05-Jan-2024"),
                                ("5 March 2023", "05-Mar-2023"), ("  15 Sept 2022 ", "15-Sep-2022"),
                                ("29-FEB-2024", "29-Feb-2024"), ("31 december 1999", "31-Dec-1999")):
            self.assertEqual(imap_mod.parse_since(typed), expected, typed)

    def test_nothing_typed_means_no_limit(self):
        for blank in ("", "   ", None):
            self.assertIsNone(imap_mod.parse_since(blank))

    def test_dates_the_server_could_not_read_are_refused_with_an_example(self):
        for typed in ("31-Feb-2024", "2024-13-01", "29-Feb-2023", "01/02/2024", "yesterday", "Jan 2024",
                      "01-Foo-2024", "2024-01", "1 January", "00-Jan-2024"):
            with self.assertRaises(ValueError, msg=typed) as caught:
                imap_mod.parse_since(typed)
            self.assertIn("01-Jan-2024", str(caught.exception))

    def test_the_result_is_stable_when_parsed_again(self):
        once = imap_mod.parse_since("2024-03-09")
        self.assertEqual(imap_mod.parse_since(once), once)


class CommandLineSince(unittest.TestCase):
    def test_a_readable_date_is_tidied(self):
        for command in (["fetch"], ["all"]):
            args = main.build_parser().parse_args(command + ["--since", "2020-01-02"])
            self.assertEqual(args.since, "02-Jan-2020")

    def test_an_unreadable_date_stops_the_command_with_a_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            main.build_parser().parse_args(["fetch", "--since", "next week"])
        self.assertIn("01-Jan-2024", err.getvalue())

    def test_leaving_it_out_is_fine(self):
        self.assertIsNone(main.build_parser().parse_args(["fetch"]).since)


class SinceDateInTheInterface(GuiCase):
    """Wiring tests against the stand-in for Tk (they can't show how it looks on a real screen)."""

    def setUp(self):
        super().setUp()
        self.page = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.depth_control = types.SimpleNamespace(get=lambda: "Balanced", set=lambda v: None,
                                                        configure=lambda **kw: None)

    def test_a_bad_date_stops_the_scan_before_a_job_starts(self):
        self.page.since_var.set("31/01/2024")
        with mock.patch.object(self.run_mod.dialogs, "showwarning") as warned:
            self.page._do_fetch()
        warned.assert_called_once()
        self.assertIn("01-Jan-2024", warned.call_args[0][1])
        self.app.run_job.assert_not_called()

    def test_a_good_date_is_tidied_in_the_box_and_passed_to_the_scan(self):
        self.page.since_var.set("2024-01-31")
        self.page._do_fetch()
        self.assertEqual(self.page.since_var.get(), "31-Jan-2024")
        job = self.app.run_job.call_args[0][0]
        with mock.patch.object(self.run_mod.pipeline, "stage_fetch") as stage:
            job(None)
        self.assertEqual(stage.call_args[0][2], "31-Jan-2024")

    def test_an_empty_box_means_no_limit(self):
        self.page._do_fetch()
        job = self.app.run_job.call_args[0][0]
        with mock.patch.object(self.run_mod.pipeline, "stage_fetch") as stage:
            job(None)
        self.assertIsNone(stage.call_args[0][2])

    def test_a_bad_date_stops_a_full_run_too(self):
        self.page.since_var.set("soon")
        with mock.patch.object(self.run_mod.dialogs, "showwarning") as warned, \
                mock.patch.object(self.page, "_provider", return_value=(object(), self.local_settings)):
            self.page._do_all()
        warned.assert_called_once()
        self.app.run_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
