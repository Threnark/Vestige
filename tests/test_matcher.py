"""Tests for migration_matcher: masked-address matching and notice analysis."""
import unittest

import migration_matcher as mm

KNOWN = {"jane.doe@gmail.com", "john@gmail.com", "work@acme.com", "old@yahoo.com"}


class MaskMatching(unittest.TestCase):
    def test_star_mask_matches_known_addresses(self):
        self.assertTrue(mm.mask_matches("j***@gmail.com", "jane.doe@gmail.com"))
        self.assertTrue(mm.mask_matches("j***@gmail.com", "john@gmail.com"))
        self.assertFalse(mm.mask_matches("j***@gmail.com", "work@acme.com"))

    def test_other_mask_styles(self):
        self.assertTrue(mm.mask_matches("j...@gmail.com", "jane.doe@gmail.com"))
        self.assertTrue(mm.mask_matches("j\u2022\u2022\u2022@gmail.com", "jane.doe@gmail.com"))
        self.assertTrue(mm.mask_matches("j\u2022\u2022\u2022e@gmail.com", "jane.doe@gmail.com"))
        self.assertTrue(mm.mask_matches("j***@g***.com", "jane.doe@gmail.com"))

    def test_visible_end_must_match(self):
        self.assertFalse(mm.mask_matches("j***x@gmail.com", "jane.doe@gmail.com"))

    def test_too_vague_masks_match_nothing(self):
        self.assertFalse(mm.mask_matches("***@***.***", "jane.doe@gmail.com"))
        self.assertIsNone(mm.mask_to_regex("no-at-sign***"))

    def test_mask_never_spans_the_at_sign(self):
        self.assertFalse(mm.mask_matches("j***@gmail.com", "j@x@gmail.com"))

    def test_normalize_printed(self):
        self.assertEqual(mm.normalize_printed("  <J***@Gmail.com>. "), "j***@gmail.com")
        for none_like in ("", "N/A", "none", None, "not shown", "no address"):
            self.assertEqual(mm.normalize_printed(none_like), "")


class AnalyzeChange(unittest.TestCase):
    def test_notice_to_old_address_with_one_masked_match(self):
        a = mm.analyze_change("old@yahoo.com", "old_address", "", "w***@acme.com", KNOWN, "changed")
        self.assertEqual(a.recipient_role, "old_address")
        self.assertEqual(a.likely_new, "work@acme.com")
        self.assertEqual(a.match_quality, "strong")

    def test_several_matches_is_only_possible(self):
        a = mm.analyze_change("old@yahoo.com", "old_address", "", "j***@gmail.com", KNOWN)
        self.assertEqual(a.match_quality, "possible")
        self.assertEqual(a.likely_new, "")
        self.assertEqual(a.new_candidates, ["jane.doe@gmail.com", "john@gmail.com"])

    def test_no_known_address_fits_gives_hint_only_and_never_invents(self):
        a = mm.analyze_change("old@yahoo.com", "old_address", "", "z***@proton.me", KNOWN)
        self.assertEqual(a.match_quality, "hint_only")
        self.assertEqual(a.likely_new, "")
        self.assertEqual(a.new_candidates, [])
        self.assertIn("doesn't match any address", a.summary)

    def test_notice_to_new_address_resolves_the_previous_one(self):
        a = mm.analyze_change("jane.doe@gmail.com", "new_address", "o***@yahoo.com", "", KNOWN)
        self.assertEqual(a.likely_old, "old@yahoo.com")
        self.assertEqual(a.likely_new, "jane.doe@gmail.com")
        self.assertEqual(a.match_quality, "strong")

    def test_recipient_itself_is_never_a_candidate_for_the_other_side(self):
        # j***@gmail.com fits the recipient too, but the OTHER address is wanted.
        a = mm.analyze_change("jane.doe@gmail.com", "new_address", "j***@gmail.com", "", KNOWN)
        self.assertEqual(a.old_candidates, ["john@gmail.com"])

    def test_printed_recipient_overrides_a_wrong_role(self):
        a = mm.analyze_change("work@acme.com", "old_address", "", "work@acme.com", KNOWN)
        self.assertEqual(a.recipient_role, "new_address")

    def test_unclear_role_is_inferred_from_a_matching_mask(self):
        a = mm.analyze_change("jane.doe@gmail.com", "unclear", "j***@gmail.com", "x@proton.me", KNOWN)
        self.assertEqual(a.recipient_role, "old_address")

    def test_nothing_printed(self):
        a = mm.analyze_change("old@yahoo.com", "old_address", "n/a", "None", KNOWN)
        self.assertEqual(a.match_quality, "none")
        self.assertIn("doesn't say", a.summary)

    def test_full_unknown_address_is_a_hint_not_a_match(self):
        a = mm.analyze_change("old@yahoo.com", "old_address", "", "fresh@proton.me", KNOWN)
        self.assertEqual(a.new_status, "exact_unknown")
        self.assertEqual(a.match_quality, "hint_only")


if __name__ == "__main__":
    unittest.main()
