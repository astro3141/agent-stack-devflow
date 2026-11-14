"""The issue draft generator's deterministic parts, shared by its steps (draft_gather, draft_check).

Nothing here writes to a remote: the steps hand it a remote.ReadOnly, and the functions below only
read what they are given.
"""
import re

import dfcore

DRAFTER = "devflow-drafter"
# @2: the drafter's report separates reference_files (read, cited, never changed) from
# scope_candidates (what the work would change — the only files the project's hook classifies), and
# the issue body states the same scope between SCOPE_OPEN and SCOPE_CLOSE.
# What carries over from @1, and what does not: a @1 *report* (draft.json without reference_files)
# still reads, with no references. A @1 *issue body* has no scope section, and @2 refuses a task
# draft without one — a draft is checked in the run that made it, so an old draft is drafted again,
# never re-checked under the new contract.
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
# A claim word is excused only by a negation of *that word*, in a fixed shape — never by a negation
# that is merely near it ("승인된 작업은 문제가 없음" and "will not change the approved scope" negate
# something else, and stay claims). What the shapes do not cover stays a claim, for a person to read.
#   Korean, after the word:  승인되지 않은 / 검증 완료되지 않음        (its own negative form)
#                            승인된 범위 아님 / 승인된 것이 아닙니다 / 확정된 사항은 없다
#                            (at most one noun, then a word that *starts* with 아니·아닌·아님·아닙·아닐·없)
#   English, right before it: not / never / n't [yet | been | be | formally | fully]* <word>
#                            nothing / none / no <noun> [<word>] is|are|was|were|has been|have been <word>
# Each occurrence is read on its own: one negated mention never excuses another in the same line.
KO_NEG_WORD = re.compile(r"^(?:아니|아닌|아님|아닙|아닐|없)")
KO_SELF_NEG = re.compile(r"^(?:되)?지$")
KO_JOIN = re.compile(r"(?:며|고|지만|는데|으나|면서|어서|아서|해도|하여|하고|로|으로)$")
EN_NEG_BEFORE = re.compile(r"(?:\b(?:not|never)|n't)(?:\s+(?:yet|been|be|formally|fully))*\s+$", re.I)
EN_NONE_BEFORE = re.compile(r"\b(?:nothing|none|no\s+\w+)(?:\s+\w+)?\s+(?:is|are|was|were|has\s+been|have\s+been)\s+$", re.I)
CLAUSE_END = re.compile(r"[,.;:!?()\[\]，。、·—–]")


def _negated(line, m):
    if re.search(r"[가-힣]", m.group(0)):
        rest = CLAUSE_END.split(line[m.end():], maxsplit=1)[0]
        words = rest.split()
        if rest[:1] and not rest[:1].isspace():
            tail = words.pop(0)     # the rest of the word itself: 승인된|, 검증 완료|되지
            if KO_SELF_NEG.match(tail):
                return bool(words) and words[0].startswith("않")
            if KO_NEG_WORD.match(tail):
                return False        # 승인됨아님 — not a shape a person writes; read it
            if not re.fullmatch(r"(?:이|가|은|는|을|를|도|만)", tail):
                return False        # 확정되었|다, 승인됨|이며 … — the word goes on to say something else
        if words and KO_NEG_WORD.match(words[0]):
            return True
        return len(words) > 1 and not KO_JOIN.search(words[0]) and bool(KO_NEG_WORD.match(words[1]))
    before = CLAUSE_END.split(line[:m.start()])[-1]
    return bool(EN_NEG_BEFORE.search(before) or EN_NONE_BEFORE.search(before))


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


def per_file_once(notes, files):
    """The hook's notes, one line per message: "<file>: <message>" said of several scope files
    becomes "<message> — [files]": the same sentence five times tells a person nothing new."""
    out, by = [], {}
    for n in notes:
        path, sep, msg = str(n).partition(": ")
        if sep and path in files:
            if msg not in by:
                by[msg] = []
                out.append(msg)
            by[msg].append(path)
        else:
            out.append(n)
    return [f"{x} — {by[x]}" if x in by else x for x in out]


SCOPE_ITEM = re.compile(r"^\s*[-*+]\s+`([^`\n]+)`")
SCOPE_MARK = re.compile(re.escape(SCOPE_OPEN) + "|" + re.escape(SCOPE_CLOSE))


def scope_sections(text):
    """The change scope the issue body states: (sections, problems). Each section is the list of
    paths between SCOPE_OPEN and its SCOPE_CLOSE, one per list line, the path its first inline code
    ("- `items/kisa/U-18.yaml` — why"). problems names every marker out of place: an open without its
    close, a close without its open, an open inside an open. A section counts only when closed."""
    sections, problems, start = [], [], None
    text = text or ""
    for m in SCOPE_MARK.finditer(text):
        if m.group(0) == SCOPE_OPEN:
            if start is not None:
                problems.append(f"{SCOPE_OPEN} again before {SCOPE_CLOSE}")
            start = m.end()
        else:
            if start is None:
                problems.append(f"{SCOPE_CLOSE} without {SCOPE_OPEN} before it")
                continue
            body = text[start:m.start()]
            sections.append([x.group(1).strip() for x in map(SCOPE_ITEM.match, body.splitlines()) if x])
            start = None
    if start is not None:
        problems.append(f"{SCOPE_OPEN} is never closed by {SCOPE_CLOSE}")
    return sections, problems


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
