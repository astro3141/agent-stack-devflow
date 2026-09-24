# devflow — the development workflow, as a package

One workflow for every project: **준비 → 구현 → 검증·리뷰 → 통합·출시 → 완료** (개발 범용 워크플로
v0.1). What is common lives here; what is one project's lives in that project's repository.

| | where | decides |
|---|---|---|
| **this package** | `packages/devflow/` | the phases, the four verdicts and when each applies, what makes evidence current, the common review lenses, the guards on every external effect |
| **the execution profile** | `config/profiles/<name>.yaml` (run input `profile`) | which provider may run a model review, under which quota — nothing about the work |
| **the project registration** | `config/devflow/projects/<name>.yaml` | which repository, which canonical branch, where the project's policy is |
| **the project policy and hook** | `.devflow/` in the project's repository, read **at the canonical commit** | official criteria, change classes, checks, shared generated artifacts, its own lenses, who approves, who merges |
| **a task's state** | the GitHub issue (task block + one state comment) and the commit status `devflow/verdict` on the judged head | — |
| **design and reusable knowledge** | Notion | — |

Nothing in this package names a project. The infra scanner's KISA / Item ID / Foundation rules are
in `infra-scanner-io/.devflow/`, not here.

## What a run does

A run is one pass over one task. It reads everything from the remote, advances the task as far as
the evidence allows, publishes the state, and ends. **Resuming a task is running it again** — the
next run starts from GitHub, not from anything this runner kept.

```
evaluate    fix the frame: repository, base/head SHA, canonical head, workflow + policy digests,
            the blobs of the official criteria. Read the policy and run the hook from canonical.
            Classify (hook), gather checks on exactly the head, the trace, the Plan operations,
            the reviews written against this head, the approvals given for it.
route → review_plan → reviews   only when a required review is missing and a model may supply it:
            the platform's route + tasks.py, under the principal devflow-reviewer, which may write
            only its own review_r<n>.json. Each reviewer reads frozen materials, never the candidate.
adjudicate  phase, progress (active / waiting / stopped + reason, need, owner, resume condition),
            execution (OK / TOOL_FAILURE), reviews (complete / incomplete / not_required), verdict.
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

## Using it

**Once per project** — register it, and put `.devflow/` in its repository (see
`infra-scanner-io/.devflow/` for a complete example). For a real remote the operator gives two things
(docs/packages.md, "Credentials of its own"):

```
docker/package.env        DEVFLOW_GITHUB_TOKEN=<fine-grained token: contents read; issues, pull requests, commit statuses read/write>
docker/egress/allow       ^api\.github\.com$
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
| waiting · review | a reviewer posts a PR review on **this head** with a ```` ```devflow-review ```` block (fields: `head_sha`, `policy_digest`, `lenses`, `materials`, `answers`, `findings`, `unverified`, `verdict` — see `prompts/review.md`); or run with `review_mode=model` |
| waiting · decision | a person named in the policy comments `/devflow approve <gate> <task digest \| head sha>`, or records the decision in the task |
| waiting · verification | CI on the head; or `/devflow except <check> <head sha> <reason and alternative evidence>` for a check that could not run — never for a FAIL |
| waiting · merge | the merge owner merges exactly the named head |
| waiting · integration_order | another change holds the shared artifact; run again after it lands |
| waiting · environment | fix what failed (the run named it) and run again — all effects are guarded |

**Interrupted run** — `run_workflow.py resume <id>` continues it from Conductor's checkpoint; or just
start a new run: every step re-reads the remote, and `integrate` / `publish` ask the remote before
acting, so nothing is done twice.

## Evidence rules the controls pin

`controls.py` (87 controls, fixture remotes, no model, no network) drives the real steps:

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
