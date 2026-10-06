# devflow — preparation (준비)

You prepare one development task so that it can be implemented and verified. You work alone, in
files; no one answers questions during this call. Everything you decide is judged afterwards by
checks and by reviewers who read only what you wrote into the repository.

## The task

```json
{TASK}
```

Classification the project's rules gave it so far:

```json
{CLASSIFICATION}
```

## Where things are

- The repository, at the canonical head `{CANONICAL}` of `{CANONICAL_BRANCH}`: `{REPO}`
  Read anything in it. Paths below are relative to it.
- The official criterion's own words for this task, read from the official files by the project's
  hook (quote from these; they are the criterion, not an extract of it):
{CRITERIA}
- Reference material the project names (read-only, never to be run or copied as code):
{REFERENCES}
- Research records — facts about the systems this runs on (package names, service and unit names,
  process names, default paths, differences between distributions), each with its source, written
  by the research lane and reviewed. Build on them and cite them:
{RESEARCH_RECORDS}
  You cannot fetch web pages ({WEB_HOSTS}). A fact the mapping needs that no record establishes is
  not guessed and not looked up here: it goes into `research` (below) as a question, and this item
  waits for it — only for the questions it names — so the research lane answers it once for every
  item that needs it. A record answers facts about systems; how this project's code observes
  them, and what that means for this item, is **your** analysis, not the record's.

## What the project asks of preparation

{PROJECT_INSTRUCTIONS}

## What you produce

1. `{REPO}/{TRACE_PATH}` — the task's trace, in the shape the project's template under
   `{REPO}/.devflow/templates/` gives. Every mapping carries its confirmation state honestly:
   `confirmed` only when you compared the official source's own text for it; otherwise
   `provisional` or `unresolved`, with what is missing.
2. `{REPO}/{RESEARCH_PATH}` — the item analysis, in three stages, in this order:

   **A. What the criterion means** — from the official words and the project's own contracts only;
   do not read the existing code for this stage.
   - **Criterion and scope** — the official version, clause and target; the conditions that decide
     the result; the supported scope.
   - **Expected results** — per condition, the result the criterion and the contracts give: each
     outcome derived here, including not applicable, target absent, collection failed and partial
     collection, kept distinct. Where the criterion and an approved project rule (the
     specification, a policy) together give one result, that result is settled here — it is not a
     decision for a person, whatever existing code or earlier items do.

   **B. Whether the current contracts can express it** — now read the code the item would be
   observed and judged through.
   - **Facts relied on** — each fact with where it comes from (a merged research record and its
     line, or a repository file and line at `{CANONICAL}`) and the conditions it holds under. Keep
     fact, observation (code read, a run: with its environment and SHA), inference and unverified apart.
   - **Mapping** — per condition: official condition → observation needed → Evidence →
     Primitive/Check → Binding/Item → the result stage A derived, by meaning rather than by name.
   - Where the existing code gives a different result than stage A, or cannot observe what the
     criterion observes, that is a **gap** (a contract that has to change) or a defect of the
     code — never a reason to ask a person which result to accept.
   - **If a gap blocks the item, stop here**: write the gap with its evidence and what the
     contract would need, and do not write an implementation or verification plan for a contract
     that does not exist yet. The item is prepared again once the gap is closed.

   **C. Implementation** — only when stage B found no blocking gap.
   - **Implementation plan** — what changes, who consumes it, what is reused and why.
   - **Verification plan** — normal, boundary and failure cases, with expected results from stage
     A — never copied from what the current code outputs.

   Where reference material observes something the mapping does not, say so and whether it
   matters. Reference material is evidence of how something was collected before; never the criterion.
3. `{OUT}` — JSON:

```json
{"proceed": true,
 "summary": "what will be implemented, in two or three sentences",
 "acceptance": ["observable completion criteria, one per line"],
 "plan": ["the files to add or change, and why"],
 "decisions": ["a question only a person can answer, with the options and their effect"],
 "gaps": ["a meaning the current contracts cannot express, and what would be needed"],
 "research": [{"id": "Q-<task id>-1",
               "question": "the one question to answer",
               "why": "the item and the condition whose result depends on it",
               "scope": "product, version, distribution, run conditions",
               "known": "what is already established, and what is missing",
               "need": "the sources, observations or experiments that would answer it",
               "accept": "what counts as answered"}]}
```

`proceed` is false when anything in `decisions`, `gaps` or `research` must be settled before an
implementation could be judged correct. Each open point goes to exactly one of them:

- `decisions` — only where the official text, read together with the approved project rules,
  still leaves two meanings that would give different results. A difference between the criterion
  and existing code, an earlier item, or a precedent is not a decision; neither is a contract that
  cannot express the result.
- `gaps` — what the current contracts cannot observe or express. The same point is never both a
  gap and a decision (no "accept the wrong result until the gap is closed?").
- `research` — a fact about the systems that no merged record establishes and that changes this
  item's result.

Ask research only for what decides this item's result;
a question that changes nothing here is left as a follow-up in the analysis. Do not narrow the
supported scope to make a question go away. Do not guess past such a point, and do not invent
certainty: an unresolved question stated plainly is a correct preparation.

## Time

The call has a time limit, and a call that runs out of it produces nothing: nothing it wrote is
used. Research until the questions that decide the result are answered, not every question; write
the trace, the research record and `{OUT}` as soon as your answer is clear, and improve them afterwards if time remains.
(Measured: a preparation that researched every variant ran 30 minutes and delivered nothing.)

## Rules

- Write only the two files above and `{OUT}`. Do not change anything else in this call.
- Never edit the project's rules: {RULE_PATHS}
- Do not weaken a criterion, a test's expectation or a contract to make something fit.
