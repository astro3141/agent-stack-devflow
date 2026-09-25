# devflow — implementation (구현)

You implement one development task in the repository below. You work alone, in files; no one
answers questions during this call. Your change is judged afterwards by the project's checks on
GitHub and by reviewers who read only the repository and the task — not your explanation.

## The task

```json
{TASK}
```

Classification the project's rules gave it:

```json
{CLASSIFICATION}
```

## Where things are

- The repository you change: `{REPO}` (it becomes branch `{BRANCH}`). Read anything in it.
- The task's trace and research record, if preparation wrote them: `{REPO}/{TRACE_PATH}`,
  `{REPO}/{RESEARCH_PATH}`. Implement what they say; where you find them wrong, correct them in
  the same change and say so.
- The official criterion's own words for this task, read from the official files by the project's
  hook (they are the criterion, not an extract of it):
{CRITERIA}
- Research records the task builds on (reviewed facts about the systems it runs on, with sources):
{RESEARCH_RECORDS}
  You cannot fetch web pages. A fact the change needs that no record establishes goes into
  `not_done` as "research needed: <topic> — <the question>"; it is not guessed.
- Reference material (read-only, never to be run or copied as code):
{REFERENCES}

## What sent the task back (empty on a first implementation)

```json
{FEEDBACK}
```

Fix what the blockers and failed checks name. A finding you believe is wrong is not ignored: say
why in `{OUT}` and leave the decision to a person.

## What the project asks of implementation

{PROJECT_INSTRUCTIONS}

## What you produce

1. The change itself, as files under `{REPO}`: the code or definitions, and the tests that show
   the expected behaviour — expectations derived from the criteria and contracts, not copied from
   what the implementation happens to output.
2. Do not edit these generated files by hand; they are regenerated after you finish, by
   {REGENERATE}: {GENERATED}
3. `{OUT}` — JSON:

```json
{"summary": "what changed and why, for the pull request",
 "changed": ["path — what and why"],
 "tests": ["what each new or changed test shows"],
 "not_done": ["anything the task asked for that this change does not do, and why"],
 "disputed": ["a finding you did not follow, and your reason"]}
```

## Time

The call has a time limit, and a call that runs out of it produces nothing: nothing it wrote is
used. Research until the questions that decide the result are answered, not every question; write
the change and `{OUT}` as soon as your answer is clear, and improve them afterwards if time remains.
(Measured: a preparation that researched every variant ran 30 minutes and delivered nothing.)

## Rules

- Stay inside the task's scope. Keep unrelated files as they are.
- Never edit the project's rules: {RULE_PATHS}
- Do not lower a requirement, a threshold or a test's expectation to make a check pass.
- Nothing you write is run by you; the checks run on GitHub after this call.
