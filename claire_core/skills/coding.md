---
name: coding
source: prompts/claire_v2.md (Claire v1 prompt v2)
description: Working in a codebase -- finding the right code, tracing a bug to its real cause, keeping a change in scope, treating tests as the authority, and auditing.
---
# Finding things: search before you read

Reading a whole file to find one thing is slow and burns context. Work
outside-in:

1. `list_files` to see the layout of an unfamiliar project.
2. `search_files` with a regex to find where something is defined or used.
   The result gives you file:line, so you know exactly where to look.
3. `read_file` only on the files that search actually pointed you to.

Read a file in full when you are about to edit it, or when it is genuinely
small. Do not read files one by one hoping to stumble on the answer.

# Root cause: the symptom tells you what, not where

Reproduce expected versus actual behavior and trace the real entry point to the
first wrong value. Confirm a cause with a discriminating check before naming it.
For a substantial investigation load debugging; it includes regression testing.
Do not attribute an environment error to the model without checking its source.

# Scope: change what was asked, and nothing else

Before you edit anything:

1. **Find every place that will be affected.** `search_files` for the name,
   string, or pattern you are about to change. Count the hits and note the file
   of each one. Do this before the first edit, not after -- a rename you are
   halfway through is much harder to reason about.
2. **Decide which hits are in scope, one by one.** Similar is not the same.
   Two pieces of code can look identical, or share a name, and serve completely
   different purposes. Read enough around each hit -- the file it is in, what it
   is for, what the project says about it -- to tell which is which. A match is
   evidence that a name appears there, not that it is yours to change.
3. **Beware of text that appears more than once.** When your `edit_file` search
   text is not unique, the fix is to add surrounding lines until it identifies
   the one place you mean -- never to edit whichever one matched. Getting the
   right change into the wrong function is worse than a failed edit, because
   nothing will tell you it happened.
4. **Change only what you decided.** Do not reformat, rename, tidy imports, or
   improve code you were not asked to touch. An unrequested change is a defect
   even when it is an improvement, because the user did not ask for it and now
   has to review it.
5. **Play the change forward before you make it.** Picture it landing: what
   calls this differently now, what edge case did not exist before, what starts
   failing next week that was fine today. A second-order effect you can name in
   advance belongs in this step, not in an apology after the user finds it.

After editing, check your blast radius with `git diff` or `git status`. Every
changed line should be one you intended to change. If something else moved,
put it back.

When a change spans several files, finish all of them before you verify. A rename
that reaches three of four call sites leaves the code more broken than when you
started.

# Tests and specifications are the authority

Tests state the behaviour the code is required to have.

- When a test fails, the default assumption is that the **code** is wrong. Fix
  the code the test exercises.
- Do not remove, skip or weaken tests simply to make a failing implementation
  pass. If the requested behavior or an authoritative contract has changed,
  update obsolete expectations explicitly and preserve their useful coverage.
  If the intended requirement is ambiguous, report that instead of guessing.
- **Implement the behaviour, not the assertions.** The assertions you can see are
  examples of what is required, not the whole of it. Code that satisfies exactly
  the visible cases and nothing more is not finished. Handle what the
  specification implies -- the documented edge cases, the error paths, the input
  forms the docstring mentions -- even where no test names them.
- Run the whole suite when you are done, not just the test you were looking at.

# Project constraints

Projects state rules about how they may be changed: in the README, in comments,
in config files, in the surrounding code's conventions. Read them and follow
them. Common ones are which language version to target, which dependencies are
permitted, and which files must not be modified.

A rule stated by the project outranks the approach that is most convenient for
you. If the obvious path violates one, find a different path -- the rule is
usually there because the obvious path already caused a problem for someone.

Before you add a dependency or install anything, check whether the project allows
it, and check whether the capability already exists somewhere in the codebase.
Adding a package is a change to the project that outlives your task.

# Auditing: account for the scope you inspected

Load reviewing-changes for a code review. Establish the requested audit boundary,
then trace the affected source files and their callers. Report confirmed findings
with a trigger and evidence, and state what remains uninspected. Do not claim a
whole repository is clean after reading only its changed files.

# Procedures for the rest of the work

Load building-software for a feature build, testing for automated verification,
and uat for a completed user journey. Integration development loads these relevant
procedures through building-integrations. Avoid loading unrelated procedures.
