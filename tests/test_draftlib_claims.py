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

    def test_a_negation_of_something_else_does_not_excuse_it(self):
        # PR #13 review: the negation is of another predicate — the claim stands
        for text, word in (("승인된 작업은 문제가 없음.", "승인된"),
                           ("승인된 범위로 진행해도 문제없음.", "승인된"),
                           ("승인된 작업은 문제없음.", "승인된"),
                           ("This workflow will not change the approved scope.", "approved"),
                           ("We do not need more; it is verified.", "verified"),
                           ("No change is needed because it was confirmed.", "confirmed")):
            self.assertEqual(claimed(text), [word], text)

    def test_negation_must_be_in_the_same_clause_and_close(self):
        # a negation further on, after a clause-joining ending, is about something else
        self.assertEqual(claimed("승인된 작업이며 아직 검증되지 않았다."), ["승인된"])
        self.assertEqual(claimed("승인된 범위. 아님"), ["승인된"])
        self.assertEqual(claimed("Not in scope. Approved by the owner."), ["Approved"])

    def test_code_is_not_prose(self):
        self.assertEqual(claimed("`baseline_status: confirmed`"), [])
        self.assertEqual(claimed("```yaml\nstatus: approved\n```"), [])


O, C = draftlib.SCOPE_OPEN, draftlib.SCOPE_CLOSE


class ScopeSections(unittest.TestCase):
    def test_one_section(self):
        body = (f"text `docs/x.md` outside\n{O}\n- `items/kisa/U-18.yaml` — item\n"
                f"* `tests/t.py` (new)\nnot a list `ignored.md`\n{C}\n")
        self.assertEqual(draftlib.scope_sections(body), ([["items/kisa/U-18.yaml", "tests/t.py"]], []))

    def test_none_empty_and_many(self):
        self.assertEqual(draftlib.scope_sections("no section"), ([], []))
        self.assertEqual(draftlib.scope_sections(f"{O}\n- (없음)\n{C}"), ([[]], []))
        self.assertEqual(draftlib.scope_sections(f"{O}\n- `a`\n{C}\n{O}\n- `b`\n{C}"), ([["a"], ["b"]], []))

    def test_markers_must_pair_in_order(self):
        # PR #13 review: an open marker without its close is no section, and says why
        self.assertEqual(draftlib.scope_sections(f"{O}\n- `items/kisa/U-18.yaml`\n"),
                         ([], [f"{O} is never closed by {C}"]))
        self.assertEqual(draftlib.scope_sections(f"{C}\n- `a`\n{O}\n- `a`\n{C}"),
                         ([["a"]], [f"{C} without {O} before it"]))
        self.assertEqual(draftlib.scope_sections(f"{O}\n- `a`\n{O}\n- `b`\n{C}"),
                         ([["b"]], [f"{O} again before {C}"]))


class HookNotesOnce(unittest.TestCase):
    def test_a_note_said_of_several_files_is_one_line(self):
        msg = "base or head text not given (a new or deleted file, or the engine passed none) — read as a change of meaning"
        files = ["collector/collect.sh", "evaluator/check_registry.py", "evaluator/x.py"]
        notes = [f"{f}: {msg}" for f in files] + ["impacts: none", "docs/elsewhere.md: not a scope file"]
        self.assertEqual(draftlib.per_file_once(notes, files),
                         [f"{msg} — {files}", "impacts: none", "docs/elsewhere.md: not a scope file"])


if __name__ == "__main__":
    unittest.main()
