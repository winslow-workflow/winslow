# Contributing to Winslow

Thanks for your interest in Winslow. It's early (0.x) — issues, ideas, and PRs
are all welcome.

## Development setup

Winslow uses [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/winslow-workflow/winslow
cd winslow
uv sync --all-extras      # install with the terminal-UI extra + dev tools
```

Run the CLI against a workflow directory:

```bash
uv run winslow show                                    # list discovered workflows
uv run winslow show --initialize --workflow <name>     # list one workflow's tasks
uv run winslow run                                     # launch the TUI
uv run winslow run --mode headless --workflow <name>   # headless run
```

## Tests

```bash
uv run pytest
```

The suite runs the engine end to end in both headless and TUI-runner modes.
Please add or update tests for behavior changes; CI runs the suite on every PR.

## Style

- Keep changes in the style of the surrounding code.
- Comments explain *why*, not *what* — skip comments on obvious code. See
  `AGENTS.md` for the full comment-style rules.
- Run the linter and formatter before pushing:

```bash
uv run ruff check src tests
uv run ruff format src tests
```

## Pull requests

1. Branch off `main`.
2. Keep PRs focused; describe the change and the reasoning.
3. Make sure tests pass and the CLI still starts (`uv run winslow show`).

## Reporting bugs

Open an issue with what you ran, what you expected, and what happened
(a minimal `workflow.py` that reproduces it is ideal).
