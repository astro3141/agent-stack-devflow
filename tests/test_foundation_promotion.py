import copy
import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import dfcore  # noqa: E402
import remote  # noqa: E402


class FakeRemote:
    def __init__(self, commits):
        self.commits = commits

    def commit(self, sha):
        row = self.commits.get(sha)
        return None if row is None else {"parents": row["parents"], "message": "", "author": ""}

    def file_at(self, sha, path):
        return self.commits[sha]["files"].get(path)

    def contains(self, ancestor, descendant):
        todo, seen = [descendant], set()
        while todo:
            cur = todo.pop()
            if cur == ancestor:
                return True
            if cur in seen or cur not in self.commits:
                continue
            seen.add(cur)
            todo.extend(self.commits[cur]["parents"])
        return False

    def changed_files(self, base, head):
        before, after = self.commits[base]["files"], self.commits[head]["files"]
        return {"files": sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))}


class FoundationPromotionTests(unittest.TestCase):
    def setUp(self):
        self.base, self.a, self.b, self.merge = "0" * 40, "a" * 40, "b" * 40, "m" * 40
        self.record_path = ".issue-orchestrator/foundation-pr/T-F-1.json"
        self.writes = [".issue-orchestrator/authority-guard.sh", self.record_path]
        self.task = {"id": "T-F-1", "issue": 40, "scope_files": ["collector/lib/core.sh"]}
        self.promotion = {"record_path": self.record_path, "writes": self.writes,
                          "evidence_paths": [".devflow/tasks/{task}.yaml"], "scope_mode": "exact"}
        record = {"schema": "devflow/foundation-promotion@1", "task": "T-F-1", "issue": 40,
                  "repository": "owner/repo", "canonical_branch": "main",
                  "task_digest": "task-digest", "proposal_digest": "proposal-digest",
                  "canonical_base": self.base, "foundation_commit": self.a,
                  "approved_files": ["collector/lib/core.sh"],
                  "evidence_files": [".devflow/tasks/T-F-1.yaml"],
                  "promotion_files": sorted(self.writes)}
        base_files = {"collector/lib/core.sh": "old\n", ".issue-orchestrator/authority-guard.sh": "old-base\n"}
        a_files = {**base_files, "collector/lib/core.sh": "fixed\n",
                   ".devflow/tasks/T-F-1.yaml": "trace\n"}
        b_files = {**a_files, ".issue-orchestrator/authority-guard.sh": f'EXPECTED_BASE="{self.a}"\n',
                   self.record_path: json.dumps(record, sort_keys=True)}
        self.commits = {
            self.base: {"parents": [], "files": base_files},
            self.a: {"parents": [self.base], "files": a_files},
            self.b: {"parents": [self.a], "files": b_files},
            self.merge: {"parents": [self.base, self.b], "files": dict(b_files)},
        }

    def validate(self, commits=None, merged=False):
        pr = {"head_sha": self.b, "merged": merged,
              "merge_commit_sha": self.merge if merged else None}
        return dfcore.validate_promotion(FakeRemote(commits or self.commits), self.promotion, self.task,
                                         "task-digest", "proposal-digest", pr,
                                         self.merge if merged else self.base, "owner/repo", "main")

    def test_valid_candidate_and_merge(self):
        self.assertTrue(self.validate()["valid"])
        self.assertTrue(self.validate(merged=True)["valid"])

    def test_unapproved_product_file_fails_closed(self):
        commits = copy.deepcopy(self.commits)
        commits[self.a]["files"]["unapproved.txt"] = "x\n"
        self.assertIn("unapproved.txt", " ".join(self.validate(commits)["problems"]))

    def test_non_inert_promotion_file_fails_closed(self):
        commits = copy.deepcopy(self.commits)
        commits[self.b]["files"]["evaluator/check_registry.py"] = "tamper\n"
        self.assertIn("promotion files", " ".join(self.validate(commits)["problems"]))

    def test_squash_or_rebase_merge_is_rejected(self):
        commits = copy.deepcopy(self.commits)
        commits[self.merge]["parents"] = [self.base]
        self.assertIn("merge commit", " ".join(self.validate(commits, merged=True)["problems"]))

    def test_task_branch_exposes_a_and_b_atomically(self):
        fixture = {"repos": {"owner/repo": {
            "default_branch": "main", "branches": {"main": self.base},
            "commits": {self.base: {"parents": [], "files": {"product": "old\n"},
                                      "author": "human", "message": "base"}},
            "pulls": {}, "issues": {}, "check_runs": {}, "reviews": {}}}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=ROOT,
                                         delete=False, encoding="utf-8") as f:
            json.dump(fixture, f)
            path = f.name
        try:
            rem = remote.FixtureRemote(path, "owner/repo")
            author = {"name": dfcore.IMPLEMENTER}
            a = rem.create_commit(self.base, {"product": (b"new\n", "100644")}, "A", author)
            self.assertIsNone(rem.branch_head_or_none("devflow/foundation"))
            b = rem.push_commit("devflow/foundation", a,
                                {"record": (b"envelope\n", "100644")}, "B", author,
                                branch_expected=None)
            self.assertEqual(rem.branch_head("devflow/foundation"), b)
            self.assertEqual(rem.commit(b)["parents"], [a])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
