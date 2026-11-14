# devflow — the development workflow, as a package

One workflow for every project: **준비 → 구현 → 검증·리뷰 → 통합·출시 → 완료** (개발 범용 워크플로
v0.1). What is common lives here; what is one project's lives in that project's repository.

| | where | decides |
|---|---|---|
| **this package** | `packages/devflow/` | the phases, the four verdicts and when each applies, what makes evidence current, the common review lenses, the guards on every external effect, and the implementer: how it prepares, implements and pushes within the project's rules |
| **the execution profile** | `config/profiles/<name>.yaml` (run inputs `profile`, `impl_profile`) | which provider may run a model review or the implementer, under which quota and call limit — nothing about the work |
| **the project registration** | `config/devflow/projects/<name>.yaml` | which repository, which canonical branch, where the project's policy is |
| **the project policy and hook** | `.devflow/` in the project's repository, read **at the canonical commit** | official criteria, change classes, checks, shared generated artifacts, its own lenses, who approves, who merges |
| **a task's state** | the GitHub issue (task block + one state comment) and the commit status `devflow/verdict` on the judged head | — |
| **design and reusable knowledge** | Notion | — |

**Using it: [RUNBOOK.md](RUNBOOK.md)** — setup, starting a task, what each waiting state asks of you,
the commands, the limits, where to look. This page is the design.

Nothing in this package names a project. The infra scanner's KISA / Item ID / Foundation rules are
in `infra-scanner-io/.devflow/`, not here.

## Three workflows

| | |
|---|---|
| `devflow-auto` | what a person starts: passes over one task, one after another, until the next move is a person's (a decision, a gate, the merge), CI's (it waits for the checks on the head, then passes again) or nobody's (no progress). Start it again to resume |
| `devflow` | one pass: judge the task from GitHub, and act on it when devflow may — prepare, implement, push, or hand over |
| `devflow-draft` | before there is a task: a person's request → a checked draft of the task issue, in the run's workspace. Reads only; posts nothing (below) |

## What a pass does

A pass reads everything from the remote, advances the task as far as the evidence allows,
publishes the state, and ends. **Resuming a task is running it again** — the next pass starts from
GitHub, not from anything this runner kept.

```
evaluate    fix the frame: repository, base/head SHA, canonical head, workflow + policy digests,
            the blobs of the official criteria. Read the policy and run the hook from canonical.
            Classify (hook, with the base/head text of the changed files `hook.blob_paths`
            name, so the project can read what changed inside a file, not only which file),
            gather checks on exactly the head, the trace when the hook's `required_evidence`
            names `criteria_mapping` (the one kind the engine verifies itself; the other kinds
            are the classification's reading, shown with the state — the checks the policy
            requires by class and impact stand regardless), the Plan operations,
            the reviews written against this head, the approvals given for it.
route → review_plan → reviews   only when a required review is missing and a model may supply it:
            the platform's route + tasks.py, under the principal devflow-reviewer, which may write
            only its own review_r<n>.json. Each reviewer reads frozen materials, never the candidate.
adjudicate  phase, progress (active / waiting / stopped + reason, need, owner, resume condition),
            execution (OK / TOOL_FAILURE), reviews (complete / incomplete / not_required), verdict.
act         whether devflow's implementer acts now: the policy says `implement.by: agent`, the task
            says `implement: agent`, the verdict sent it to implementation, and rounds are left —
            or, before a gate that binds a proposal, whether the proposer drafts it (below).
work → prepare_agent → proceed → implement_agent → deliver     when it acts (below)
work → propose_agent → deliver                                 a proposal before its gate (below)
integrate   PASS on the head: re-check the head, take the shared-artifact lock, re-check canonical,
            then merge (merge_by: agent) or hand to the merge owner (merge_by: human). After the merge
            and the post-merge checks: release the lock.
publish     the state comment on the issue (edited in place) and devflow/verdict on the head.
record      MLflow, through the platform's recorder: NO_EXECUTION when no model ran, a child run per
            model review when some did.
```

### The four verdicts, and undecided

