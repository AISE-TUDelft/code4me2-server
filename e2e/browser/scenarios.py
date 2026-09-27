"""Task 07 §6 browser scenarios A–E against the disposable e2e stack.

Uses the installed Playwright Chromium via the Python API. Each run creates a
fresh participant account through the public API so the one-active-enrollment
rule never confounds the scenario, and targets studies by captured id (not by
name matching). Adds no repository dependency; never touches the dev stack.

Every check has one stable id from the runner's exact inventory; the runner
refuses to pass when an id is missing, duplicated, blocked or unexpected. Join
codes and account credentials are never printed or written to the results.

Beyond the visible UI, the checks re-read the real API models: the edited study
metadata, the stopped study's retained enrollment/assignment counts, the
cloned study's identity/profile selection/status, the participant's own
enrollment projection and the owner-scoped participant coverage model.

A: researcher create/edit/lock, B: participant web join, C: stop/clone,
D: authorization, E: study workspace tabs (participants, dashboard, analytics),
participant budgets (spent/budget columns, a top-up from the drawer re-read
through the budget API) and the participant's My studies page.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

config_path = os.environ.get("CODE4ME_E2E_BROWSER_CONFIG")
CONFIG = json.loads(Path(config_path).read_text()) if config_path else {}
UI = CONFIG.get("ui_url", "http://localhost:3900")
API = CONFIG.get("base_url", "http://localhost:28008")
OWNER = CONFIG.get("researcher", {"email": "e2e-researcher@example.com", "password": "ResearcherPass123"})
ADMIN = CONFIG.get("admin", {"email": "e2e-admin@example.com", "password": "AdminPass123"})
PROFILE_NAME = CONFIG.get("profile_name", "e2e-profile")
EXPECTED_IDS = tuple(
    str(item) for item in json.loads(os.environ.get("CODE4ME_E2E_BROWSER_EXPECTED_IDS", "[]"))
)

results: list[dict[str, str]] = []


def record(step_id: str, title: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results.append({"id": step_id, "title": title, "status": status, "detail": detail})
    print(f"{status}  {step_id}  {title}" + (f"  :: {detail}" if detail else ""))


def record_blocked(step_id: str, title: str, reason: str) -> None:
    results.append({"id": step_id, "title": title, "status": "BLOCKED", "detail": reason})
    print(f"BLOCKED  {step_id}  {title}  :: {reason}")


def api_post(path: str, payload: dict, token: str | None = None) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read() or b"{}")


def fresh_participant() -> dict:
    stamp = int(time.time() * 1000)
    account = {
        "email": f"e2e-browser-{stamp}@example.com",
        "password": "BrowserPass123",
        "name": "Browser Participant",
    }
    api_post("/api/user/create", {**account, "config_id": CONFIG.get("config_id", 1)})
    return account


def login(page, account) -> None:
    page.goto(f"{UI}/login", wait_until="networkidle")
    page.fill("#email", account["email"])
    page.fill("#password", account["password"])
    page.click('button[type="submit"]')
    page.wait_for_url(lambda url: "/login" not in str(url), timeout=20000)


def find_study_id(page, name: str) -> str | None:
    return page.evaluate(
        """async (name) => {
            const res = await fetch('/api/research/studies', { credentials: 'include' });
            const data = await res.json();
            return (data.studies || []).find((s) => s.name === name)?.study_id || null;
        }""",
        name,
    )


def study_detail(page, study_id: str) -> dict:
    return page.evaluate(
        """async (id) => {
            const res = await fetch(`/api/research/studies/${id}`, { credentials: 'include' });
            if (!res.ok) return {};
            const data = await res.json();
            return data.study || {};
        }""",
        study_id,
    )


def participant_budget(page, study_id: str, enrollment_id: str | None) -> dict:
    """The owner-scoped budget of one enrollment (resolved from the table when unknown)."""
    return page.evaluate(
        """async ({ id, enrollmentId }) => {
            let eid = enrollmentId;
            if (!eid) {
                const rows = await fetch(`/api/research/studies/${id}/analytics/participants`, { credentials: 'include' });
                let data = {};
                try { data = await rows.json(); } catch (_) { data = {}; }
                eid = ((data.participants || [])[0] || {}).enrollment_id || null;
            }
            if (!eid) return {};
            const res = await fetch(`/api/research/studies/${id}/enrollments/${eid}/budget`, { credentials: 'include' });
            if (!res.ok) return {};
            return await res.json();
        }""",
        {"id": study_id, "enrollmentId": enrollment_id},
    )


def participant_coverage(page, study_id: str) -> dict:
    return page.evaluate(
        """async (id) => {
            const res = await fetch(
                `/api/research/operations/participants/coverage?study_id=${encodeURIComponent(id)}`,
                { credentials: 'include' });
            let data = {};
            try { data = await res.json(); } catch (_) { data = {}; }
            return { status: res.status, data };
        }""",
        study_id,
    )


def open_tab(details, name: str) -> None:
    """Select a study workspace tab; tab panels only exist while selected."""
    details.get_by_role("tab", name=re.compile(name, re.I)).click()


def main() -> int:
    participant_account = fresh_participant()
    study_id: str | None = None
    clone_id: str | None = None
    enrollment_id: str | None = None
    assignment_id: str | None = None
    profile_id: str | None = None
    renamed = ""
    edited_description = ""
    clone_name = ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        owner_ctx = browser.new_context()
        participant_ctx = browser.new_context()
        owner = owner_ctx.new_page()
        participant = participant_ctx.new_page()

        captured: list[dict] = []
        owner.on(
            "request",
            lambda req: captured.append({"method": req.method, "url": req.url, "body": req.post_data})
            if "/api/research/" in req.url
            else None,
        )

        try:
            # ---- Scenario A: researcher create + metadata lock ----
            login(owner, OWNER)
            owner.goto(f"{UI}/research/studies", wait_until="networkidle")
            record("A1", "owner reaches the research control plane", owner.locator("h2#research-studies-title").is_visible())
            record(
                "A2",
                "no revision/draft/supersede authoring controls render",
                owner.get_by_text(re.compile(r"revision|supersede|publish draft", re.I)).count() == 0,
            )

            owner.click('button:has-text("New study")')
            study_name = f"Browser study {int(time.time())}"
            owner.locator("form.research-card input").first.fill(study_name)
            owner.locator("fieldset.research-profile-selection input[type=checkbox]").first.check()
            # The seeded arm runs the built-in agent (metered), so the form
            # requires a default budget per participant before it submits.
            owner.locator("#study-default-budget").wait_for(timeout=20000)
            owner.locator("#study-default-budget").fill("5")
            owner.click('button:has-text("Create Draft study")')
            owner.wait_for_selector("text=Study created in Draft state.", timeout=20000)

            create_req = next(
                (r for r in captured if r["method"] == "POST" and r["url"].endswith("/api/research/studies")),
                None,
            )
            create_body = json.loads(create_req["body"]) if create_req and create_req["body"] else {}
            record(
                "A3",
                "create posts the complete setup once (with profiles and the default budget)",
                bool(create_req)
                and len(create_body.get("profile_ids", [])) >= 1
                # The form normalises the amount ("5" → "5.00"); compare the value, not the spelling.
                and str(create_body.get("default_budget_usd") or "").replace(",", "") in ("5", "5.0", "5.00"),
            )
            record("A4", "create body carries no revision/condition key", bool(create_req) and not re.search(r"revision|condition", create_req["body"]))

            study_id = find_study_id(owner, study_name)
            owner.locator("button.research-list-item", has_text=study_name).click()
            details = owner.locator('section[aria-label="Study details"]')
            details.wait_for()
            join_code = details.locator("dd").nth(1).inner_text().strip()
            record("A5", "DRAFT shows a join code and the fixed profile summary", bool(re.match(r"^[A-F0-9]{6,}$", join_code)))
            record("A6", "profile selection is presented as fixed", details.get_by_text("Profile selection is fixed after study creation.").is_visible())
            record("A7", "study details contain no revision ID", not re.search(r"revision", details.inner_text(), re.I))

            renamed = f"{study_name} (edited)"
            edited_description = f"Edited description for {study_name}"
            # Metadata editing lives in the study's Settings tab.
            open_tab(details, "settings")
            details.locator("form input").first.fill(renamed)
            details.locator("form textarea").first.fill(edited_description)
            owner.click('button:has-text("Save metadata")')
            owner.wait_for_selector("text=Study metadata updated.", timeout=20000)
            record("A8", "metadata edit is allowed before first consent", True)

            persisted_study = study_detail(owner, study_id)
            record(
                "A12",
                "edited metadata is persisted in the study read model",
                persisted_study.get("name") == renamed
                and persisted_study.get("description") == edited_description
                and not persisted_study.get("consent_locked_at")
                and persisted_study.get("research_status") == "DRAFT",
            )

            # ---- Scenario B: participant web-only join ----
            participant.goto(f"{UI}/research/join", wait_until="networkidle")
            participant.locator("input[required]").fill(join_code)
            participant.click('button:has-text("Review study")')
            participant.wait_for_url(re.compile(r"/login$"), timeout=20000)
            record("B1", "unauthenticated join redirects to login preserving intent", True)

            login(participant, participant_account)
            participant.wait_for_url(re.compile(r"/research/join"), timeout=20000)
            participant.wait_for_function("() => document.querySelector('input[required]').value.length > 0", timeout=20000)
            prefilled = participant.locator("input[required]").input_value()
            record("B2", "login returns to the join page with the code preserved", prefilled == join_code)
            participant.click('button:has-text("Review study")')
            participant.wait_for_selector("text=I accept the study consent notice.", timeout=20000)
            record("B3", "safe study summary and consent control render", True)
            record("B4", "participant sees exactly one checkbox (consent, no profile picker)", participant.locator('input[type="checkbox"]').count() == 1)

            participant.click('button:has-text("Accept and join")')
            participant.wait_for_selector("text=You must accept the consent notice before joining.", timeout=20000)
            record("B5", "consent-unchecked join is refused", True)

            participant.locator('input[type="checkbox"]').first.check()
            participant.click('button:has-text("Accept and join")')
            participant.wait_for_selector('div[aria-label="Enrollment handoff"]', timeout=20000)
            handoff = participant.locator('div[aria-label="Enrollment handoff"]')
            enrollment_id = handoff.get_attribute("data-enrollment-id")
            assignment_id = handoff.get_attribute("data-assignment-id")
            profile_id = handoff.get_attribute("data-agent-profile-id")
            record(
                "B6",
                "exactly one enrollment and assignment handed off",
                bool(enrollment_id and assignment_id and profile_id),
                "enrollment, assignment and agent profile ids present"
                if enrollment_id and assignment_id and profile_id
                else "handoff data attributes incomplete",
            )
            record("B7", "success state carries the IntelliJ next step", "post-enrollment activation" in handoff.inner_text())

            own_status = participant.evaluate(
                """async () => {
                    const res = await fetch('/api/research/participants/me', { credentials: 'include' });
                    let data = {};
                    try { data = await res.json(); } catch (_) { data = {}; }
                    return { status: res.status, data };
                }"""
            )
            own_rows = [
                row
                for row in (own_status.get("data") or {}).get("enrollments", [])
                if row.get("study_id") == study_id
            ]
            record(
                "B8",
                "participant read model reports the persisted ACTIVE enrollment",
                own_status.get("status") == 200
                and len(own_rows) == 1
                and own_rows[0].get("enrollment_id") == enrollment_id
                and own_rows[0].get("status") == "ACTIVE",
            )

            # ---- back in A: ACTIVE, metadata locked, stale client rejected ----
            owner.click('button:has-text("Refresh")')
            owner.wait_for_function(
                """(name) => Array.from(document.querySelectorAll('button.research-list-item'))
                    .some((b) => b.textContent.includes(name) && b.textContent.includes('Active'))""",
                arg=renamed,
                timeout=20000,
            )
            owner.locator("button.research-list-item", has_text=renamed).click()
            # Reselecting a study may reset the workspace to its Overview tab.
            open_tab(details, "settings")
            owner.wait_for_function(
                """() => {
                    const panel = document.querySelector('section[aria-label="Study details"]');
                    if (!panel) return false;
                    const input = panel.querySelector('form input');
                    return Boolean(input) && input.disabled;
                }""",
                timeout=20000,
            )
            study_state = study_detail(owner, study_id)
            record(
                "A9",
                "owner sees ACTIVE with one active enrollment and assignment",
                study_state.get("research_status") == "ACTIVE"
                and study_state.get("active_enrollment_count") == 1
                and study_state.get("assignment_count") == 1
                and study_state.get("active_assignment_count") == 1,
                f"enrollments={study_state.get('enrollment_count')} "
                f"active={study_state.get('active_enrollment_count')} "
                f"assignments={study_state.get('assignment_count')}",
            )
            record("A10", "metadata inputs are disabled after first consent", details.locator("form input").first.is_disabled())

            forced_status = owner.evaluate(
                """async (id) => {
                    const res = await fetch(`/api/research/studies/${id}/metadata`, {
                        method: "PATCH", headers: { "Content-Type": "application/json" },
                        credentials: "include", body: JSON.stringify({ name: "stale client write" }),
                    });
                    return res.status;
                }""",
                study_id,
            )
            record("A11", "stale-client metadata write is rejected after consent", forced_status == 409, f"status={forced_status}")

            identity_ready = bool(enrollment_id and assignment_id and profile_id)
            if identity_ready:
                coverage_payload = participant_coverage(owner, study_id)
                coverage_data = coverage_payload.get("data") or {}
                coverage_rows = [
                    row
                    for row in coverage_data.get("participants", [])
                    if row.get("enrollment_id") == enrollment_id
                ]
                coverage_assignment = (coverage_rows[0].get("assignment") if coverage_rows else None) or {}
                record(
                    "A13",
                    "coverage read model exposes the enrollment and frozen assignment",
                    coverage_payload.get("status") == 200
                    and coverage_data.get("participant_count") == 1
                    and len(coverage_rows) == 1
                    and coverage_rows[0].get("status") == "ACTIVE"
                    and coverage_assignment.get("assignment_id") == assignment_id
                    and coverage_assignment.get("agent_profile_id") == profile_id
                    and coverage_assignment.get("status") == "ACTIVE"
                    and bool(coverage_assignment.get("strategy")),
                )
            else:
                record_blocked("A13", "coverage read model exposes the enrollment and frozen assignment", "handoff identity missing (see B6)")

            # ---- Scenario E: study workspace — participants, dashboards, analytics ----
            open_tab(details, "participants")
            participants_table = details.get_by_role("table", name="Enrolled participants")
            participants_table.wait_for(timeout=20000)
            rows = participants_table.locator("tbody tr")
            arm_cell = rows.first.locator("td").nth(1).inner_text().strip() if rows.count() else ""
            table_text = participants_table.inner_text()
            record(
                "E1",
                "participants tab lists the enrolled participant, its frozen arm and no account identity",
                rows.count() == 1
                and PROFILE_NAME in arm_cell
                and "Not assigned" not in arm_cell
                and "Active" in table_text
                and "@" not in table_text,
                f"arm={arm_cell.splitlines()[0] if arm_cell else ''}",
            )
            table_lower = table_text.lower()  # header cells render uppercased by CSS
            record(
                "E5",
                "participants tab shows the participant's spent amount and budget",
                "spent" in table_lower and "budget" in table_lower and "$5.00" in table_text and "$0.00" in table_text,
                "table=" + " ".join(table_text.split())[:260],
            )
            rows.first.get_by_role("button", name=re.compile("open dashboard", re.I)).click()
            drawer = owner.get_by_role("dialog")
            drawer.wait_for(timeout=20000)
            owner.wait_for_function(
                """() => { const d = document.querySelector('[role=dialog]');
                           return d && !d.textContent.includes('Loading participant telemetry'); }""",
                timeout=20000,
            )
            drawer_text = drawer.inner_text()
            record(
                "E2",
                "the participant dashboard opens with metadata-only telemetry",
                "Participant" in drawer_text and ("Prompts" in drawer_text or "No telemetry" in drawer_text),
            )
            # ---- Budgets: top up from the drawer, then re-read the budget API ----
            drawer.get_by_role("button", name=re.compile("adjust budget", re.I)).click()
            adjust_form = drawer.get_by_role("form", name="Adjust budget")
            adjust_form.wait_for(timeout=20000)
            adjust_form.locator("#adjust-budget-amount").fill("2.50")
            adjust_form.locator("#adjust-budget-reason").fill("browser top-up")
            adjust_form.get_by_role("button", name=re.compile("top up budget", re.I)).click()
            owner.wait_for_selector("text=Topped up by $2.50", timeout=20000)
            budget_state = participant_budget(owner, study_id, enrollment_id)
            budget_balance = budget_state.get("balance") or {}
            record(
                "E6",
                "owner tops up the participant's budget from the drawer and the ledger records the reason",
                budget_balance.get("limit_micro_usd") == 7_500_000
                and budget_balance.get("consumed_micro_usd") == 0
                and any(
                    item.get("kind") == "TOP_UP" and item.get("reason") == "browser top-up"
                    for item in budget_state.get("recent_adjustments", [])
                ),
                f"limit={budget_balance.get('limit_micro_usd')}",
            )
            owner.keyboard.press("Escape")
            drawer.wait_for(state="detached", timeout=20000)
            open_tab(details, "analytics")
            owner.wait_for_selector("text=Arm comparison", timeout=20000)
            record(
                "E3",
                "analytics compares arms on participant-level metrics",
                owner.get_by_text("Participants with telemetry").count() >= 1,
            )
            participant.goto(f"{UI}/research/my-studies", wait_until="networkidle")
            participant.wait_for_selector(f"text={renamed}", timeout=20000)
            record(
                "E4",
                "My studies shows the joined study and its schedule",
                participant.get_by_text("Study ends").count() >= 1 and participant.locator('input[type="checkbox"]').count() == 0,
            )
            record(
                "E7",
                "My studies shows the remaining budget without any arm detail",
                participant.get_by_text("Budget remaining").count() >= 1
                and participant.get_by_text(re.compile(r"provided by the study")).count() >= 1
                and participant.get_by_text("$7.50").count() >= 1
                and participant.get_by_text(re.compile(r"own account|profile", re.I)).count() == 0,
            )

            # ---- Scenario C: stop, retained data, clone ----
            open_tab(details, "settings")
            owner.wait_for_selector('button:has-text("Stop study")', state="visible", timeout=20000)
            owner.once("dialog", lambda dialog: dialog.accept())
            owner.click('button:has-text("Stop study")')
            owner.wait_for_selector("text=Study stopped. Collection is revoked and retained data remains available.", timeout=20000)
            record("C1", "stop is terminal and non-destructive in the UI", True)
            stopped_state = study_detail(owner, study_id)
            record(
                "C2",
                "stopped study retains its enrollment rows",
                stopped_state.get("research_status") == "STUDY_STOPPED"
                and stopped_state.get("enrollment_count") == 1,
                f"enrollments={stopped_state.get('enrollment_count')}",
            )

            clone_name = f"{renamed} (copy)"
            owner.click('button:has-text("Clone as new Draft")')
            # The clone form renders above the study list, not inside the details panel.
            clone_form = owner.locator("form.study-create-form").filter(has_text="Clone study")
            clone_form.wait_for()
            clone_profile = clone_form.get_by_label(PROFILE_NAME, exact=True)
            clone_copies = clone_form.get_by_text(clone_name).count() >= 1
            record(
                "C3",
                "clone form is prefilled from the stopped study without profiles",
                clone_copies
                and "not copied" in clone_form.inner_text()
                and clone_profile.count() == 1
                and not clone_profile.is_checked(),
            )
            if clone_profile.count() != 1:
                record_blocked("C4", "clone persists a distinct DRAFT study with the reselected profile", "the agent profile is not offered in the clone form")
            else:
                clone_profile.check()
                clone_form.get_by_role("button", name="Clone Draft study").click()
                owner.wait_for_selector("text=/Clone created/", timeout=20000)
                clone_id = find_study_id(owner, clone_name)
                clone_study = study_detail(owner, clone_id) if clone_id else {}
                clone_profiles = [item.get("profile_id") for item in clone_study.get("profile_selections") or []]
                record(
                    "C4",
                    "clone persists a distinct DRAFT study with the reselected profile",
                    bool(clone_id)
                    and clone_id != study_id
                    and clone_study.get("name") == clone_name
                    and clone_study.get("description") == edited_description
                    and clone_study.get("research_status") == "DRAFT"
                    and profile_id in clone_profiles
                    and clone_study.get("enrollment_count") == 0
                    and clone_study.get("assignment_count") == 0
                    and bool((clone_study.get("lifecycle_capabilities") or {}).get("joinable")),
                )

            participant.goto(f"{UI}/research/join", wait_until="networkidle")
            participant.locator("input[required]").fill(join_code)
            participant.click('button:has-text("Review study")')
            participant.wait_for_selector("text=/stopped and cannot accept new participants|invalid or no longer available/i", timeout=20000)
            record("C5", "stopped study cannot be joined", True)

            if assignment_id:
                stopped_coverage = participant_coverage(owner, study_id)
                stopped_data = stopped_coverage.get("data") or {}
                stopped_rows = [
                    row
                    for row in stopped_data.get("participants", [])
                    if row.get("enrollment_id") == enrollment_id
                ]
                stopped_assignment = (stopped_rows[0].get("assignment") if stopped_rows else None) or {}
                record(
                    "C6",
                    "coverage read model retains the stopped enrollment and assignment",
                    stopped_coverage.get("status") == 200
                    and stopped_data.get("participant_count") == 1
                    and len(stopped_rows) == 1
                    and stopped_rows[0].get("status") == "STUDY_STOPPED"
                    and stopped_assignment.get("assignment_id") == assignment_id
                    and stopped_assignment.get("status") == "STUDY_STOPPED",
                )
            else:
                record_blocked("C6", "coverage read model retains the stopped enrollment and assignment", "handoff identity missing (see B6)")

            # ---- Scenario D: authorization ----
            participant.goto(f"{UI}/research/studies", wait_until="networkidle")
            # Non-researchers are redirected to My studies; older builds showed
            # a permission notice instead. Either way no control plane renders.
            try:
                participant.wait_for_url(re.compile(r"/research/my-studies"), timeout=20000)
                redirected = True
            except Exception:  # noqa: BLE001 - fall back to the notice check
                redirected = False
            record(
                "D1",
                "participant cannot reach the researcher control plane",
                participant.locator("h2#research-studies-title").count() == 0
                and (redirected or participant.get_by_text("You do not have permission to view research studies.").is_visible()),
                "redirected to My studies" if redirected else "",
            )
            revoke_status = participant.evaluate(
                """async (args) => {
                    const res = await fetch(
                        `/api/research/studies/${args.studyId}/enrollments/${args.enrollmentId}/revoke`,
                        { method: "POST", headers: { "Content-Type": "application/json" },
                          credentials: "include", body: "{}" });
                    return res.status;
                }""",
                {"studyId": study_id, "enrollmentId": enrollment_id},
            )
            record("D2", "participant cannot revoke an enrollment", revoke_status in (403, 404), f"status={revoke_status}")

            admin_ctx = browser.new_context()
            admin = admin_ctx.new_page()
            login(admin, ADMIN)
            admin.goto(f"{UI}/research/studies", wait_until="networkidle")
            record("D3", "admin reaches the control plane", admin.locator("h2#research-studies-title").is_visible())
            # The kill-switch control lives in the Settings tab of a non-stopped
            # study; the clone is the only non-stopped study at this point.
            admin_target = clone_name if clone_id else CONFIG.get("study_name", "E2E Synthetic Study")
            admin.locator("button.research-list-item", has_text=admin_target).first.click()
            admin_details = admin.locator('section[aria-label="Study details"]')
            admin_details.wait_for(timeout=20000)
            open_tab(admin_details, "settings")
            kill_switch = admin.get_by_role("button", name=re.compile("kill switch", re.I))
            try:
                # The admin flag loads asynchronously; absence after a wait is
                # a failed check, not a harness crash.
                kill_switch.first.wait_for(timeout=10000)
                kill_switch_present = True
            except Exception:  # noqa: BLE001 - report absence as a failed check
                kill_switch_present = False
            record(
                "D4",
                "admin sees the kill-switch control in the study detail panel",
                kill_switch_present,
            )
            # Compare like with like: the owner looks at the same Settings tab.
            open_tab(details, "settings")
            record(
                "D5",
                "non-admin researcher sees no kill-switch control",
                owner.get_by_role("button", name=re.compile("kill switch", re.I)).count() == 0,
            )
            coverage_forbidden = participant.evaluate(
                """async (id) => {
                    const res = await fetch(
                        `/api/research/operations/participants/coverage?study_id=${encodeURIComponent(id)}`,
                        { credentials: "include" });
                    return res.status;
                }""",
                study_id,
            )
            record(
                "D6",
                "participant cannot read study participant coverage",
                coverage_forbidden in (403, 404),
                f"status={coverage_forbidden}",
            )
            admin_ctx.close()
        except Exception as error:  # noqa: BLE001 - report and continue
            record("harness", "unhandled browser harness error", False, str(error).replace("\n", " ")[:300])
        finally:
            browser.close()

    context = {
        "study_id": study_id or "",
        "edited_name": renamed,
        "edited_description": edited_description,
        "clone_study_id": clone_id or "",
        "clone_name": clone_name,
        "profile_id": profile_id or "",
        "enrollment_id": enrollment_id or "",
        "assignment_id": assignment_id or "",
    }
    ids = [item["id"] for item in results]
    duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
    missing = [step_id for step_id in EXPECTED_IDS if step_id not in ids]
    unexpected = [step_id for step_id in ids if step_id not in EXPECTED_IDS]
    non_pass = [item for item in results if item["status"] != "PASS"]
    if EXPECTED_IDS:
        inventory_ok = not missing and not unexpected and not duplicates and len(results) == len(EXPECTED_IDS)
    else:
        inventory_ok = not duplicates
    print(f"\nTOTAL {len(results)}  PASS {len(results) - len(non_pass)}  NON-PASS {len(non_pass)}")
    for item in non_pass:
        print(f"  {item['status']} {item['id']}  {item['title']}  {item['detail']}")
    if not inventory_ok:
        print(f"  INVENTORY missing={missing} unexpected={unexpected} duplicated={duplicates}")
    result_path = os.environ.get("CODE4ME_E2E_BROWSER_RESULTS")
    if result_path:
        Path(result_path).write_text(json.dumps({
            "steps": results,
            "context": context,
            "inventory": {"expected": list(EXPECTED_IDS), "missing": missing,
                          "unexpected": unexpected, "duplicated": duplicates},
        }, indent=2))
    return 0 if (not non_pass and inventory_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
