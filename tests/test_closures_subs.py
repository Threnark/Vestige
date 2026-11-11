"""End-to-end tests of the closed-account and subscription pipelines, the depth setting and the
choice of pipelines, using a made-up mailbox and a fake AI that answers each new prompt."""
import csv
import dataclasses
import glob
import mailbox
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

import ai_providers as ai
import billing_math as bm
import db
import module_d_export as export
import module_f_closures as closures
import module_g_subscriptions as subs
import pipeline
import run_options
from models import Account
from tests.test_pipeline import add_message


def _rfc(d: date) -> str:
    return datetime(d.year, d.month, d.day, 10, 0).strftime("%a, %d %b %Y 10:00:00 +0000")


def build_scenario(path):
    """A mailbox that runs to 2026-10-20 with: a live monthly subscription (price rose at the end),
    a monthly one that silently stopped in 2024, one that was cancelled, a one-off shop, two Apple
    charges (one repeating, one single), two closure notices (one followed by later mail), and a
    service that simply went quiet."""
    box = mailbox.mbox(path)
    box.lock()
    n = [0]

    def add(frm, subject, d, body, to="me@gmail.com"):
        n[0] += 1
        add_message(box, frm, to, subject, _rfc(d), body, f"<{n[0]}@t>")

    months = lambda year, month, count: [date(year + (month - 1 + i) // 12, (month - 1 + i) % 12 + 1, 14)
                                         for i in range(count)]
    # 1. alive: StreamFlix, 9 monthly receipts to 2026-10-14; the last one costs more
    for i, d in enumerate(months(2026, 2, 9)):
        total = "NZ$19.99" if i == 8 else "NZ$17.99"
        add("StreamFlix <billing@streamflix.com>", "Your StreamFlix receipt", d,
            f"Thank you for your payment. Premium plan. Subtotal: NZ$15.64 GST: NZ$2.35 Total: {total}")
    # 2. silently stopped: CloudBox, 6 monthly receipts in 2024
    for d in months(2024, 1, 6):
        add("CloudBox <receipts@cloudbox.io>", "Your CloudBox subscription receipt", d,
            "Payment received. Amount paid: US$9.00 for CloudBox Pro")
    # 3. cancelled: GymPass, 5 monthly receipts then a cancellation notice
    for d in months(2025, 1, 5):
        add("GymPass <billing@gympass.co.nz>", "Your GymPass membership receipt", d, "Amount paid: $29.00")
    add("GymPass <billing@gympass.co.nz>", "Your membership has been cancelled", date(2025, 6, 2),
        "We're sorry to see you go. Your membership ended.")
    # 4. a shop with irregular receipts and no subscription wording
    for d in (date(2025, 3, 2), date(2025, 7, 19), date(2026, 1, 30)):
        add("ShopCo <orders@shopco.com>", "Your receipt from ShopCo", d, "Total: $54.10")
    # 5. Apple: one repeating NZ$1.29 charge, one single NZ$12.99
    for d in months(2026, 6, 5):
        add("Apple <no_reply@email.apple.com>", "Your receipt from Apple.", d, "iCloud+ 50GB. Total: NZ$1.29")
    add("Apple <no_reply@email.apple.com>", "Your receipt from Apple.", date(2026, 4, 3), "Some App. Total: NZ$12.99")
    # 6. closures
    add("OldService <hello@oldservice.com>", "Welcome to OldService - verify your email", date(2019, 1, 10), "Welcome!")
    add("OldService <hello@oldservice.com>", "Your account has been closed", date(2023, 5, 2),
        "As you asked, your OldService account has been closed.")
    add("GameSite <team@gamesite.com>", "Welcome to GameSite - verify your email", date(2020, 3, 1), "Welcome!")
    add("GameSite <team@gamesite.com>", "Your account has been deleted", date(2024, 2, 10),
        "Your GameSite account was deleted at your request.")
    add("GameSite <team@gamesite.com>", "Weekly game news", date(2025, 3, 1), "News!")
    add("Shop <hi@bigshop.com>", "Closing sale: 50% off everything", date(2025, 11, 1), "Sale!")
    # 7. went quiet (a sign-up and nothing since)
    add("OldForum <noreply@oldforum.org>", "Welcome to OldForum - verify your email", date(2021, 4, 1), "Hello!")
    # 8. the newest mail in the mailbox
    add("A Friend <friend@example.net>", "Hello", date(2026, 10, 20), "Lunch?")
    box.flush()
    box.unlock()
    box.close()


class FakeAI(ai.AIProvider):
    """Answers all four prompts from simple text patterns, and records what it was asked."""

    NAMES = {"oldservice.com": "OldService", "gamesite.com": "GameSite", "oldforum.org": "OldForum"}

    def __init__(self):
        super().__init__()
        self.is_local = True
        self.calls = []                     # (tool name, user message)

    def structured_call(self, system_prompt, user_message, tool, essential=(), fallbacks=None, max_tokens=3000):
        name = tool["name"]
        self.calls.append((name, user_message))
        if name == "record_closure_checks":
            out = []
            for block in user_message.split("### GROUP id=")[1:]:
                key = block.splitlines()[0]
                domain = key.split("#")[0]
                out.append({"group_id": key, "is_closure": bool(re.search(r"closed|deleted", block)),
                            "service_name": self.NAMES.get(domain, domain), "kind": "closed" if "closed" in block else "deleted",
                            "who": "user", "sample": 1, "confidence": 0.9})
            return out
        if name == "record_subscription_checks":
            out = []
            for block in user_message.split("### GROUP id=")[1:]:
                key = block.splitlines()[0]
                total = re.search(r"(\d)\) total", block)
                service = {"streamflix.com": "StreamFlix", "cloudbox.io": "CloudBox",
                           "gympass.co.nz": "GymPass"}.get(key.split("~")[0], "iCloud+" if "~" in key else key)
                out.append({"group_id": key, "is_subscription": True, "service_name": service,
                            "plan_name": "Premium", "status": "cancelled" if "cancel" in block else "active",
                            "period": "monthly", "amount": int(total.group(1)) if total else 0, "confidence": 0.9})
            return out
        if name == "record_signup_classifications":
            out = []
            for block in user_message.split("### GROUP: ")[1:]:
                key = block.splitlines()[0].split(" ")[0]
                service = self.NAMES.get(key, "")
                out.append({"group_id": key, "is_signup": bool(service), "service_name": service,
                            "category": "other", "signup_method": "native_password", "confidence": 0.9,
                            "representative_sample_index": 0, "reasoning": "fake"})
            return out
        return []                                                  # the email-change prompt: nothing to find

    def tools(self):
        return [name for name, _ in self.calls]


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mbox = os.path.join(self.tmp, "mail.mbox")
        build_scenario(self.mbox)
        self.acct = Account(name="gmail", provider="gmail", email="me@gmail.com")
        self.conn = db.open_database(None)
        self.ai = FakeAI()
        pipeline.stage_import_mail(self.conn, self.acct, self.mbox)
        self.pre = pipeline.stage_prefilter(self.conn)

    def run_pipelines(self, which, depth=None):
        pipeline.run_full(self.conn, [self.acct], self.ai, fetch=False, pipelines=which, depth=depth)


class PreFilterTracks(Scenario):
    def test_closure_and_billing_are_flagged_without_the_ai(self):
        self.assertEqual(self.pre["closure_notices"], 2)             # the "closing sale" is not one
        self.assertEqual(self.ai.calls, [])
        counts = db.counts(self.conn)
        self.assertEqual(counts["closure_notices"], 2)
        self.assertGreaterEqual(counts["billing_emails"], 24)

    def test_amounts_are_kept_for_billing_emails_only(self):
        row = self.conn.execute("SELECT amounts_json FROM emails WHERE subject='Hello'").fetchone()
        self.assertIsNone(row["amounts_json"])
        row = self.conn.execute("SELECT amounts_json FROM emails WHERE subject='Your StreamFlix receipt' "
                                "ORDER BY date_iso DESC").fetchone()
        self.assertIn("19.99", row["amounts_json"])

    def test_running_the_prefilter_again_does_nothing(self):
        again = pipeline.stage_prefilter(self.conn)
        self.assertEqual((again["scored"], again["rechecked"]), (0, 0))


class ClosedAccounts(Scenario):
    def test_verdicts_come_from_later_mail_not_from_the_ai(self):
        self.run_pipelines(["closed"])
        records = {r.service: r for r in closures.closure_records(self.conn)}
        self.assertEqual(set(records), {"OldService", "GameSite"})
        self.assertEqual(records["OldService"].verdict, closures.VERDICT_LIKELY)
        self.assertEqual(records["OldService"].later_count, 0)
        self.assertEqual(records["GameSite"].verdict, closures.VERDICT_POSSIBLE)
        self.assertEqual(records["GameSite"].later_count, 1)
        self.assertEqual(records["GameSite"].later_latest, "2025-03-01")

    def test_only_the_shortlist_reaches_the_ai(self):
        self.run_pipelines(["closed"])
        self.assertEqual(set(self.ai.tools()), {"record_closure_checks"})
        sent = " ".join(m for _, m in self.ai.calls)
        self.assertIn("oldservice.com", sent)
        self.assertNotIn("streamflix", sent)
        self.assertNotIn("me@gmail.com", sent)                       # your address is never sent
        self.assertNotIn("Closing sale", sent)

    def test_checked_senders_are_not_asked_again(self):
        self.run_pipelines(["closed"])
        asked = len(self.ai.calls)
        self.run_pipelines(["closed"])
        self.assertEqual(len(self.ai.calls), asked)

    def test_signup_rows_keep_the_service_and_add_a_note(self):
        self.run_pipelines(["signups", "closed"])
        rows = {r["Service Name"]: r for r in export.build_signup_rows(self.conn, 0.5)}
        self.assertIn("OldService", rows)                            # still listed
        self.assertTrue(rows["OldService"]["Account Check"].startswith("Likely closed"))
        self.assertEqual(rows["OldService"]["Activity Hint"], "")          # the notice is stronger evidence: no weak hint too
        self.assertEqual(rows["OldService"]["Last Mail From Service"], "2023-05-02")   # but the plain fact is still exported
        self.assertTrue(rows["GameSite"]["Account Check"].startswith("Possibly closed"))
        self.assertEqual(rows["OldForum"]["Account Check"], "")

    def test_quiet_hint_is_separate_and_never_says_closed(self):
        self.run_pipelines(["signups"])                             # closure pipeline NOT run
        rows = {r["Service Name"]: r for r in export.build_signup_rows(self.conn, 0.5)}
        self.assertEqual(rows["OldForum"]["Activity Hint"], "No mail since 2021-04-01")
        self.assertEqual(rows["OldForum"]["Account Check"], "")
        for row in rows.values():
            self.assertNotIn("closed", row["Activity Hint"].lower())

    def test_quiet_hint_needs_a_long_gap(self):
        index = closures.ActivityIndex(self.conn)
        self.assertEqual(index.hint("2026-01-01", "me@gmail.com"), "")             # 10 months: not quiet
        self.assertEqual(index.hint("2025-10-19", "me@gmail.com"), "No mail since 2025-10-19")   # a year
        self.assertEqual(index.hint("", "me@gmail.com"), "")


class Subscriptions(Scenario):
    def setUp(self):
        super().setUp()
        self.run_pipelines(["subscriptions"])
        self.records = {r.service: r for r in subs.subscription_records(self.conn)}

    def test_only_plausible_subscriptions_are_asked_about(self):
        asked = " ".join(m for _, m in self.ai.calls)
        for key in ("streamflix.com", "cloudbox.io", "gympass.co.nz", "email.apple.com~NZD1.29"):
            self.assertIn(f"### GROUP id={key}", asked)
        self.assertNotIn("shopco.com", asked)                        # irregular shop receipts: no AI cost
        self.assertNotIn("NZD12.99", asked)                          # a single Apple purchase
        self.assertEqual(set(self.ai.tools()), {"record_subscription_checks"})

    def test_status_is_judged_by_code_from_the_dates(self):
        self.assertEqual(self.records["StreamFlix"].state, bm.STATE_ACTIVE)
        self.assertEqual(self.records["CloudBox"].state, bm.STATE_STOPPED)
        self.assertEqual(self.records["GymPass"].state, bm.STATE_CANCELLED)
        self.assertEqual(self.records["iCloud+"].state, bm.STATE_ACTIVE)

    def test_cycle_last_charge_and_next_expected_come_from_code(self):
        r = self.records["StreamFlix"]
        self.assertEqual((r.cycle, r.cycle_source, r.charges), ("monthly", "observed", 9))
        self.assertEqual(r.last_charge, date(2026, 10, 14))
        self.assertEqual(r.next_expected, date(2026, 11, 13))        # last charge + the 30-day monthly cycle

    def test_the_ai_only_chooses_among_numbered_amounts(self):
        r = self.records["StreamFlix"]
        self.assertEqual((r.amount, r.currency), (19.99, "NZD"))     # the Total, not the subtotal or tax
        self.assertEqual(self.records["CloudBox"].amount, 9.0)
        sent = " ".join(m for _, m in self.ai.calls)
        self.assertRegex(sent, r"amounts: 1\) subtotal NZD 15\.64; 2\) tax NZD 2\.35; 3\) total NZD 19\.99")

    def test_totals_count_only_active_subscriptions_per_currency(self):
        t = subs.totals(list(self.records.values()))
        self.assertEqual(t["counted"], 2)                            # StreamFlix + iCloud+
        month, year = t["per_currency"]["NZD"]
        self.assertAlmostEqual(month, 19.99 + 1.29, places=2)
        self.assertAlmostEqual(year, (19.99 + 1.29) * 12, places=1)
        self.assertNotIn("USD", t["per_currency"])                   # the stopped USD one is not added
        self.assertEqual(t["possibly_stopped"], 1)
        self.assertIn("1 possibly stopped, not counted", subs.totals_sentence(list(self.records.values())))

    def test_states_sort_active_first(self):
        order = [r.state for r in subs.subscription_records(self.conn)]
        self.assertEqual(order, sorted(order, key=lambda s: subs._STATE_ORDER[s]))

    def test_the_subscription_status_is_not_stored(self):
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(subscriptions)")}
        self.assertFalse(columns & {"state", "status", "cycle", "last_charge", "next_expected"})


class DepthAndSelection(Scenario):
    def test_choosing_pipelines_runs_only_those_ai_stages(self):
        self.run_pipelines(["signups"])
        self.assertEqual(set(self.ai.tools()), {"record_signup_classifications"})
        self.assertEqual(db.counts(self.conn)["closed_accounts_found"], 0)
        self.assertEqual(db.counts(self.conn)["subscriptions_found"], 0)

    def test_all_pipelines_by_default(self):
        pipeline.run_full(self.conn, [self.acct], self.ai, fetch=False)
        self.assertTrue({"record_signup_classifications", "record_closure_checks",
                         "record_subscription_checks"} <= set(self.ai.tools()))
        self.assertGreater(db.counts(self.conn)["closed_accounts_found"], 0)
        self.assertGreater(db.counts(self.conn)["subscriptions_found"], 0)

    def test_nothing_chosen_is_refused(self):
        with self.assertRaises(ValueError):
            run_options.normalize_pipelines([])
        with self.assertRaises(ValueError):
            run_options.normalize_pipelines(["signups", "nonsense"])

    def test_deeper_means_more_text_sent(self):
        sizes = {}
        for name in run_options.DEPTH_NAMES:
            depth = run_options.get_depth(name)
            sizes[name] = (len(closures.build_user_message(closures.build_groups(self.conn, depth), depth)),
                           len(subs.build_user_message(sorted(subs.collect_groups(self.conn).values(),
                                                              key=lambda g: g.key), depth)))
        self.assertLess(sizes["Quick"][1], sizes["Balanced"][1])
        self.assertLess(sizes["Balanced"][1], sizes["Thorough"][1])
        self.assertLessEqual(sizes["Quick"][0], sizes["Balanced"][0])

    def test_balanced_is_exactly_the_old_behaviour(self):
        import module_c_classify as c
        import module_e_email_changes as e
        d = run_options.BALANCED
        self.assertEqual((d.signup_samples, d.signup_chars, d.change_chars), (3, 400, 800))
        self.assertEqual((c.MAX_SAMPLES_PER_GROUP, c.SNIPPET_CHARS_SENT, e.SNIPPET_CHARS_SENT), (3, 400, 800))
        self.assertIs(run_options.get_depth(None), run_options.BALANCED)
        self.assertIs(run_options.get_depth("THOROUGH"), run_options.THOROUGH)

    def test_quick_sends_fewer_subscription_samples(self):
        group = subs.collect_groups(self.conn)["gympass.co.nz"]
        self.assertEqual(len(group.sample_rows(run_options.QUICK)), 1)
        self.assertEqual(group.sample_rows(run_options.BALANCED)[0]["billing_kind"], "charge")
        self.assertEqual(group.sample_rows(run_options.BALANCED)[1]["billing_kind"], "cancel")
        self.assertEqual(len(group.sample_rows(run_options.THOROUGH)), 3)

    def test_prompts_are_smaller_than_the_older_ones(self):
        from prompts import classification_prompt, closure_prompt, subscription_prompt
        old = len(classification_prompt.SYSTEM_PROMPT)
        self.assertLess(len(closure_prompt.SYSTEM_PROMPT), old)
        self.assertLess(len(subscription_prompt.SYSTEM_PROMPT), old)


class BodiesAreOnlyFetchedForWhatIsNeeded(Scenario):
    def test_subscription_bodies_are_limited_to_the_sample_emails(self):
        ids = subs.body_ids_wanted(self.conn, run_options.BALANCED)
        total_billing = db.counts(self.conn)["billing_emails"]
        self.assertLess(len(set(ids)), total_billing / 2)           # a handful per sender, not every receipt

    def test_unticked_pipelines_need_no_bodies(self):
        # Pretend everything came over IMAP: give every row a UID and no body.
        self.conn.execute("UPDATE emails SET uid='1', body_fetched=0")
        only_signups = db.emails_missing_body(self.conn, ("signups",))
        everything = db.emails_missing_body(self.conn, ("signups", "changes", "closed"),
                                            subs.body_ids_wanted(self.conn))
        self.assertLess(len(only_signups), len(everything))
        signup_candidates = {r["id"] for r in self.conn.execute(
            "SELECT id FROM emails WHERE prefilter_label='candidate'")}
        self.assertEqual({r["id"] for r in only_signups}, signup_candidates)   # nothing else is downloaded


class Export(Scenario):
    def test_new_tables_are_exported_and_sanitised(self):
        self.run_pipelines(None)
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        result = export.export_files(self.conn, export.ExportOptions(
            folder=out, formats=("csv", "md"), add_timestamp=False), now=datetime(2026, 11, 3, 9, 0))
        names = sorted(os.path.basename(f) for f in result.files)
        self.assertIn("signups_closed_accounts.csv", names)
        self.assertIn("signups_subscriptions.csv", names)
        with open(os.path.join(out, "signups_subscriptions.csv"), encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(list(rows[0]), export.SUBSCRIPTION_COLUMNS)
        by_name = {r["Service Name"]: r for r in rows}
        self.assertEqual(by_name["StreamFlix"]["Status"], "Active")
        self.assertEqual(by_name["StreamFlix"]["Last Charge Amount"], "NZD 19.99")
        self.assertEqual(by_name["CloudBox"]["Status"], "Possibly stopped")
        with open(os.path.join(out, "signups.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("## Closed or deactivated accounts", md)
        self.assertIn("## Subscriptions", md)
        self.assertIn("Active subscriptions (2)", md)
        with open(os.path.join(out, "signups.csv"), encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh))
        self.assertEqual(header, export.SIGNUP_COLUMNS)
        self.assertEqual(header[:7], ["Service Name", "Email Address Used", "Sign-up Confirmation Date",
                                      "Category", "Sign-up Method", "Confidence", "Source Domain(s)"])

    def test_nothing_to_export_is_still_refused(self):
        empty = db.open_database(None)
        with self.assertRaises(export.ExportError):
            export.export_files(empty, export.ExportOptions(folder=self.tmp))


class EachRowSaysWhichAddressItBelongsTo(unittest.TestCase):
    """One mailbox that receives mail at three of your addresses: the Closed accounts and Subscriptions
    rows must name the address(es) involved, so several addresses can be reviewed at once."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        path = os.path.join(tmp, "mail.mbox")
        box = mailbox.mbox(path)
        box.lock()
        count = [0]

        def add(frm, subject, when, body, to):
            count[0] += 1
            add_message(box, frm, to, subject, _rfc(when), body, f"<{count[0]}@t>")

        months = lambda year, month, n: [date(year + (month - 1 + i) // 12, (month - 1 + i) % 12 + 1, 14)
                                         for i in range(n)]
        for d in months(2026, 2, 9):                                   # billed to one address
            add("StreamFlix <billing@streamflix.com>", "Your StreamFlix receipt", d,
                "Premium plan. Total: NZ$17.99", "me@gmail.com")
        for i, d in enumerate(months(2024, 1, 6)):                     # billed to two addresses
            add("CloudBox <receipts@cloudbox.io>", "Your CloudBox subscription receipt", d,
                "Payment received. Amount paid: US$9.00 for CloudBox Pro",
                "me+work@gmail.com" if i < 3 else "me@gmail.com")
        add("OldService <hello@oldservice.com>", "Welcome to OldService - verify your email", date(2019, 1, 10),
            "Welcome!", "me+old@gmail.com")
        add("OldService <hello@oldservice.com>", "Your account has been closed", date(2023, 5, 2),
            "As you asked, your OldService account has been closed.", "me+old@gmail.com")
        add("GameSite <team@gamesite.com>", "Welcome to GameSite - verify your email", date(2020, 3, 1),
            "Welcome!", "me@gmail.com")
        add("GameSite <team@gamesite.com>", "Your account has been deleted", date(2024, 2, 10),
            "Your GameSite account was deleted at your request.", "me@gmail.com")
        add("A Friend <friend@example.net>", "Hello", date(2026, 10, 20), "Lunch?", "me@gmail.com")
        box.flush()
        box.unlock()
        box.close()
        self.conn = db.open_database(None)
        account = Account(name="gmail", provider="gmail", email="me@gmail.com")
        pipeline.stage_import_mail(self.conn, account, path)
        pipeline.run_full(self.conn, [account], FakeAI(), fetch=False)

    def test_a_closed_account_names_the_address_its_notice_went_to(self):
        rows = {r["Service Name"]: r for r in export.build_closed_rows(self.conn)}
        self.assertEqual(rows["OldService"]["Email Address Used"], "me+old@gmail.com")
        self.assertEqual(rows["GameSite"]["Email Address Used"], "me@gmail.com")

    def test_a_subscription_lists_every_address_that_received_its_billing_emails(self):
        rows = {r["Service Name"]: r for r in export.subscription_table(self.conn)[0]}
        self.assertEqual(rows["StreamFlix"]["Email Address Used"], "me@gmail.com")
        self.assertEqual(rows["CloudBox"]["Email Address Used"], "me+work@gmail.com; me@gmail.com")

    def test_an_address_the_email_did_not_give_is_shown_as_unknown(self):
        closed = [dataclasses.replace(r, address="") for r in closures.closure_records(self.conn)]
        with mock.patch.object(closures, "closure_records", return_value=closed):
            self.assertEqual({r["Email Address Used"] for r in export.build_closed_rows(self.conn)}, {"(unknown)"})
        bare = [dataclasses.replace(r, by_address=()) for r in subs.subscription_records(self.conn)]
        with mock.patch.object(subs, "subscription_records", return_value=bare):
            self.assertEqual({r["Email Address Used"] for r in export.subscription_table(self.conn)[0]},
                             {"(unknown)"})

    def test_a_subscription_breaks_its_charges_down_by_address(self):
        record = {r.service: r for r in subs.subscription_records(self.conn)}["CloudBox"]
        self.assertEqual(record.addresses, ("me+work@gmail.com", "me@gmail.com"))
        uses = {u.address: u for u in record.by_address}
        self.assertEqual((uses["me+work@gmail.com"].charges, uses["me+work@gmail.com"].last_charge),
                         (3, date(2024, 3, 14)))
        self.assertEqual((uses["me@gmail.com"].charges, uses["me@gmail.com"].last_charge), (3, date(2024, 6, 14)))

    def test_the_breakdown_is_written_in_plain_words_in_the_export(self):
        rows = {r["Service Name"]: r for r in export.subscription_table(self.conn)[0]}
        self.assertEqual(rows["CloudBox"]["Charges By Address"],
                         "me+work@gmail.com: 3 charges, last 2024-03-14; me@gmail.com: 3 charges, last 2024-06-14")
        self.assertEqual(rows["StreamFlix"]["Charges By Address"], "me@gmail.com: 9 charges, last 2026-10-14")

    def test_an_address_that_only_got_a_notice_or_has_no_usable_date_is_described_honestly(self):
        self.assertEqual(export._address_use_text(subs.AddressUse("a@x.com", 1, date(2025, 1, 2), date(2025, 1, 2))),
                         "a@x.com: 1 charge, last 2025-01-02")
        self.assertEqual(export._address_use_text(subs.AddressUse("a@x.com", 0, None, date(2025, 5, 1))),
                         "a@x.com: billing emails only, last 2025-05-01")
        self.assertEqual(export._address_use_text(subs.AddressUse("a@x.com", 2, None, None)),
                         "a@x.com: 2 charges, last date unknown")

    def test_the_address_comes_right_after_the_service_in_both_exports(self):
        for columns in (export.CLOSED_COLUMNS, export.SUBSCRIPTION_COLUMNS):
            self.assertEqual(columns[:2], ["Service Name", "Email Address Used"])
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        export.export_files(self.conn, export.ExportOptions(folder=out, formats=("csv",), add_timestamp=False),
                            now=datetime(2026, 11, 3, 9, 0))
        with open(os.path.join(out, "signups_subscriptions.csv"), encoding="utf-8-sig", newline="") as fh:
            by_name = {r["Service Name"]: r for r in csv.DictReader(fh)}
        self.assertEqual(by_name["CloudBox"]["Email Address Used"], "me+work@gmail.com; me@gmail.com")
        with open(os.path.join(out, "signups_closed_accounts.csv"), encoding="utf-8-sig", newline="") as fh:
            by_name = {r["Service Name"]: r for r in csv.DictReader(fh)}
        self.assertEqual(by_name["OldService"]["Email Address Used"], "me+old@gmail.com")


class CommandLine(unittest.TestCase):
    def parse(self, *argv):
        import main
        return main, main.build_parser().parse_args(list(argv))

    def test_pipelines_and_depth_options(self):
        main, args = self.parse("all", "--skip-fetch", "--pipelines", "signups,closed", "--depth", "quick")
        self.assertEqual(main._pipelines(args), ("signups", "closed"))
        self.assertIs(main._depth(args), run_options.QUICK)

    def test_defaults_are_everything_at_balanced_depth(self):
        main, args = self.parse("all")
        self.assertEqual(main._pipelines(args), run_options.PIPELINE_ORDER)
        self.assertIs(main._depth(args), run_options.BALANCED)

    def test_the_new_commands_exist_and_take_a_depth(self):
        for command in ("check-closed", "find-subscriptions", "classify", "find-changes"):
            _, args = self.parse(command, "--depth", "thorough")
            self.assertEqual(args.depth, "thorough", command)

    def test_a_mistyped_pipeline_is_reported_before_anything_else_happens(self):
        main, args = self.parse("all", "--pipelines", "signup,nonsense")
        with self.assertRaises(SystemExit) as caught:
            main.cmd_all(args, None)                   # no database or AI key needed: it stops at once
        self.assertIn("Unknown pipeline", str(caught.exception))


class UpgradingAnOldSession(unittest.TestCase):
    """A saved session from Vestige 0.3 (schema 2) opens, keeps its data and gets the new rules."""

    def test_schema_2_file_is_upgraded_in_place(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "session.db")
        conn = db.open_database(path)
        conn.execute("INSERT INTO emails (account_name, message_id, sender_domain, subject, date_iso, "
                     "email_address_used, prefilter_label, prefilter_score, body_fetched, body_snippet) "
                     "VALUES ('a','<1>','shop.com','Your account has been closed','2024-01-01T10:00:00+00:00',"
                     "'me@x.com','noise',0,1,'Total: $5.00')")
        conn.commit()
        # Turn it into a version-2 file: remove everything version 3 added.
        for index in ("idx_emails_closure", "idx_emails_billing"):
            conn.execute(f"DROP INDEX {index}")
        for column, _ in db._V3_EMAIL_COLUMNS:
            conn.execute(f"ALTER TABLE emails DROP COLUMN {column}")
        conn.execute("DROP TABLE closure_checks")
        conn.execute("DROP TABLE subscriptions")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

        conn = db.open_database(path)                                # the upgrade happens here
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 1)
        result = pipeline.stage_prefilter(conn)
        self.assertEqual((result["scored"], result["rechecked"], result["closure_notices"]), (0, 1, 1))
        self.assertEqual(db.counts(conn)["closure_notices"], 1)
        self.assertEqual(conn.execute("SELECT prefilter_label FROM emails").fetchone()[0], "noise")   # old score kept

    def test_other_versions_are_still_refused(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "old.db")
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE emails (id INTEGER)")
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
        raw.close()
        with self.assertRaises(db.DatabaseVersionError):
            db.open_database(path)


if __name__ == "__main__":
    unittest.main()