| verdict | when | goes to |
|---|---|---|
| `PASS` | every required check PASS (or NOT_APPLICABLE with a reason, or NOT_RUN with an approved exception), the trace sound, the Plan's operations as expected, every required review complete **for this head and policy**, independence where required, human gates approved | integrate |
| `REQUEST_CHANGES` | a check FAILs, the trace or Plan contradicts a sufficient contract, a finding **with a basis** blocks, or the hook finds a violation (e.g. an ordinary change touching `collector/`) | implement |
| `CONTRACT_GAP` | preparation or a review finds the needed meaning cannot be expressed with the current contract | prepare (analyse, including whether a Foundation is needed) |
| `NEEDS_DECISION` | a person must choose: a hold label, a provisional or unresolved mapping, an authority document change, the rework / no-progress limit, a merge that is not what was judged | waits for the decision |
| *undecided* | evidence is missing: CI pending, reviews missing or unverified, the machinery failed | waits; a TOOL_FAILURE never becomes a verdict |

A process exit code, a file that exists or a model saying "done" is never evidence by itself.

## Research is a lane of its own

Measured on a real item: preparation spent most of its time finding out how a product is packaged
on the target distributions — facts every item about that product shares — and its most valuable
findings (a contract that cannot observe what the criterion names, a question a person must answer)
reached beyond the item. So a project may route task types to a lane of their own
(`implement.modes`):

| task type | what the lane writes | why first |
|---|---|---|
| `survey` | a set of items mapped onto the contracts: per item the condition and the contract that could express it by meaning; across items the contract gaps (Foundation proposals, with every item they block), the decisions (grouped by question), the research topics, an order | shared gaps and decisions are found once, before items stop on them one by one |
| `research` | one topic — a product on the target distributions: packages, units, processes, configuration, the variants that change a result, what the collection lane sees of each, what earlier scripts looked at — every fact with its source and date | implementers build on it instead of redoing it |

The lane is one model step as **`devflow-researcher`**, whose egress profile (`research`) is the
only devflow one carrying web hosts (agent-stack §53–§55: every devflow model step is brokered into
its role's profile container, and the broker holds the credential); it may write only the project's
research directory (`writes`), and its record is reviewed (lens `research-record`, by
`devflow-research-reviewer` in a profile of its own with the same hosts) and merged like any change.
Item tasks then cite the merged records: their implementer is confined to the providers (profile
`closed`) and fetches nothing — a fact no record establishes stops preparation with
"research needed: …".

## A proposal before Gate1 (제안)

Preparation runs after Gate1, so the thing Gate1 approves — the design, the exact scope — had no
step of its own to be written in; a Foundation's proposal was written by hand (#63). A project may
now have it drafted before the gate (`propose` in its policy):

```yaml
propose:
  by: agent                     # devflow-proposer drafts it
  required_for: [FOUNDATION]    # the pilot: every Foundation's gate1 binds a proposal
  max_rounds: 3                 # drafting rounds, apart from implement.max_rounds
  path: .devflow/proposals/{task}.md
  instructions: .devflow/instructions/propose.md   # optional
human_gates:
  FOUNDATION:
    - {id: gate1, phase: prepare, binds: proposal}
```

Any other task may ask for one (`proposal: true` in its block) where its change class has a
prepare-phase gate that `binds: proposal`; nothing else changes for tasks that do not.

| | what |
|---|---|
| who | `devflow-proposer` (principals.yaml): profile `closed`; may write only `/ws/<run>/proposal/proposal.md` and its report (anchored CEL rules — a `simple` `.contains()` is a substring test in Preloop and let `../` through) — not the checkout, so not the product, the rules, the task block or an approval. deliver also refuses any change to the checkout, and pushes exactly two files |
| where | `devflow-proposal/<task id>`: the proposal and `<path stem>.proposal.json`, devflow's record of it (canonical base, inputs, scope_files, depends_on, decisions, research, round, run, the revision request it answers). Fast-forwarded only, never a pull request — every version a person was shown stays in the branch's history, and the state comment lists every commit pushed and why |
| when | a gate binds a proposal and it is missing, stale, or a person asked for a revision — independent of `implement.by` and `implement: human`. A pass with nothing changed drafts nothing |
| stale | any input changed: the task block (less `scope_files`, `pr`, `issue`, `transaction_commit`), the policy or hook, the pinned criteria and contracts, a file it depends on or proposes to change, the research answering its questions. Judged at canonical's head before the change lands, and at the commit it landed on afterwards — so landing does not make its own approval stale |
| the gate | binds the proposal's commit and both blobs, its canonical base and current inputs, besides what it bound before (task, policy, workflow, hook, approval blobs). Approved by nothing while the proposal is not FRESH, while the task block's `scope_files` differ from the proposal's (a person copies them — the task stays the person's), or while it leaves a decision or research open |
| revise | `/devflow revise proposal <proposal sha>` and what to change on the following lines; answered once |
| limit | `propose.max_rounds`; past it, `/devflow approve propose <inputs digest>` grants one round **for exactly those inputs**: unused when the inputs change, it pays for nothing else. Rounds grants paid for stay counted (`history.proposal.grants_spent`); each comment is one grant |

