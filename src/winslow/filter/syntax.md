# Filter Syntax

Filter queries apply to the Tasks pane. The History pane accepts the same
syntax, but only with the builtin name and group filters: a history row is a
value record, not a live task, so a project filter cannot run there. A query
with a project filter shows a warning in the History pane.

## Name filter

Bare text matches task names (case-insensitive).

```
build
```

## Filter commands

Use `!` followed by a short or long command name, then a value.

| Command       | Description               |
|---------------|---------------------------|
| `!g` `!group` | Match tasks in a group    |

## Negation

`~` (alias `not`) excludes instead of includes. It goes immediately before the
value — so for a command it comes **after** the command, never before it.

```
~build         name does NOT contain "build"
!g ~deploy     NOT in group "deploy"
```

`~!g deploy` is invalid — the command annotates the value, so the negation
belongs on the value (`!g ~deploy`). You can still negate a parenthesized group:
`~(build or test)`.

## Boolean operators

| Operator | Aliases | Description                 |
|----------|---------|-----------------------------|
| `&`      | `and`   | Both conditions must match  |
| `\|`     | `or`    | Either condition must match |

## Comma shorthand

Comma-separated values expand to OR — applies to all filter types.

```
build,test,lint       tasks named build, test, or lint
!g deploy,release     tasks in the deploy or release group
!g ~deploy,release    NOT in the deploy or release group
```

## Grouping

Parentheses control evaluation order.

## Examples

```
build                              name contains "build"
!g deploy                          in group "deploy"
!group deploy                      same as above (long form)

build and !g deploy                name "build" AND in group "deploy"
build or test                      name "build" OR name "test"
build,test                         same as above (comma shorthand)

~staging                           name does NOT contain "staging"
!g ~staging                        NOT in group "staging"
~(build or test)                   NOT (name "build" or "test")

(build or test) and !g ci          (name "build" or "test") AND in group "ci"
build and !g deploy,release        name "build" AND in group "deploy" or "release"
!g infra and !g ~experimental      in group "infra" AND NOT in group "experimental"

(fetch,transform) and !g etl and !g ~slow
                                   name "fetch" or "transform",
                                   in group "etl",
                                   NOT in group "slow"
```
