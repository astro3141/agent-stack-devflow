"""What must not leave in a gist: draftlib.sensitive finds credentials, keys and private addresses,
names the file, line and kind, and lets the ordinary words of a security draft through."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import draftlib  # noqa: E402


def kinds(line):
    return sorted({f["kind"] for f in draftlib.sensitive({"x.md": line})})


class Sensitive(unittest.TestCase):
    def test_found(self):
        for line, kind in [
            ("ghp_" + "a" * 36, "github token"), ("ghu_" + "B1" * 15, "github token"),
            ("github_pat_" + "11ABC" * 6, "github token"),
            ("sk-" + "x" * 30, "api key"), ("xoxb-1234567890-abc", "api key"),
            ("AKIA" + "ABCDEFGHIJKLMNOP", "aws access key"),
            ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
            ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "jwt"),
            ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "bearer token"),
            ("password: Hunter2Hunter2", "credential assignment"), ("api_key=abcd1234efgh", "credential assignment"),
            ('secret = "s3cr3tvalue"', "credential assignment"),
            ("https://admin:pw12345@host.example/x", "url with credentials"),
            ("10.20.30.40", "private network address"), ("호스트 192.168.0.1 에서", "private network address"),
            ("172.16.5.4", "private network address"),
        ]:
            with self.subTest(line=line):
                self.assertIn(kind, kinds(line))

    def test_ordinary_words_pass(self):
        for line in ("password_complexity 항목 (minlen=8)", "U-02 비밀번호 복잡성", "password: <set by the operator>",
                     "token: ${DEVFLOW_GITHUB_TOKEN}", "secret: ***masked***", "/etc/shadow owner root, max_mode 0400",
                     "버전 1.10.20.3", "8.8.8.8", "172.32.0.1", "canonical e918207f851316eb3ec4f44199cb1692cad17215",
                     "https://github.com/astro3141/infra-scanner-io/issues/21", "the token's account (ghu_ prefix)"):
            with self.subTest(line=line):
                self.assertEqual(kinds(line), [])

    def test_names_where_not_what(self):
        got = draftlib.sensitive({"1-issue.md": "a\nb\npassword: Hunter2Hunter2\n", "2-report.md": "ok"})
        self.assertEqual(got, [{"file": "1-issue.md", "line": 3, "kind": "credential assignment"}])


if __name__ == "__main__":
    unittest.main()
