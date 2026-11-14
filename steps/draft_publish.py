"""devflow step: put a checked draft where a person can read it on GitHub — a secret gist, when
this run asks for one and the project allows it. The only write devflow-draft makes.

usage: draft_publish.py <project> <verdict> [<publish>]

  project   a registration under config/devflow/projects/<project>.yaml
  verdict   check's verdict (INVALID / NEEDS_INPUT / DRAFTED); "" when check did not run
  publish   this run's request: "gist", or "" for none (the workflow input `publish`)

Two keys, both required, because a secret gist is unlisted, not private — anyone with its link can
read it, and a draft of a private repository's work may carry that repository's content:
  * the run asks: `publish=gist` when it is started. Nothing is published by default, and a
    project's setting alone never publishes anything.
  * the project allows: `draft_publish: gist` in its registration (the instance's own file, not the
    project's canonical policy). Without it a request is refused.
Then the files are scanned before anything leaves (credentials, tokens, private keys, credential
assignments, private-network addresses): a finding refuses the publish and names the file, line
and kind — never the value. Otherwise one secret gist of the token's account carries
draft/issue.md (when the drafter wrote one) and draft/report.md, named by the run, the project, the
canonical SHA and the verdict. A gist is not an issue: nothing in the repository changes, and
nothing devflow reads ever reads it back.

Never changes the draft's outcome. A gist that could not be made is reported (status FAILED, with
the reason and, for a token that may not make gists, what to grant) and the run ends as check said.

What a repeat does: "no-op" for the same draft — devflow/draft_publish.json keeps the link and the
digest of what it carries, and the same files are not published twice. A changed draft gets a new
gist. A response lost in transit may still have made one; the step says so rather than retry.
"""
REPEATABLE = "no-op"
import json, os, sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PKG, "lib"))
import dfcore  # noqa: E402
import draftlib  # noqa: E402
import step  # noqa: E402

WS = dfcore.workspace()
D = os.path.join(WS, "devflow")
DRAFT = os.path.join(WS, "draft")
RECORD = os.path.join(D, "draft_publish.json")
MODES = ("", "none", "gist")
REQUESTS = ("", "none", "gist")
GRANT_HINT = ("the GitHub App devflow signs in through needs the account permission 'Gists: Read "
              "and write' (GitHub → Settings → Developer settings → GitHub Apps → the App → "
              "Permissions); accept the change on GitHub, then publish again — or set "
              "draft_publish: none")


def emit(**kw):
    for k in ("url", "gist", "error", "hint"):
        kw.setdefault(k, "")
    step.out(**kw)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def main(argv):
    if len(argv) < 1:
        emit(status="FAILED", error="usage: draft_publish.py <project> <verdict> [<publish>]")
        return 0
    project, verdict = argv[0], (argv[1] if len(argv) > 1 else "").strip()
    request = (argv[2] if len(argv) > 2 else "").strip().lower()
    if request not in REQUESTS:
        emit(status="FAILED", error=f"publish={request!r} is not one of {list(REQUESTS[1:])} — there is no public gist")
        return 0
    if request != "gist":
        emit(status="SKIPPED", error="this run did not ask to publish (publish=gist)")
        return 0
    reg = dfcore.registration(project)
    mode = str(reg.get("draft_publish") or "").strip().lower()
    if mode not in MODES:
        emit(status="FAILED", error=f"draft_publish: {mode!r} is not one of {list(MODES[1:])}",
             hint=f"in {reg['_path']}")
        return 0
    if mode != "gist":
        emit(status="REFUSED", error=f"this run asked to publish, and project {project!r} does not allow it",
             hint=f"draft_publish: gist in {reg['_path']} allows it (the operator's choice)")
        return 0
    report = read(os.path.join(DRAFT, "report.md"))
    if not verdict or report is None:
        emit(status="SKIPPED", error="there is no checked draft in this run to publish")
        return 0
    files = {"2-report.md": report}
    issue = read(os.path.join(DRAFT, "issue.md"))
    if issue is not None and issue.strip():
        files["1-issue.md"] = issue
    found = draftlib.sensitive(files)
    if found:
        where = ", ".join(f"{f['file']}:{f['line']} ({f['kind']})" for f in found[:12])
        emit(status="REFUSED", error=f"not published — {len(found)} line(s) look sensitive: {where}",
             hint="read those lines in /ws/<run>/draft/; nothing left the workspace")
        return 0
    digest = dfcore.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False))[:16]
    try:
        prev = json.load(open(RECORD, encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        prev = {}
    if prev.get("digest") == digest and prev.get("url"):
        emit(status="OK", url=prev["url"], gist=prev.get("gist", ""),
             error="already published (the same draft)")
        return 0

    try:
        ctx = json.load(open(os.path.join(D, "draft_context.json"), encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        ctx = {}
    canonical = str(ctx.get("canonical") or "")[:12]
    run = step.stack_run_id() or step.run_id()
    description = (f"devflow-draft {run} · {project} · canonical {canonical or '?'} · {verdict}"
                   " — a draft, not an issue")
    try:
        rem = dfcore.open_remote(reg, WS, dfcore.egress_env())
        got = rem.create_gist(description, files)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:400]
        lost = "response was lost" in msg or "Transient" in type(e).__name__
        emit(status="FAILED", error=msg,
             hint=(GRANT_HINT if "not accessible by integration" in msg else
                   "the gist may have been made before the answer was lost — look in your gists"
                   " before publishing again" if lost else ""))
        return 0
    rec = {"url": got.get("url", ""), "gist": got.get("id", ""), "digest": digest, "run": run,
           "verdict": verdict, "canonical": canonical, "files": sorted(files), "secret": True}
    tmp = RECORD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RECORD)
    emit(status="OK", url=rec["url"], gist=rec["gist"], error="published: " + ", ".join(sorted(files)))
    return 0


if __name__ == "__main__":
    # a crash is still an answer (TOOL_FAILURE, under `error`), and still does not change the draft's outcome
    sys.exit(step.main(lambda: main(sys.argv[1:]), failure_key="error", url="", gist="", hint=""))
