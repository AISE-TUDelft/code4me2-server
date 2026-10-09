"""The task message the agent receives (the only user turn of a run)."""

from __future__ import annotations

TEMPLATE = """\
You are working on your own on a software engineering task. The repository is \
checked out at {workspace}, which is your workspace root; use paths relative to \
it. Its development environment is already installed: `python` and the \
project's test tools are on PATH.

<issue>
{problem_statement}
</issue>

Change the repository's non-test source files so that the issue above is resolved.

- Tests that check the fix already exist and are hidden from you. Do not modify \
existing tests or test configuration.
- First find the relevant code and reproduce the problem, for example with a \
small script. Then make the smallest change that fixes it and confirm the \
reproduction now behaves correctly. Run the most relevant existing tests to \
check that nothing else broke.
- Think about edge cases the issue mentions or implies.
- Nobody will answer questions during this task. Do not ask for confirmation or \
clarification: make reasonable assumptions and keep going until the fix is done.
- Do not commit, stash or switch branches; leave your changes in the working \
tree. Remove scratch files you created once you are done.
- End with a short summary of what you changed.
"""


def task_prompt(problem_statement: str, *, workspace: str) -> str:
    return TEMPLATE.format(workspace=workspace, problem_statement=problem_statement.strip())
