"""The issue draft generator's deterministic parts, shared by its steps (draft_gather, draft_check).

Nothing here writes to a remote: the steps hand it a remote.ReadOnly, and the functions below only
read what they are given.
"""
import re

import dfcore

DRAFTER = "devflow-drafter"
# @2: the drafter's report separates reference_files (read, cited, never changed) from
# scope_candidates (what the work would change — the only files the project's hook classifies), and
# the issue body states the same scope between SCOPE_OPEN and SCOPE_CLOSE. A @1 report (no
# reference_files) still reads: its references are none.
DRAFT_SCHEMA = "devflow/draft@2"
SCOPE_OPEN = "<!-- devflow:scope -->"
SCOPE_CLOSE = "<!-- /devflow:scope -->"
TEMPLATE_DEFAULT = ".devflow/templates/task.md"
RELATIONS = ("new", "update", "follow_up", "duplicate")
UNKNOWN_KINDS = ("research", "contract_gap", "decision")
# an identifier-like code: an item (U-37, SRV-174), a task id (T-0054, T-F-SVCRUN)
# bounded by ASCII letters, digits and underscore on both sides, not by \b: a Korean particle is a
# word character, and "U-17을" or "#21번" must still name U-17 and #21 — while "#21abc", "#21_x",
# "_U-17" or "U-17_x" are part of some other identifier and name nothing
CODE = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]{0,7}(?:-[A-Z0-9]{1,10})+(?![A-Za-z0-9_])")
ISSUE_REF = re.compile(r"(?<![A-Za-z0-9_/])#(\d{1,6})(?![A-Za-z0-9_])")
WORD = re.compile(r"[0-9A-Za-z가-힣_]{2,}")
# words that claim a state only a verification or a person can give (requirement: no "confirmed" or
# "ready" without it). Matched in the draft's prose, outside code fences.
CLAIM = re.compile(r"(?i)\b(confirmed|verified|ready to (?:implement|merge|start)|approved)\b"
                   r"|확정(?:됨|되었|했|된)|검증(?:됨|되었|\s*완료)|준비\s*완료|승인(?:됨|되었|된)")
# A claim word negated in its own clause says what is NOT so ("승인된 범위 아님", "검증 완료되지 않음",
# "not approved", "has not been verified") — that is the honest wording the drafter is asked for, not a
# claim. Each occurrence is read on its own: a negated one does not excuse another in the same line
# ("승인된 범위 아님, D-1은 승인됨" is still a claim). The negation must be close and in the same
# clause — punctuation and a clause-joining ending or conjunction end the clause.
CLAUSE_END = re.compile(r"[,.;:!?()\[\]，。、·—–]|\s(?:but|however|although|though|whereas|while|and|so)\b"
                        r"|\s(?:그러나|하지만|그런데|그리고|다만)(?=\s|$)", re.I)
# 아니·아닌·아님·아닙(니다)·아닐·아냐: every form of 아니다 — Hangul syllables differ by their final consonant
KO_NEG = re.compile(r"아[니닌님닙닐냐]|않|없|못|^안$|^전(?:이|에|$)")
KO_JOIN = re.compile(r"(?:며|고|지만|는데|으나|면서|어서|아서)$")
EN_NEG = re.compile(r"^(?:not|never|no|nothing|none|neither|nor|without|cannot|yet-to-be)$|n't$", re.I)


def _negated(line, m):
    if re.search(r"[가-힣]", m.group(0)):
        # Korean negates after the word: the rest of its own word, then at most two more words
        rest = CLAUSE_END.split(line[m.end():], maxsplit=1)[0]
        words = rest.split()  # the first is the rest of the word itself when it goes on ("확정되었|다")
        for w in words[:3]:
            if KO_NEG.search(w):
                return True
            if KO_JOIN.search(w):
                return False
        return False
    # English negates before the word, within the last four words of its clause
    before = CLAUSE_END.split(line[:m.start()])[-1].split()
    return any(EN_NEG.search(w.strip("\"'*_")) for w in before[-4:])
FENCE = re.compile(r"```.*?```", re.S)
INLINE = re.compile(r"`[^`\n]+`")


def codes(text):
    return sorted(set(CODE.findall(text or "")))


def words(text):
    return {w.lower() for w in WORD.findall(text or "")}


def task_codes(task, title):
    """The codes an existing task is about: its id, items and topic, and its title."""
    got = set(codes(title))
    if task:
        for k in ("id", "topic"):
            got |= set(codes(str(task.get(k) or "")))
            if task.get(k):
                got.add(str(task[k]))
        for k in ("items", "affected_items", "depends_on"):
            v = task.get(k) or []
            for x in (v if isinstance(v, list) else [v]):
                got |= set(codes(str(x))) | {str(x)}
    return got


def summarize(issue):
    """One existing issue, as the drafter and the checks see it."""
    task, why = dfcore.parse_task(issue)
    row = {"number": issue["number"], "state": issue["state"], "title": issue.get("title") or "",
           "labels": issue.get("labels") or []}
    if task:
        row["task"] = {k: task.get(k) for k in ("id", "type", "goal", "change_class", "items", "affected_items",
                                                "topic", "implement", "pr", "depends_on") if task.get(k) not in (None, "", [])}
    else:
        row["task_problem"] = why
    row["codes"] = sorted(task_codes(task, row["title"]))
    return row