A hook that requires `scope_files` before anything else (the infra scanner's does, for a
Foundation) receives `proposal_requested` with `classify`, so it can accept their absence until the
proposal supplies them; the engine still holds the gate until the two are equal.

## Drafting the task issue (초안, devflow-draft)

Before a task exists, a person describes the work in their own words; `devflow-draft` turns that
into a draft of the issue — body and ` ```devflow ` block — grounded in the canonical branch and the
existing issues, and checked by the project's real rules. **Read only**: it creates, edits and
comments on no issue, approves nothing, opens no pull request and changes no code. A draft is not
an issue, an approval or permission to run; a person reads it and posts it (or not). The one
write is a secret gist of the draft, made only when the run asks and the project allows it
(`publish`, below).

```
gather ─▶ route ─▶ draft_agent (devflow-drafter) ─▶ check ─▶ publish ─▶ record
```

| step | what |
|---|---|
| `draft_gather.py` | through `remote.ReadOnly` (every write, and every method not listed as a read, raises before it reaches the remote): the canonical head, the policy and hook there, the template (policy `draft.template`, default `.devflow/templates/task.md`), the pinned criteria and contracts, every issue with its block parsed by `parse_task`. Finds without a model the issues the request may duplicate, update or follow: a `#n` it names or a shared item/task code (`strong`), or overlapping words (`similar`); the bodies and comments of the strong ones go to the drafter (devflow's state comment as its sentences, without its JSON marker; a prompt over 190,000 characters is refused) |
| `draft_agent` | one call as `devflow-drafter` (profile `closed`; may write only `/ws/<run>/draft/issue.md` and `/ws/<run>/devflow/draft.json`, anchored CEL like the proposer). Decides new / update / follow_up / duplicate first; reports facts with sources, inferences with what they rest on, unknowns as research / contract_gap / decision, and a class candidate with evidence — or `unresolved` |
| `draft_check.py` | no remote at all: the block by `parse_task` and the policy's vocabulary, a task id already in use, the hook's `classify` with the drafter's `scope_candidates` — what the work would change — as the files (violations refuse; problems, decisions and gaps are open), while `reference_files` — what it only reads or cites — never reach the hook; a file in both lists, a reference not at the canonical commit, or an issue body whose one scope section (`<!-- devflow:scope -->` … `<!-- /devflow:scope -->`) names other files than `scope_candidates` is refused, a class only with evidence at the canonical commit, every fact's source openable (a path, `path:line` or an existing `#n`), every strong candidate addressed and no "new" for work an open issue covers, no claim of a verified or ready state in the prose (confirmed, approved, 확정됨, 준비 완료, ...; each occurrence on its own — one negated in its own clause, such as "승인된 범위 아님" or "not approved", is not a claim), and a checkout the drafter left unchanged |
| `draft_publish.py` | two keys, both required: the run asks (`publish=gist` when it is started — nothing by default, and a project's setting alone never publishes) and the project allows (`draft_publish: gist` in its registration, `config/devflow/projects/<p>.yaml`, the instance's file — not the canonical policy; without it a request is REFUSED). Then the files are scanned before anything leaves — tokens, API and cloud keys, private keys, JWTs, credential assignments, URLs with credentials, private-network addresses — and a finding REFUSES the publish, naming file, line and kind, never the value. Otherwise one **secret** gist of the token's account with `1-issue.md` and `2-report.md`, described by the run, the project, the canonical SHA and the verdict. Never public (there is no way to ask for one), never an issue, nothing in the repository; it never changes the verdict. The link is kept in `/ws/<run>/devflow/draft_publish.json` and the same draft is not published twice. A token that may not make gists is FAILED with what to grant — the GitHub App devflow signs in through needs the account permission *Gists: Read and write*; a lost answer is FAILED and says the gist may exist, without a retry |

The answer is `INVALID` (a check refused it), `NEEDS_INPUT` (a sound draft with open items for a
person) or `DRAFTED` — never "ready". It stays in the run's workspace: `/ws/<run>/draft/issue.md`
and `/ws/<run>/draft/report.md` (canonical SHA, related issues, sources, checks, what is open),
and — when the run asked with `publish=gist` — at the secret gist the run's last line names. A
secret gist is unlisted, not private: anyone who has its link can read it, which is why each run
asks for itself. The report says no issue, pull request or repository was written, and points to
the run's publish record for whether a gist was made.

