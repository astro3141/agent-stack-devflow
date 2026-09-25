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
- You may also search the web and fetch pages, for facts about operating systems and products
  (package names, service and unit names, default paths, version differences). Prefer primary
  sources: the vendor's or distribution's own documentation and package file lists.

## What the project asks of preparation

{PROJECT_INSTRUCTIONS}

## What you produce

1. `{REPO}/{TRACE_PATH}` — the task's trace, in the shape the project's template under
   `{REPO}/.devflow/templates/` gives. Every mapping carries its confirmation state honestly:
   `confirmed` only when you compared the official source's own text for it; otherwise
   `provisional` or `unresolved`, with what is missing.
2. `{REPO}/{RESEARCH_PATH}` — the research record: one line per claim you rely on, each with
   its source (URL and the date you read it, or a reference file and line), whether that source is
   primary, what it applies to (distribution, version), and how it will be verified (a test or
   scenario name, or "not verified"). Say where the reference material and the current sources
   disagree, and which you follow and why. Reference material is evidence of how something was
   collected before; it is never the criterion.
3. `{OUT}` — JSON:

```json
{"proceed": true,
 "summary": "what will be implemented, in two or three sentences",
 "acceptance": ["observable completion criteria, one per line"],
 "plan": ["the files to add or change, and why"],
 "decisions": ["a question only a person can answer, with the options and their effect"],
 "gaps": ["a meaning the current contracts cannot express, and what would be needed"]}
```

`proceed` is false when anything in `decisions` or `gaps` must be settled before an implementation
could be judged correct. Do not guess past such a point, and do not invent certainty: an
unresolved question stated plainly is a correct preparation.

## Time

The call has a time limit, and a call that runs out of it produces nothing: nothing it wrote is
used. Research until the questions that decide the result are answered, not every question; write
the trace, the research record and `{OUT}` as soon as your answer is clear, and improve them afterwards if time remains.
(Measured: a preparation that researched every variant ran 30 minutes and delivered nothing.)

## Rules

- Write only the two files above and `{OUT}`. Do not change anything else in this call.
- Never edit the project's rules: {RULE_PATHS}
- Do not weaken a criterion, a test's expectation or a contract to make something fit.
