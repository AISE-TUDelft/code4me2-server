"""Compact summaries of test-runner output (pytest, unittest, Gradle, Maven,
Jest, Vitest, go test, cargo test).

A failing test run can print thousands of lines; the model needs the counts
and which tests failed with their first error line. The parsers are
deliberately forgiving: when nothing recognisable is found they return None
and the raw (already head+tail-capped) output is all the model gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

MAX_FAILURES = 10
_MAX_MESSAGE_CHARS = 300


@dataclass
class RunnerSummary:
    framework: str
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    failures: list[dict[str, str]] = field(default_factory=list)
    summary_line: str | None = None

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {"framework": self.framework}
        for key in ("passed", "failed", "errors", "skipped"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if self.failures:
            result["failures"] = self.failures[:MAX_FAILURES]
        if self.summary_line:
            result["summary_line"] = self.summary_line
        return result

    @property
    def has_failures(self) -> bool:
        return bool((self.failed or 0) + (self.errors or 0)) or bool(self.failures)

    @property
    def all_passed(self) -> bool:
        """Real passing counts and nothing failed: the raw log adds little."""
        return bool(self.passed) and not self.has_failures

    def headline(self) -> str:
        parts = []
        for key in ("failed", "errors", "passed", "skipped"):
            value = getattr(self, key)
            if value:
                parts.append(f"{value} {key}")
        text = f"{self.framework}: {', '.join(parts) if parts else 'no test counts found'}"
        names = [failure["name"] for failure in self.failures[:5]]
        if names:
            text += "\nFailing: " + "; ".join(names)
        return text


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_MESSAGE_CHARS else text[: _MAX_MESSAGE_CHARS - 1] + "…"


def _add_failure(summary: RunnerSummary, name: str, message: str = "") -> None:
    name = name.strip()
    if not name or any(item["name"] == name for item in summary.failures):
        return
    if len(summary.failures) < MAX_FAILURES:
        entry = {"name": _cut(name)}
        if message.strip():
            entry["message"] = _cut(message)
        summary.failures.append(entry)


def _count(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


# -------------------------------------------------------------------- pytest

_PYTEST_SUMMARY_RE = re.compile(r"^=+ (.*? in [\d.]+s(?: \([^)]*\))?) =+$", re.MULTILINE)
_PYTEST_SHORT_RE = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", re.MULTILINE)


def _pytest(text: str) -> RunnerSummary | None:
    matches = _PYTEST_SUMMARY_RE.findall(text)
    short = _PYTEST_SHORT_RE.findall(text)
    if not matches and not short:
        return None
    summary = RunnerSummary("pytest")
    if matches:
        body = matches[-1]
        summary.summary_line = body
        summary.passed = _count(r"(\d+) passed", body) or 0
        summary.failed = _count(r"(\d+) failed", body) or 0
        summary.errors = _count(r"(\d+) errors?", body) or 0
        summary.skipped = _count(r"(\d+) skipped", body)
    for _kind, name, message in short:
        _add_failure(summary, name, message)
    return summary


# ------------------------------------------------------------------ unittest

_UNITTEST_RAN_RE = re.compile(r"^Ran (\d+) tests? in [\d.]+s", re.MULTILINE)
_UNITTEST_RESULT_RE = re.compile(r"^(OK|FAILED)(?: \(([^)]*)\))?$", re.MULTILINE)
_UNITTEST_FAIL_RE = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]*)\)", re.MULTILINE)


def _unittest(text: str) -> RunnerSummary | None:
    ran = _UNITTEST_RAN_RE.search(text)
    if not ran:
        return None
    summary = RunnerSummary("unittest")
    total = int(ran.group(1))
    result = _UNITTEST_RESULT_RE.findall(text)
    details = result[-1][1] if result else ""
    summary.failed = _count(r"failures=(\d+)", details) or 0
    summary.errors = _count(r"errors=(\d+)", details) or 0
    summary.skipped = _count(r"skipped=(\d+)", details)
    summary.passed = max(0, total - summary.failed - summary.errors - (summary.skipped or 0))
    summary.summary_line = f"Ran {total} tests: {result[-1][0] if result else 'unknown'} {details}".strip()
    for _kind, method, where in _UNITTEST_FAIL_RE.findall(text):
        _add_failure(summary, f"{where}.{method}" if where and method not in where else where or method)
    return summary


# -------------------------------------------------------------------- gradle

_GRADLE_COUNT_RE = re.compile(r"(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?")
_GRADLE_FAIL_RE = re.compile(r"^(\S.*?) > (.+?) FAILED$", re.MULTILINE)


def _gradle(text: str) -> RunnerSummary | None:
    count = _GRADLE_COUNT_RE.search(text)
    fails = list(_GRADLE_FAIL_RE.finditer(text))
    build = re.search(r"^BUILD (SUCCESSFUL|FAILED)", text, re.MULTILINE)
    # Only test output counts: "BUILD SUCCESSFUL" alone is any task (dependencies,
    # tasks, assemble), whose full output the model needs.
    if not count and not fails:
        return None
    summary = RunnerSummary("gradle")
    if count:
        total, failed = int(count.group(1)), int(count.group(2))
        skipped = int(count.group(3)) if count.group(3) else 0
        summary.failed = failed
        summary.skipped = skipped or None
        summary.passed = max(0, total - failed - skipped)
    lines = text.splitlines()
    index_of = {id(match): text.count("\n", 0, match.start()) for match in fails}
    for match in fails:
        line_index = index_of[id(match)]
        message = lines[line_index + 1].strip() if line_index + 1 < len(lines) else ""
        _add_failure(summary, f"{match.group(1)} > {match.group(2)}", message)
    if build:
        summary.summary_line = f"BUILD {build.group(1)}"
    return summary


# --------------------------------------------------------------------- maven

_MAVEN_TOTAL_RE = re.compile(
    r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)(?!, Time)"
)
_MAVEN_FAIL_RE = re.compile(r"^\[ERROR\]\s+(\S+?[.#:]\S+?)(?::\d+)?\s+(.*)$", re.MULTILINE)


def _maven(text: str) -> RunnerSummary | None:
    totals = _MAVEN_TOTAL_RE.findall(text)
    if not totals:
        return None
    run, failures, errors, skipped = (int(value) for value in totals[-1])
    summary = RunnerSummary(
        "maven",
        passed=max(0, run - failures - errors - skipped),
        failed=failures,
        errors=errors,
        skipped=skipped or None,
    )
    section = re.search(r"^\[ERROR\] (?:Failures|Errors):\s*$(.*?)^\[(?:INFO|ERROR)\] *$", text, re.MULTILINE | re.DOTALL)
    if section:
        for name, message in _MAVEN_FAIL_RE.findall(section.group(1)):
            _add_failure(summary, name, message)
    build = re.search(r"BUILD (SUCCESS|FAILURE)", text)
    if build:
        summary.summary_line = f"BUILD {build.group(1)}"
    return summary


# -------------------------------------------------------------- jest/vitest

_JEST_TESTS_RE = re.compile(r"^Tests:\s+(.*?)(\d+) total", re.MULTILINE)
_JEST_BULLET_RE = re.compile(r"^\s*● (.+?)$", re.MULTILINE)
_VITEST_TESTS_RE = re.compile(r"^\s*Tests\s+(.+?)\s*\((\d+)\)\s*$", re.MULTILINE)
_VITEST_FAIL_RE = re.compile(r"^\s*(?:FAIL|×|✗)\s+(.+?)\s*$", re.MULTILINE)


def _jest(text: str) -> RunnerSummary | None:
    tests = _JEST_TESTS_RE.search(text)
    if tests:
        body = tests.group(1)
        summary = RunnerSummary(
            "jest",
            passed=_count(r"(\d+) passed", body) or 0,
            failed=_count(r"(\d+) failed", body) or 0,
            skipped=_count(r"(\d+) skipped", body),
            summary_line=tests.group(0).strip(),
        )
        for name in _JEST_BULLET_RE.findall(text):
            if " › " in name or not name.startswith("Test suite failed"):
                _add_failure(summary, name)
        return summary
    vitest = _VITEST_TESTS_RE.search(text)
    if vitest:
        body = vitest.group(1)
        summary = RunnerSummary(
            "vitest",
            passed=_count(r"(\d+) passed", body) or 0,
            failed=_count(r"(\d+) failed", body) or 0,
            skipped=_count(r"(\d+) skipped", body),
            summary_line=vitest.group(0).strip(),
        )
        for name in _VITEST_FAIL_RE.findall(text):
            _add_failure(summary, name)
        return summary
    return None


# ------------------------------------------------------------------- go test

_GO_FAIL_RE = re.compile(r"^\s*--- FAIL: (\S+)", re.MULTILINE)
_GO_PASS_RE = re.compile(r"^\s*--- PASS: ", re.MULTILINE)
# "ok  \texample.com/pkg\t0.012s" / "FAIL\texample.com/pkg\t0.3s" / "(cached)"
_GO_PKG_RE = re.compile(
    r"^(ok|FAIL)\s+(\S+)\s+(?:[\d.]+s|\(cached\))", re.MULTILINE
)


def _go(text: str) -> RunnerSummary | None:
    packages = _GO_PKG_RE.findall(text)
    fails = _GO_FAIL_RE.findall(text)
    if not packages and not fails:
        return None
    summary = RunnerSummary("go test", failed=len(fails))
    passes = len(_GO_PASS_RE.findall(text))
    if passes:
        summary.passed = passes
    for name in fails:
        _add_failure(summary, name)
    failed_packages = [name for status, name in packages if status == "FAIL"]
    summary.summary_line = (
        f"{len(packages) - len(failed_packages)} package(s) ok, {len(failed_packages)} failed"
    )
    return summary


# --------------------------------------------------------------- cargo test

_CARGO_RESULT_RE = re.compile(
    r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored"
)
_CARGO_FAIL_RE = re.compile(r"^test (\S+) \.\.\. FAILED$", re.MULTILINE)


def _cargo(text: str) -> RunnerSummary | None:
    results = _CARGO_RESULT_RE.findall(text)
    if not results:
        return None
    summary = RunnerSummary(
        "cargo test",
        passed=sum(int(item[1]) for item in results),
        failed=sum(int(item[2]) for item in results),
        skipped=sum(int(item[3]) for item in results) or None,
    )
    for name in _CARGO_FAIL_RE.findall(text):
        _add_failure(summary, name)
    return summary


# ---------------------------------------------------------------- dispatcher

_RUNNERS = {
    "pytest": (_pytest,),
    "py.test": (_pytest,),
    "gradle": (_gradle,),
    "gradlew": (_gradle,),
    "gradlew.bat": (_gradle,),
    "mvn": (_maven,),
    "mvnw": (_maven,),
    "mvnw.cmd": (_maven,),
    "go": (_go,),
    "cargo": (_cargo,),
    "jest": (_jest,),
    "vitest": (_jest,),
}
_JS_LAUNCHERS = frozenset({"npm", "npx", "yarn", "pnpm", "bun", "node"})
_PYTHON_LAUNCHERS = frozenset({"python", "python3", "py", "uv", "poetry", "hatch", "tox"})


def summarize_test_output(argv: list[str], stdout: str, stderr: str) -> RunnerSummary | None:
    """Parse a recognised test runner's output; None when the command is not one."""
    if not argv:
        return None
    program = PurePath(argv[0]).name.lower()
    text = f"{stdout}\n{stderr}"
    parsers: tuple = _RUNNERS.get(program, ())
    if not parsers and program in _PYTHON_LAUNCHERS:
        if any(arg in ("pytest", "py.test") for arg in argv[1:]):
            parsers = (_pytest,)
        elif "unittest" in argv[1:]:
            parsers = (_unittest,)
        else:
            parsers = (_pytest, _unittest)
    if program == "go" and "test" not in argv[1:]:
        parsers = ()  # go build/vet/run print no test summary
    if program == "cargo" and "test" not in argv[1:]:
        parsers = ()
    if not parsers and program in _JS_LAUNCHERS:
        parsers = (_jest,)
    if not parsers and program == "make":
        parsers = (_pytest, _gradle, _maven, _jest, _go, _cargo, _unittest)
    for parser in parsers:
        try:
            summary = parser(text)
        except (re.error, ValueError, IndexError):
            summary = None
        if summary is not None:
            return summary
    return None
