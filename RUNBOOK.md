# devflow runbook

What to do, in the order you meet it. The README says why devflow is built the way it is; this page
says how to use it and what to do when it waits for you. Everything devflow wants from a person
appears as **one sentence on the task's issue** (the devflow state comment) — this page is the list
of those sentences and what each one asks.

## 1. Once per instance (operator)

| what | where | check |
|---|---|---|
| install the package | `config/packages.local.yaml` + `scripts/packages.sh install devflow` | `scripts/packages.sh verify` |
| create the roles | `scripts/up.sh` (applies `principals.yaml`) | `principals.py list` shows `devflow-implementer`, `devflow-researcher`, `devflow-reviewer`, `devflow-research-reviewer` |
| provision the egress profiles the roles name | the stack's `docker/egress/profiles/<name>.allow` + compose — `closed`, `research`, `research-review` | the broker's `/health` lists them; a role mapped to a missing profile is refused by name |
| GitHub | register a GitHub App (device flow on, token expiry off), put its Client ID in `docker/package.env` as `DEVFLOW_GITHUB_CLIENT_ID`, restart; then panel → 워크플로 실행 → devflow → **연결** | the panel shows 연결됨 |
| shared egress for devflow's scripts | `docker/egress/allow.local` (the instance's own, git-ignored; `up.sh` merges it into `config/generated/egress/allow`): `^github\.com$`, `^api\.github\.com$`, `^codeload\.github\.com$`. Only devflow's scripts call GitHub, through the shared proxy; its model roles' profiles (`closed`, `research`, `research-review`) do not reach GitHub, and need not | `up.sh --check`; `grep github config/generated/egress/allow` |
| the implementer's profile | `config/profiles/long-task.yaml` — claude only, 60-minute calls, `native_allow: [WebSearch, WebFetch]` | `run_workflow.py` refuses an unknown profile |

## 2. Once per project

1. Register it: `config/devflow/projects/<name>.yaml` (repository, canonical branch, `remote: github`).
2. Put `.devflow/` on the project's **canonical** branch: `project.yaml` (policy), the hook, lenses,
   templates, `instructions/`. devflow reads them there; a pull request that edits them is
   proposing rules, and is judged by the ones already there.
3. For devflow to implement: `implement.by: agent` in the policy. For research and survey lanes:
   `implement.modes`.

## 3. Starting a task

1. Open an issue from the project's task template. The ` ```devflow ` block is what devflow reads:
   `id`, `type`, `goal`, `change_class`, `items`, and `implement: agent` (devflow does the work) or
   `implement: human` (a person opens the pull request and names it: `pr: <n>`).
2. Panel → 워크플로 실행 → **devflow-auto**, `project=<registration>`, `issue=<n>` — or
   `run_workflow.py start <id> devflow-auto research-default project=<p> issue=<n> --detach`.
3. Watch the issue: one devflow comment, edited in place, and the commit status `devflow/verdict`
   on each judged head.

`devflow-auto` runs passes until the next move is someone else's, then stops and says whose.
**Starting it again resumes**: every pass reads the task from GitHub.

## 4. Order of work for a new area

An item starts from its own questions; research answers only what an item needs.

1. **item** task — preparation reads the official condition, writes the item analysis (criterion
   and scope, facts relied on, mapping, exceptional outcomes, implementation and verification
   plans) and cites merged research records. A fact no record establishes becomes a research
   request with an ID (`Q-<task>-<n>`); the item waits (`research`) for those questions only.
2. **research** task (`type: research`, `topic: ...`, `questions: [Q-…]`) — facts about systems
   with their sources, and nothing about how the project's code observes them. The record's
   `answers:` / `unanswered:` lines name the questions it settles. Reviewed by two models.
3. When the record is **merged**, the waiting item resumes by itself: an answered question
   releases it (devflow prepares again, then implements); a question the record lists as
   `unanswered:` becomes a decision on the item (scope, another source, or new research).
4. **survey** task (`type: survey`, `items: [...]`) — only when several items share questions,
   gaps or decisions: maps them, and gives an order.

Changing a research task's block (its goal, its questions) makes devflow rewrite its open record
for the task as it now stands.

A research record goes round again only when its items would be worse off: a blocking review
finding on it must name, in `affects`, the question or item condition it endangers — a fact the
items need that is missing, or one they rely on that cannot be trusted. Anything else is advisory:
shown on the issue, merged with the record, no further round. (A person's ` ```devflow-review `
follows the same rule.)

## 5. When it waits for you

The state comment ends with `waiting (<reason>): <need> · owner: <who> · resumes when: <condition>`.

