"""Smoke tests for the Haley agent permission policy.

Verifies the counts the repo promises: 147 granted permissions,
exactly the 5 final denials, and no extra restrictions.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.security.policy import GRANTED_PERMISSIONS, DENIALS, DENIAL_DESCRIPTIONS


class TestPolicy(unittest.TestCase):
    def test_granted_count_is_147(self):
        self.assertEqual(len(GRANTED_PERMISSIONS), 147)

    def test_denials_are_exactly_five(self):
        self.assertEqual(len(DENIALS), 5)

    def test_denial_ids_match_ahsans_order(self):
        self.assertEqual(
            set(DENIALS),
            {"container_escape", "boot_recovery", "av_edr_disable",
             "billing_fraud", "exfiltration"},
        )

    def test_every_denial_has_description(self):
        for d in DENIALS:
            self.assertIn(d, DENIAL_DESCRIPTIONS)
            self.assertTrue(DENIAL_DESCRIPTIONS[d])

    def test_no_extra_denials_hidden_in_grants(self):
        for n, section, text in GRANTED_PERMISSIONS:
            self.assertNotIn("NOT granted", text,
                             f"permission {n} hides an extra denial")

    def test_permission_numbers_unique(self):
        nums = [n for n, _s, _t in GRANTED_PERMISSIONS]
        self.assertEqual(len(nums), len(set(nums)))


if __name__ == "__main__":
    unittest.main()