def candidates(request, rows, limit=8):
    """Existing issues a request may duplicate, update or follow — found without a model.

    strong: the request names the issue (#n) or a code the issue's task is about.
    similar: the words of the request and of the issue's title and goal overlap (Jaccard >= 0.2).
    """
    req_codes = set(codes(request))
    refs = {int(n) for n in ISSUE_REF.findall(request or "")}
    rw = words(request)
    out = []
    for row in rows:
        why = []
        if row["number"] in refs:
            why.append(f"the request names #{row['number']}")
        shared = sorted(req_codes & set(row["codes"]))
        if shared:
            why.append(f"shared codes {shared}")
        tw = words(row["title"] + " " + str((row.get("task") or {}).get("goal") or ""))
        sim = len(rw & tw) / len(rw | tw) if rw and tw else 0.0
        if why:
            out.append({"number": row["number"], "state": row["state"], "strength": "strong", "why": why,
                        "similarity": round(sim, 3)})
        elif sim >= 0.2:
            out.append({"number": row["number"], "state": row["state"], "strength": "similar",
                        "why": [f"word overlap {sim:.2f}"], "similarity": round(sim, 3)})
    out.sort(key=lambda c: (c["strength"] != "strong", c["state"] != "open", -c["similarity"], c["number"]))
    return out[:limit]


BODY_MAX = 6000


def body_excerpt(body):
    """An existing issue's body as the drafter reads it: the first BODY_MAX characters, marked when cut."""
    body = body or ""
    return body if len(body) <= BODY_MAX else body[:BODY_MAX] + f"\n\n[… {len(body) - BODY_MAX} more characters not shown]"


COMMENTS_MAX = 8000


def readable(body):
    """A comment as people read it: devflow's state marker (machine JSON) cut out."""
    body = body or ""
    i = body.find(dfcore.STATE_MARK)
    if i < 0:
        return body
    j = body.find("-->", i)
    return (body[:i] + "[devflow's machine state omitted]" + (body[j + 3:] if j >= 0 else "")).strip()


def comments_excerpt(comments):
    """An issue's comments as the drafter reads them, newest last, cut to COMMENTS_MAX characters
    from the newest end (what was decided last matters most)."""
    if not comments:
        return ""
    parts = [f"**comment by {c.get('user') or '?'}**\n\n{c.get('body') or ''}" for c in comments]
    text, used = [], 0
    for p in reversed(parts):
        if used + len(p) > COMMENTS_MAX:
            text.append(f"[… {len(parts) - len(text)} earlier comment(s) not shown]")
            break
        text.append(p)
        used += len(p)
    return "\n\nComments:\n\n````markdown\n" + "\n\n---\n\n".join(reversed(text)) + "\n````"


def prose(text):
    """The draft's prose: what is outside code fences and inline code (a claim word quoted from a file,
    in YAML or in code is not a claim)."""
    return INLINE.sub("", FENCE.sub("", text or ""))


def claims(text):
    """Lines of prose that claim a verified or ready state."""
    out = []
    for n, line in enumerate(prose(text).splitlines(), 1):
        for m in CLAIM.finditer(line):
            if not _negated(line, m):
                out.append({"line": line.strip()[:200], "word": m.group(0)})
                break
    return out


SCOPE_ITEM = re.compile(r"^\s*[-*+]\s+`([^`\n]+)`")


def scope_sections(text):
    """The change scope the issue body states: one list per SCOPE_OPEN … SCOPE_CLOSE section, each
    item's path the first inline code of a list line ("- `items/kisa/U-18.yaml` — why")."""
    out, i = [], 0
    text = text or ""
    while True:
        a = text.find(SCOPE_OPEN, i)
        if a < 0:
            return out
        b = text.find(SCOPE_CLOSE, a)
        body = text[a + len(SCOPE_OPEN): b if b >= 0 else len(text)]
        out.append([m.group(1).strip() for m in map(SCOPE_ITEM.match, body.splitlines()) if m])
        if b < 0:
            return out
        i = b + len(SCOPE_CLOSE)


# what must not leave in a gist. Deterministic and deliberately broad: a refused publish costs a
# person a look; a published credential cannot be taken back.
SENSITIVE = [
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("api key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}")),
    ("credential assignment", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|credential)s?\b\s*[:=]\s*"
        r"[\"']?(?![<{$]|\*{3})[^\s\"'`,;)]{8,}")),
    ("url with credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
    ("private network address", re.compile(
        r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])")),
]


def sensitive(files):
    """[{file, line, kind}] for every line that looks like something that must not leave."""
    found = []
    for name in sorted(files):
        for n, line in enumerate(files[name].splitlines(), 1):
            for kind, pat in SENSITIVE:
                if pat.search(line):
                    found.append({"file": name, "line": n, "kind": kind})
    return found