| reason | typical need | what you do |
|---|---|---|
| `decision` | answer the listed decisions | record the answer **in the task block** (the task changes, and devflow prepares again) |
| `research` | a research task that answers the listed question IDs | open (or let the survey open) a research task with `questions: [Q-…]`; merge its record — the item resumes by itself |
| `budget` | the automatic rework rounds are spent (the verdict stays REQUEST_CHANGES) | read the blocking findings; `/devflow approve rework <head sha>` for one more round; or push a fix yourself (the branch becomes yours); or close |
| `budget` | the model review budget or round limit is spent | `/devflow approve reviews <head sha>` for one more model round, or review the head yourself |
| `decision` | a reviewer asks for a decision | answer on the issue, in the task block |
| `decision` | the same blocking condition came back without progress | decide between the implementation and the finding; add evidence, or change the task |
| `decision` | merged outside the workflow / merged head ≠ judged head | review the merged change, or revert |
| `contract_gap` | analyse the gap; is a Foundation needed? | a Foundation task (it has its own gates), or narrow the task |
| `task` | fix the task block or the project configuration | the blocker names the field |
| `implementation` | devflow's next round, or a person | nothing, if `implement: agent` and rounds are left — the next pass acts; otherwise push the fix |
| `implementation` | bring the change onto the canonical head by hand | the same files changed on both sides: merge them yourself |
| `verification` | CI on the head | nothing — devflow-auto waits for the checks itself |
| `verification` | a required check was skipped | make it run, or `/devflow except <check> <head sha> <reason and alternative evidence>` (never for a FAIL) |
| `review` | complete the required reviews | nothing while a model may supply them (devflow-auto waits for an eligible provider); or a person posts a PR review with a ` ```devflow-review ` block for **this** head |
| `merge` | merge pull request #n at exactly <sha> | merge it — agents do not merge; merging another head is reported as NEEDS_DECISION |
| `integration_order` | another change holds the shared artifact | nothing — run again after it lands |
| `environment` | the machinery failed | read the `tool:` lines; fix it; run again (every effect is guarded) |

## 6. Commands (comments on the task's issue)

Counted only from someone the policy lists under `approvers`, who did not implement the change.

| command | effect |
|---|---|
| `/devflow approve <gate> <task digest>` | a gate bound to the task (e.g. `gate1`, `criterion`) |
| `/devflow approve <gate> <head sha>` | a gate bound to a head (e.g. `gate2`) |
| `/devflow approve rework <head sha>` | one more automatic rework round for that head, beyond `loop.max_rework` (and `implement.max_rounds`); spent by that round |
| `/devflow approve reviews <head sha>` | one more round of model review for that head, past `reviews.budget.model_calls` and `reviews.max_rounds`; spent by the round it pays for — each comment is one round (post another when a new devflow or policy version asks for the head's reviews again) |
| `/devflow except <check> <head sha> <reason and alternative evidence>` | a required check that could not run counts, for that head only; never a FAIL |

## 7. Limits that stop automatic work

| limit | policy key | when it is reached |
|---|---|---|
| automatic reworks | `loop.max_rework` | the verdict stays REQUEST_CHANGES, waiting `budget`; `/devflow approve rework` grants one more |
| same blocker, no progress | `loop.max_no_progress` | NEEDS_DECISION |
| implementation rounds (incl. failed ones) | `implement.max_rounds` | devflow stops acting; the reason is on the issue |
| model calls per task | `reviews.budget.model_calls` | waiting `budget`: a person reviews, or `/devflow approve reviews` grants one more model round |
| review rounds per task | `reviews.max_rounds` | waiting `budget`: a person reviews, or `/devflow approve reviews` grants one more model round |
| a pass's wall time | the workflow's `timeout_seconds` | the pass ends; start again |
| one model call | the profile's `execution.timeout_ms` | the call produces nothing; the round is spent |

## 8. Where to look

| question | where |
|---|---|
| what state is the task in | the issue's devflow comment; the head's `devflow/verdict` status |
| what a pass did, step by step | panel → 실행 기록, or `run_workflow.py show <id>` / `tail <id>` |
| what a model was given | `/ws/<conductor run>/devflow/` — `prompt_*.md`, `materials/`, `evaluation.json` |
| what a model did | `/work/evidence/p281/<run>-<label>-<provider>/` — `result.json`, `permissions.jsonl`, `events.jsonl` |
| where a brokered call went out | the profile proxy's log (`docker logs <stack>-egress-<profile>`) |

## 9. Known failure shapes (measured)

| symptom | cause | fix |
|---|---|---|
| every write waits 300 s and expires | a principal rule using `endsWith(...)` | use `contains(...)` (Preloop evaluates it) |
| a review finished but was DENIED at the write | the review file's name is outside the reviewer's write rule (`review_r*`) | reviewer labels must keep the `r` prefix |
| a brokered step refused as `invalid expected` | an old runner without subdirectory paths | stack ≥ `24ab1fb` |
| a long call killed at 15 minutes through the broker | an old `broker_dispatch.py` with a fixed 900 s | stack ≥ `2985815` |
| two passes in the same state, auto stopped | the router held (quota observation rate-limited, or quota spent) | devflow ≥ `6d6b4b7` waits; if quota is spent, start again later |
| WebFetch refused on the role's own hosts | the step did not run in its role's profile | devflow ≥ `785b54a` (its model steps call `broker_dispatch.py`) |
| codex writes a file as `undefined`, or reaches for its own edit tool | codex's own behaviour | keep `long-task` claude-only for implementer and researcher |
