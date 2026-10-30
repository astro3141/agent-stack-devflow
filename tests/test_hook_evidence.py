"""The hook's classify receives the text of the changed files the policy names, and says which
kinds of evidence the change must show; a hook that still answers only `trace_required` is read
as before."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import dfcore  # noqa: E402

POL = {"hook": {"blob_paths": ["collector", "evaluator/*.py"]}}


class HookBlobs(unittest.TestCase):
    def test_only_named_paths_both_sides(self):
        base = {"collector/lib/core.sh": "a\n", "items/u01.yaml": "x"}
        head = {"collector/lib/core.sh": "a\n# b\n", "items/u01.yaml": "y", "collector/new.sh": "n"}
        got = dfcore.hook_blobs(POL, sorted(set(base) | set(head)), base.get, head.get)
        self.assertEqual(set(got), {"collector/lib/core.sh", "collector/new.sh"})
        self.assertEqual(got["collector/lib/core.sh"], {"base": "a\n", "head": "a\n# b\n"})
        self.assertEqual(got["collector/new.sh"], {"base": None, "head": "n"})

    def test_binary_oversized_and_failing_reads_are_unknown(self):
        big = "x" * (dfcore.HOOK_BLOB_LIMIT + 1)
        def base(path):
            raise RuntimeError("remote down")
        head = {"collector/a.sh": b"\xff\xfe", "collector/b.sh": big, "evaluator/c.py": b"ok"}.get
        got = dfcore.hook_blobs(POL, ["collector/a.sh", "collector/b.sh", "evaluator/c.py"], base, head)
        self.assertEqual(got["collector/a.sh"], {"base": None, "head": None})
        self.assertEqual(got["collector/b.sh"], {"base": None, "head": None})
        self.assertEqual(got["evaluator/c.py"], {"base": None, "head": "ok"})

    def test_no_blob_paths_means_no_blobs(self):
        self.assertEqual(dfcore.hook_blobs({}, ["collector/a.sh"], lambda p: "x", lambda p: "y"), {})


class RequiredEvidence(unittest.TestCase):
    def test_required_evidence_decides_the_trace(self):
        self.assertTrue(dfcore.criteria_mapping_required({"required_evidence": ["criteria_mapping", "regression"]}))
        self.assertFalse(dfcore.criteria_mapping_required({"required_evidence": ["non_executable_change"]}))
        self.assertFalse(dfcore.criteria_mapping_required({"required_evidence": []}))

    def test_trace_required_is_read_when_the_hook_is_older(self):
        self.assertTrue(dfcore.criteria_mapping_required({"trace_required": True}))
        self.assertFalse(dfcore.criteria_mapping_required({"trace_required": False}))
        self.assertFalse(dfcore.criteria_mapping_required({}))


if __name__ == "__main__":
    unittest.main()
