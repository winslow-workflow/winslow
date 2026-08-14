# AGENTS.md

## Documentation style

Do not document failure paths in `docs/`: no lists of what raises, no exception class names, no
error-message walkthroughs. The docs teach the working path. The error itself must carry enough
context and direction to point the user to a solution - write that context into the exception
message, at the raise site.

Describe only behavior that happens. Do not document what does not happen ("an undeclared read
does not cascade", "the framework never refreshes in the background"). State the positive rule
instead, or leave it out.

State each fact once per page. When two sections need the same fact, one section owns it and the
other links to it.

## Code style

Prefer a declarative and functional style over a procedural and imperative one. Keep functions free of
side effects where possible. Do not mutate data in place as an intermediate step: implement the solution
as a series of data transformations.

Before proposing a solution, consider whether the problem can be solved by introducing a new data
structure, or by changing an existing one. Prefer such a change over new procedural logic.

## Comment style

Comments and docstrings in `src/winslow/` follow the **ASD-STE100 writing rules** (Simplified Technical
English). The approved-vocabulary dictionary is *not* applied: normal software vocabulary stays as-is
(lock, listener, batch, import hook, descriptor, MRO, garbage collector, topological generation).

Write to these rules:

1. Active voice, present tense. Past tense only for a state that already happened.
2. One idea per sentence. Max 20 words for an instruction, 25 for descriptive text.
3. No contractions. Write `does not`, `is not`, `cannot`.
4. No em dash or en dash as a clause joiner. Use a full stop, or a colon when a real list follows.
5. No idiom, metaphor, humour or slang. Not `footgun`, `heavy handed`, `rides the outer claim`,
   `launder a defect`, `drown the real values`.
6. No first person. `we`, `our`, `us` become the subject that acts: "the runner", "the graph", "the UI".
7. Keep the articles. No telegraphic style: write `This prefix marks a scoped package`, not
   `Prefix marking a scoped package`.
8. Max 3 words in a noun cluster. Break a longer one with `of` or `for`.
9. No ellipsis. Name the item that is left out.
10. One word per concept, used consistently: task, batch, session, store, workflow config. Do not drift
    between synonyms (`kill` / `stop` / `end`) for one action.
11. Keep the cross-references — `(see StoreListener)`, `(see Graph)` — they are useful and STE-legal.

Delete a comment that only restates the code (`# Submit tasks to the executor`). Keep every non-obvious
*why*. Do not comment simple code, and do not write a comment that describes the code a change replaced.

Out of scope for these rules: log messages, exception messages, `# type:` and `# noqa` pragmas. A
`TODO`/`FIXME` marker stays; only its sentence follows the rules.

Examples:

```python
# Before
# Redundant writes are dropped whole: the dict write itself would
# be free, but callback + listeners can be expensive (UI adapters),
# and observers would see a transition that didn't happen.

# After
# A redundant write is dropped completely. The dict write is cheap,
# but the callback and the listeners can be slow (UI adapters).
# Observers must not see a transition that did not occur.
```

```python
# Before
"""This is a more heavy handed approach to task eligibility, basically we skip task creation altogether."""

# After
"""Override this to prevent the creation of the task instance.

This is stronger than is_eligible, which filters the tasks after their creation.
"""
```