```
run_workflow.py start <id> devflow-draft long-task project=<p> "request=<what you want done>"
```

A run input carries only letters, digits, `.`, `_`, `-` and space, up to 200 (the stack's rule), so
a request in Korean, with punctuation or longer than that comes as a file in the hand-in directory,
by name — UTF-8, up to 20,000 characters; exactly one of `request` and `request_file`:

```
cp request.md <agent-stack>/handoff/u17-request.md
run_workflow.py start <id> devflow-draft long-task project=<p> request_file=u17-request.md
```

Not in it, on purpose: posting the issue, starting research, branching a Foundation, scheduling
by dependency.

## devflow implements (준비 · 구현)

| stage | who | what |
|---|---|---|
| `work` | script | the tree to change, fetched from GitHub at the commit the evaluation fixed: canonical's head for a new task, the PR's head for a rework, canonical's head plus the change for a rebase; the project's reference material as UTF-8 text; the project's instructions (`implement.instructions`, read at canonical); for a rework, what sent it back — the blockers and what each failing check said about itself |
| `prepare_agent` | model, principal `devflow-implementer` (providers only) | the trace (mappings with honest confirmation states) built on the research records, a note of which facts it relies on and from where, `prepare.json` with `decisions` and `gaps` |
| `proceed` | script | implementation starts only when preparation left no question; otherwise its record is pushed so a person sees it, and the task waits for the decision (NEEDS_DECISION / CONTRACT_GAP in preparation). Answering it in the task block changes the task; the next pass prepares again |
| `implement_agent` | model, `devflow-implementer` | the change and its tests; `implement.json` |
| `deliver` | script | the canonical rules decide what may leave the workspace — the project's rule paths are refused (the task's own trace and research record excepted), the project's hook classifies the change and its path floor refuses what the class may not touch; the project's generators run (`implement.regenerate`); normally **one commit** authored `devflow-implementer` on `devflow/<task id>` (created, or fast-forwarded — never forced; a branch someone else moved is left alone); the pull request, opened once. A named privileged PR flow may add deterministic promotion commit B after product commit A, but only through its canonical promotion command and exact `promotion.writes` set |

The pass then ends waiting for CI on the new head; the next pass judges it like any other change.
Model reviewers are not spent on a head whose checks are still running or already failing.

**Whose change it is.** A commit counts as devflow's only when the published state records devflow
pushing it. Then the implementer is `devflow-implementer`, not the account the push went through —
so the operator can still approve a gate or review it. A commit someone else pushed to the branch
makes them, and the account behind the pull request, implementers too, and devflow stops acting on
that branch.

**Bounds.** `implement.max_rounds` (every round devflow starts for a task, including failed ones),
`loop.max_rework` and `loop.max_no_progress` (in the verdict: past them it is NEEDS_DECISION),
`budget.model_calls` for reviews. Foundation is never implemented by devflow when the project
publishes it through another flow.

### Privileged pull-request promotion

A project can name a flow whose `kind` is `pull_request` and configure `promotion`. Devflow then
publishes product commit A plus an inert promotion commit B on the same task branch. A is first an
unreachable commit object; the branch ref moves only once B exists, so an error cannot expose an
A-only branch or pull request. The record in B
binds A to the proposal digest (task scope + canonical policy/workflow/hook and configured
operational control blobs), canonical base, exact
approved product files and configured evidence paths. Evaluation rejects extra files in A, extra
files in B, a stale canonical base, or a non-merge integration graph. Reviews, required checks and
the head-bound gate run on final PR head B; a person still merges, and post-merge checks run on the
merge commit.

A must be the direct child of the recorded canonical base C. Its full diff is checked; promotion
paths are never subtracted from that diff. B's guard blob must equal A's with only the single
`EXPECTED_BASE` value replaced. `promotion.baseline_path` names that guard.

Rework creates a fresh C→A2→B2 on a revision branch and opens a new PR, closing the exact previous
candidate without deleting its evidence or force-pushing. Every new B needs fresh checks, review and
Gate2. After merge, proposal inputs are read at M's first parent C; the workflow identity comes from
the same B's published PASS with all gates approved. The newly promoted guard cannot invalidate its
own approval, and a PASS still waiting for Gate2 cannot authorize post-merge completion.

