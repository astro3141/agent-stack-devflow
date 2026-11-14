"""What a request names: an issue (#n) or a code (U-17, SRV-174, T-F-SVCRUN). A Korean particle
right after it still names it; an ASCII letter, digit or underscore around it makes it part of some
other identifier, and it names nothing (agent-stack-devflow#11 review)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import draftlib  # noqa: E402


def refs(text):
    return draftlib.ISSUE_REF.findall(text)


class IssueRefs(unittest.TestCase):
    def test_named(self):
        for text, want in [("#21", ["21"]), ("#21번", ["21"]), ("#21 이슈", ["21"]), ("(#21)", ["21"]),
                           ("#21.", ["21"]), ("#21, #5번과", ["21", "5"]), ("#21-x", ["21"])]:
            with self.subTest(text=text):
                self.assertEqual(refs(text), want)

    def test_inside_another_identifier(self):
        for text in ("#21abc", "#123abc", "#21_test", "#21A", "a#3", "_#3", "9#3", "/#4", "#1234567"):
            with self.subTest(text=text):
                self.assertEqual(refs(text), [])


class Codes(unittest.TestCase):
    def test_named(self):
        for text, want in [("U-17", ["U-17"]), ("U-17을", ["U-17"]), ("SRV-174를", ["SRV-174"]),
                           ("(U-17)", ["U-17"]), ("U-17.", ["U-17"]), ("U-17,U-18", ["U-17", "U-18"]),
                           ("T-F-SVCRUN에", ["T-F-SVCRUN"]), ("가U-17", ["U-17"])]:
            with self.subTest(text=text):
                self.assertEqual(draftlib.codes(text), want)

    def test_inside_another_identifier(self):
        for text in ("_U-17", "U-17_x", "xU-17", "9U-17", "U-17a", "U-17_", "my_SRV-174"):
            with self.subTest(text=text):
                self.assertEqual(draftlib.codes(text), [])


if __name__ == "__main__":
    unittest.main()
