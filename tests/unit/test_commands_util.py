import time
import unittest

from imapgw.commands import _wildcard_match


class WildcardTests(unittest.TestCase):
    def test_matching(self):
        cases = [
            ("*", "INBOX", True),
            ("%", "Drafts", True),
            ("INBOX", "INBOX", True),
            ("inbox", "INBOX", False),
            ("Dra*", "Drafts", True),
            ("*fts", "Drafts", True),
            ("D*a*s", "Drafts", True),
            ("D*x", "Drafts", False),
            ("", "Drafts", False),
            ("*Z", "INBOX", False),
            ("**INBOX**", "INBOX", True),
            ("%*%", "", True),
        ]
        for pattern, name, expected in cases:
            with self.subTest(pattern=pattern, name=name):
                self.assertEqual(_wildcard_match(pattern, name), expected)
        self.assertTrue(_wildcard_match("inbox", "INBOX", ignore_case=True))

    def test_pathological_pattern_is_linear(self):  # F8
        started = time.monotonic()
        self.assertFalse(_wildcard_match("*" * 1000 + "Z", "INBOX"))
        self.assertTrue(_wildcard_match("*" * 1000 + "X", "INBOX"))
        self.assertLess(time.monotonic() - started, 0.1)