This proves transaction shape, not that a human physically operated an account. Devflow reads the
GitHub author of an approval comment and rejects an implementer identity, but GitHub cannot tell
whether that account acted in the UI or through a credential available to another process. Required
checks and reviews are server-enforced only when the repository plan and branch rules provide them;
`integrate` reports the rules it could observe.

`foundation-controls` runs the standalone tests and the full controls against the pinned public
agent-stack checkout (`e805b2b5514a26c2770521549c2597f6653b75f2`) in a Python container. The
registration and research hosts in `tests/` configure only that disposable fixture instance. The
Foundation scenarios drive the real evaluate/adjudicate/act/work/deliver/review/integrate steps;
model output, GitHub checks, approval comments and the human merge are simulated. Live model and
GitHub publication validation remains a separate activation check, never inferred from fixture PASS.

## GitHub, from the panel

Under a devflow workflow the panel shows **연결** (the package's login, OPERATIONS §44). Press it,
open the page it shows, type the code there. `steps/github_login.py` runs GitHub's device flow and
writes `token.json` into the login's own directory; nothing is typed into the panel, and the
platform never opens the file. A token that expires is refreshed before it does (`lib/ghauth.py`),
so a person logs in again only if devflow has not run for six months.

Once, before that: register an app with device flow enabled and put its Client ID (not a secret) in
`docker/package.env` as `DEVFLOW_GITHUB_CLIENT_ID`.

- **GitHub App** (recommended): Settings → Developer settings → GitHub Apps → New. Enable Device
  Flow; turn *Expire user authorization tokens* off for a token that never expires (left on, it is
  refreshed); no webhook. Repository permissions: Contents, Issues, Pull requests, Commit statuses —
  read and write; Checks — read. Install it on the project's repository only.
- **OAuth App**: fewer fields, a token that does not expire, but the `repo` scope reaches every
  repository the account can.

`DEVFLOW_GITHUB_TOKEN` in `docker/package.env` (a fine-grained token, which may be set never to
expire) still works instead. `docker/egress/allow.local` — the instance's own list, git-ignored,
which `scripts/up.sh` merges into the shared proxy's — needs `^github\.com$`, `^api\.github\.com$`
and `^codeload\.github\.com$` (the tree archives). Only devflow's scripts call GitHub, through the
shared proxy; the egress profiles its model roles run in do not reach GitHub.

## Using it

**Once per project** — register it, and put `.devflow/` in its repository (see
`infra-scanner-io/.devflow/` for a complete example). For a real remote the operator gives two things
(docs/packages.md, "Credentials of its own"):

```
docker/package.env        DEVFLOW_GITHUB_TOKEN=<fine-grained token: contents read; issues, pull requests, commit statuses read/write>
docker/egress/allow.local ^github\.com$ ^api\.github\.com$ ^codeload\.github\.com$   (git-ignored; up.sh merges it)
scripts/up.sh             applies principals.yaml (devflow-reviewer) and restarts the agent
```

**Start a task** — open an issue from the project's task template (the ```` ```devflow ```` block),
then:

```bash
docker exec cadp278-agent /opt/venv/bin/python /work/stack/run_workflow.py \
  start task-12-a devflow research-default project=infra-scanner issue=12 --detach
docker exec cadp278-agent /opt/venv/bin/python /work/stack/run_workflow.py tail task-12-a --follow
```

Inputs: `project` (a registration), `issue`, `review_mode` (`model` — a model may supply a missing
review; `external` — only reviews already on GitHub count), `reviewer` (principal, default
`devflow-reviewer`), `fixture` (fixture projects only).

**See the state** — on the issue (the devflow comment) and on the head commit (`devflow/verdict`);
or `run_workflow.py show <id>` for the run itself.

**Respond to a verdict**, then run again (a new run id; ids are used once):

| state | what a person or implementer does |
|---|---|
| waiting · implementation | open the PR / push the fix; name the PR in the task block (`pr: 7`) |
| waiting · review | a reviewer posts a PR review on **this head** with a ```` ```devflow-review ```` block (fields: `head_sha`, `policy_digest`, `basis_digest` — all three from the state comment — `lenses`, `materials`, `answers`, `findings`, `unverified`, `verdict` — see `prompts/review.md`); or run with `review_mode=model` |
| waiting · decision | a person named in the policy comments `/devflow approve <gate> <task digest \| head sha>`, or records the decision in the task |
| waiting · verification | CI on the head; or `/devflow except <check> <head sha> <reason and alternative evidence>` for a check that could not run — never for a FAIL |
| waiting · merge | the merge owner merges exactly the named head |
| waiting · integration_order | another change holds the shared artifact; run again after it lands |
| waiting · environment | fix what failed (the run named it) and run again — all effects are guarded |

**Interrupted run** — `run_workflow.py resume <id>` continues it from Conductor's checkpoint; or just
start a new run: every step re-reads the remote, and `integrate` / `publish` ask the remote before
acting, so nothing is done twice.

## Review settings a project chooses (`reviews:` in its policy)

| key | default | what it does |
|---|---|---|
| `assignment` | `together` | `per_lens`: one model reviewer per lens, each reading only its lens's `read` list. A time and cost choice — one reviewer carrying four lenses with contracts and implementation ran past a 600 s profile limit (measured) |
| `retries` | `0` | a model reviewer that failed or timed out (never one that was denied) runs again, through the platform's member retry; every attempt counts against `budget.model_calls` |
| `budget.model_calls` | `6` | model calls per task, across its runs; when spent, assignments go to a person |
| `independent_for`, `independent_by` | `[FOUNDATION]`, `[model, human]` | which change classes need a reviewer separate from every implementer identity, principal and session, and who may be that reviewer |
| `max_file_bytes` | `60000` | the largest material shown; anything larger is named in `not_shown` and its lens stays unverified |

A review separates **`unverified`** — a question of its lens it could not answer from the materials;
the lens stays open — from **`limits`** — how far an answer reaches, e.g. relying on a result in
`checks.json`; shown, never a gap.

## A first real item (infra-scanner-io SRV-174, 2026-09-25)

Run through the live runner on the real trees (#13 + #14 as if merged, #15 on top), their real CI
(8/8, including the container smoke and the trace-quote test) and real model reviews. Four runs, each
exposing something the fixtures could not:

1. every machine check PASS, **undecided** — the reviewer could not verify what its materials lacked
   (the official workbook, the resolver and collector code, any execution result) → checks.json and an
   `implementation` material kind; trace quotes proven against the official files in CI;
2. **undecided** — one reviewer carrying every lens timed out at 600 s → `assignment: per_lens`;
3. **REQUEST_CHANGES** from one reviewer, caused by the trial's own fixture omitting `items/_raw_*` (a
   lossy observation — the reviewer was right about what it was shown); one reviewer timed out →
   `retries`; honest reviewers could never complete → `unverified` vs `limits`;
4. **NEEDS_DECISION**, no blocking finding: the criteria-trace reviewer asks a person to decide — the
   official 판단방법 is `ps -ef | grep named`, which the service_state process channel cannot observe;
   an enabled-but-stopped unit reads REVIEW; absence reads NA; scope is BIND only. Posted on the task.

## Evidence rules the controls pin

`controls.py` (fixture remotes, no model, no network) drives the real steps:

- a review counts only for the head GitHub attaches it to, the head it names, and the policy digest
  it was written under; a new head, a policy change or a criterion change on canonical makes it stale
- a model review counts only when **this attempt** produced it, with the hash the receipt recorded,
  for this frame; a file an earlier attempt left is not evidence
- independence is an identity separate from every implementer identity, principal and session — a
  different model or vendor is never counted
- a lens whose required material was not read (including material too large to show) is
  *unverified*: it holds PASS and invents no finding; a blocking claim without a basis does not block
- the shared Plan: canonical must be contained in the head, the operation difference must equal the
  declared set, count assertions move only with it; one change holds the lock at a time
- an external effect whose answer was lost is found on the remote and not repeated
- the candidate's own edits to `.devflow/` are judged by the canonical rules and flagged
- the repetition limits (project `loop.max_rework`, `loop.max_no_progress`) stop automatic rounds
  and hand over to a person; the model review budget is per task

```bash
docker exec cadp278-agent /opt/venv/bin/python /work/packages/devflow/controls.py
docker exec cadp278-agent /opt/venv/bin/python /work/packages/devflow/fixtures/build.py write
```

## Sources

개발 범용 워크플로 v0.1, 범용 프로젝트 설정표, 범용 작업 템플릿; 인프라 스캐너 Project Profile v0.2,
설정표, 작업 템플릿; CADP Review Lens Library (lenses marked `cadp-*`, conditional) — Notion,
read 2026-09-25. The CADP library's "Spec > TD > Trace" is not used as a rule over execution
observations, and its K2/K4/K7 grades are not carried into common rules (`lenses/library.yaml`).

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 astro3141.
