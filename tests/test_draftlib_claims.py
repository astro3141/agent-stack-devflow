"""Claims of an unverified state, and the change scope an issue body states.

A claim word stays a claim — a draft may not say it is approved, confirmed, verified or ready. A claim
word negated in its own clause ("승인된 범위 아님", "not approved") says what is not so, which is what a
draft is asked to say. Each occurrence is read on its own: one negated mention never excuses another
in the same line, and the whole line is never skipped (U-18 pilot, devflow-draft run draft-u18-gist).
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import draftlib  # noqa: E402


def claimed(text):
    return [c["word"] for c in draftlib.claims(text)]


class Claims(unittest.TestCase):
    def test_affirmative_is_a_claim(self):
        for text in ("범위는 승인됨.", "분류가 확정되었다.", "구현 준비 완료입니다.", "검증 완료.", "검증됨",
                     "승인된 범위: 아래 파일", "The design is confirmed.", "Approved by the owner.",
                     "It was verified on Rocky 9.", "ready to implement"):
            self.assertTrue(claimed(text), text)

    def test_negated_is_not_a_claim(self):
        for text in ("후보 범위(승인된 범위 아님): `items/kisa/U-18.yaml`", "승인되지 않은 초안이다.",
                     "이 범위는 승인된 것이 아니다.", "승인된 것이 아닌 후보 목록", "확정된 사항은 없다.",
                     "검증 완료되지 않음", "이 문서는 초안이며, 승인된 범위가 아닙니다.", "확정된 것이 아닐 수 있다.", "The scope is not approved.", "This has not been verified.",
                     "Nothing here is confirmed.", "never approved", "It isn't confirmed.", "not ready to implement"):
            self.assertEqual(claimed(text), [], text)

    def test_mixed_line_is_still_a_claim(self):
        for text, word in (("후보 범위(승인된 범위 아님), 그러나 D-1은 승인됨.", "승인됨"),
                           ("D-1 승인됨 (승인된 범위 아님)", "승인됨"),
                           ("Not approved, but verified by CI.", "verified"),
                           ("The scope is not approved and the design is confirmed.", "confirmed")):
            self.assertEqual(claimed(text), [word], text)

    def test_negation_must_be_in_the_same_clause_and_close(self):
        # a negation further on, after a clause-joining ending, is about something else
        self.assertEqual(claimed("승인된 작업이며 아직 검증되지 않았다."), ["승인된"])
        self.assertEqual(claimed("승인된 범위. 아님"), ["승인된"])
        self.assertEqual(claimed("Not in scope. Approved by the owner."), ["Approved"])

    def test_code_is_not_prose(self):
        self.assertEqual(claimed("`baseline_status: confirmed`"), [])
        self.assertEqual(claimed("```yaml\nstatus: approved\n```"), [])


class ScopeSections(unittest.TestCase):
    def test_one_section(self):
        body = ("text `docs/x.md` outside\n<!-- devflow:scope -->\n- `items/kisa/U-18.yaml` — item\n"
                "* `tests/t.py` (new)\nnot a list `ignored.md`\n<!-- /devflow:scope -->\n")
        self.assertEqual(draftlib.scope_sections(body), [["items/kisa/U-18.yaml", "tests/t.py"]])

    def test_none_empty_and_many(self):
        self.assertEqual(draftlib.scope_sections("no section"), [])
        self.assertEqual(draftlib.scope_sections("<!-- devflow:scope -->\n- (없음)\n<!-- /devflow:scope -->"), [[]])
        two = "<!-- devflow:scope -->\n- `a`\n<!-- /devflow:scope -->\n<!-- devflow:scope -->\n- `b`\n"
        self.assertEqual(draftlib.scope_sections(two), [["a"], ["b"]])


if __name__ == "__main__":
    unittest.main()
