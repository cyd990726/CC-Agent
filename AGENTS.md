# AGENTS.md

Project-level guidance for AI coding agents working in this repository.

## Development Workflow

When the user asks to implement a new feature or make a non-trivial behavior
change, first inspect the related implementation ideas in:

```text
/Users/bytedance/Desktop/claude-code
```

Use that repository as a reference for architecture, naming, control flow,
edge-case handling, and tests. Do not copy blindly. Adapt the approach to this
project's smaller Python codebase and existing module boundaries.

For each new implementation task:

1. Identify the closest related Claude Code subsystem or files.
2. Summarize the relevant design pattern in concrete terms.
3. Propose a Mini-Agent-specific implementation plan.
4. Implement with small, scoped changes.
5. Add focused tests for the new behavior or regression.
6. Run the relevant test suite before reporting completion.

## Project Constraints

- Keep Mini-Agent minimal and readable.
- Prefer standard-library Python unless a dependency is clearly justified.
- Keep tool protocol behavior stable: model responses should remain
  `action` or `final_answer` JSON objects.
- Treat tool failures as recoverable observations whenever possible.

## Verification

Before finishing implementation work, run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q agent model tools ui main.py
git diff --check
```

For packaging-sensitive changes, also run:

```bash
python -m build
```

Remove generated build artifacts such as `build/`, `dist/`, and
`mini_agent_runtime.egg-info/` before final status unless the user explicitly
asks to keep them.
