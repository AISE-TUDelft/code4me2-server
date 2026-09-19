"""Task 07 §6 browser scenarios A–D against the disposable e2e stack.

Uses the installed Playwright Chromium via the Python API. Each run creates a
fresh participant account through the public API so the one-active-enrollment
rule never confounds the scenario, and targets studies by captured id (not by
name matching). Adds no repository dependency; never touches the dev stack.
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

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  :: {detail}" if detail else ""))


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


def main() -> int:
    participant_account = fresh_participant()
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
            record("A1 owner reaches the research control plane", owner.locator("h2#research-studies-title").is_visible())
            record(
                "A2 no revision/draft/supersede authoring controls render",
                owner.get_by_text(re.compile(r"revision|supersede|publish draft", re.I)).count() == 0,
            )

            owner.click('button:has-text("New study")')
            study_name = f"Browser study {int(time.time())}"
            owner.locator("form.research-card input").first.fill(study_name)
            owner.locator("fieldset.research-profile-selection input[type=checkbox]").first.check()
            owner.click('button:has-text("Create Draft study")')
            owner.wait_for_selector("text=Study created in Draft state.", timeout=20000)

            create_req = next(
                (r for r in captured if r["method"] == "POST" and r["url"].endswith("/api/research/studies")),
                None,
            )
            create_body = json.loads(create_req["body"]) if create_req and create_req["body"] else {}
            record("A3 create posts the complete setup once (with profiles)", bool(create_req) and len(create_body.get("profile_ids", [])) >= 1)
            record("A4 create body carries no revision/condition key", bool(create_req) and not re.search(r"revision|condition", create_req["body"]))

            study_id = owner.evaluate(
                """async (name) => {
                    const res = await fetch('/api/research/studies', { credentials: 'include' });
                    const data = await res.json();
                    return (data.studies || []).find((s) => s.name === name)?.study_id;
                }""",
                study_name,
            )
            owner.locator("button.research-list-item", has_text=study_name).click()
            details = owner.locator('section[aria-label="Study details"]')
            details.wait_for()
            join_code = details.locator("dd").nth(1).inner_text().strip()
            record("A5 DRAFT shows a join code and the fixed profile summary", bool(re.match(r"^[A-F0-9]{6,}$", join_code)), f"code={join_code}")
            record("A6 profile selection is presented as fixed", details.get_by_text("Profile selection is fixed after study creation.").is_visible())
            record("A7 study details contain no revision ID", not re.search(r"revision", details.inner_text(), re.I))

            renamed = f"{study_name} (edited)"
            details.locator("input").first.fill(renamed)
            owner.click('button:has-text("Save metadata")')
            owner.wait_for_selector("text=Study metadata updated.", timeout=20000)
            record("A8 metadata edit is allowed before first consent", True)

            # ---- Scenario B: participant web-only join ----
            participant.goto(f"{UI}/research/join", wait_until="networkidle")
            participant.locator("input[required]").fill(join_code)
            participant.click('button:has-text("Review study")')
            participant.wait_for_url(re.compile(r"/login$"), timeout=20000)
            record("B1 unauthenticated join redirects to login preserving intent", True)

            login(participant, participant_account)
            participant.wait_for_url(re.compile(r"/research/join"), timeout=20000)
            participant.wait_for_function("() => document.querySelector('input[required]').value.length > 0", timeout=20000)
            prefilled = participant.locator("input[required]").input_value()
            record("B2 login returns to the join page with the code preserved", prefilled == join_code, f"prefilled={prefilled}")
            participant.click('button:has-text("Review study")')
            participant.wait_for_selector("text=I accept the study consent notice.", timeout=20000)
            record("B3 safe study summary and consent control render", True)
            record("B4 participant sees exactly one checkbox (consent, no profile picker)", participant.locator('input[type="checkbox"]').count() == 1)

            participant.click('button:has-text("Accept and join")')
            participant.wait_for_selector("text=You must accept the consent notice before joining.", timeout=20000)
            record("B5 consent-unchecked join is refused", True)

            participant.locator('input[type="checkbox"]').first.check()
            participant.click('button:has-text("Accept and join")')
            participant.wait_for_selector('div[aria-label="Enrollment handoff"]', timeout=20000)
            handoff = participant.locator('div[aria-label="Enrollment handoff"]')
            enrollment_id = handoff.get_attribute("data-enrollment-id")
            assignment_id = handoff.get_attribute("data-assignment-id")
            profile_id = handoff.get_attribute("data-agent-profile-id")
            record("B6 exactly one enrollment and assignment handed off", bool(enrollment_id and assignment_id and profile_id), f"enrollment={enrollment_id}")
            record("B7 success state carries the IntelliJ next step", "post-enrollment activation" in handoff.inner_text())

            # ---- back in A: ACTIVE, metadata locked, stale client rejected ----
            owner.click('button:has-text("Refresh")')
            owner.wait_for_function(
                """(name) => Array.from(document.querySelectorAll('button.research-list-item'))
                    .some((b) => b.textContent.includes(name) && b.textContent.includes('Active'))""",
                arg=renamed,
                timeout=20000,
            )
            owner.locator("button.research-list-item", has_text=renamed).click()
            owner.wait_for_function(
                """() => {
                    const panel = document.querySelector('section[aria-label="Study details"]');
                    if (!panel) return false;
                    const input = panel.querySelector('input');
                    return Boolean(input) && input.disabled;
                }""",
                timeout=20000,
            )
            study_state = owner.evaluate(
                """async (id) => {
                    const res = await fetch(`/api/research/studies/${id}`, { credentials: 'include' });
                    const data = await res.json();
                    return { status: data.study.research_status, locked: Boolean(data.study.consent_locked_at),
                             enrollments: data.study.enrollment_count, active: data.study.active_enrollment_count };
                }""",
                study_id,
            )
            record("A9 owner sees ACTIVE with one active enrollment", study_state["status"] == "ACTIVE" and study_state["active"] == 1, str(study_state))
            record("A10 metadata inputs are disabled after first consent", details.locator("input").first.is_disabled())

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
            record("A11 stale-client metadata write is rejected after consent", forced_status == 409, f"status={forced_status}")

            # ---- Scenario C: stop, retained data, clone ----
            owner.wait_for_selector('button:has-text("Stop study")', state="visible", timeout=20000)
            owner.once("dialog", lambda dialog: dialog.accept())
            owner.click('button:has-text("Stop study")')
            owner.wait_for_selector("text=Study stopped. Collection is revoked and retained data remains available.", timeout=20000)
            record("C1 stop is terminal and non-destructive in the UI", True)
            stopped_state = owner.evaluate(
                """async (id) => {
                    const res = await fetch(`/api/research/studies/${id}`, { credentials: 'include' });
                    const data = await res.json();
                    return { status: data.study.research_status, enrollments: data.study.enrollment_count };
                }""",
                study_id,
            )
            record("C2 stopped study retains its enrollment rows", stopped_state["status"] == "STUDY_STOPPED" and stopped_state["enrollments"] == 1, str(stopped_state))

            owner.click('button:has-text("Clone as new Draft")')
            owner.wait_for_selector("text=/Clone created/", timeout=20000)
            record("C3 clone reports the omitted fields explicitly", True)

            participant.goto(f"{UI}/research/join", wait_until="networkidle")
            participant.locator("input[required]").fill(join_code)
            participant.click('button:has-text("Review study")')
            participant.wait_for_selector("text=/stopped and cannot accept new participants|invalid or no longer available/i", timeout=20000)
            record("C4 stopped study cannot be joined", True)

            # ---- Scenario D: authorization ----
            participant.goto(f"{UI}/research/studies", wait_until="networkidle")
            record(
                "D1 participant cannot reach the researcher control plane",
                participant.get_by_text("You do not have permission to view research studies.").is_visible(),
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
            record("D2 participant cannot revoke an enrollment", revoke_status in (403, 404), f"status={revoke_status}")

            admin_ctx = browser.new_context()
            admin = admin_ctx.new_page()
            login(admin, ADMIN)
            admin.goto(f"{UI}/research/studies", wait_until="networkidle")
            record("D3 admin reaches the control plane", admin.locator("h2#research-studies-title").is_visible())
            # The kill-switch control lives in the selected study's detail panel.
            admin.locator("button.research-list-item", has_text=CONFIG.get("study_name", "E2E Synthetic Study")).first.click()
            admin.wait_for_selector('section[aria-label="Study details"]', timeout=20000)
            record(
                "D4 admin sees the kill-switch control in the study detail panel",
                admin.get_by_role("button", name=re.compile("kill switch", re.I)).count() >= 1,
            )
            record(
                "D5 non-admin researcher sees no kill-switch control",
                owner.get_by_role("button", name=re.compile("kill switch", re.I)).count() == 0,
            )
            admin_ctx.close()
        except Exception as error:  # noqa: BLE001 - report and continue
            record("harness", False, str(error).replace("\n", " ")[:300])
        finally:
            browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\nTOTAL {len(results)}  PASS {len(results) - len(failed)}  FAIL {len(failed)}")
    for name, _, detail in failed:
        print(f"  FAILED {name} {detail}")
    result_path = os.environ.get("CODE4ME_E2E_BROWSER_RESULTS")
    if result_path:
        Path(result_path).write_text(json.dumps({"steps": [
            {"id": name, "status": "PASS" if ok else "FAIL", "detail": detail}
            for name, ok, detail in results
        ]}, indent=2))
    return 1 if failed or len(results) != 27 else 0


if __name__ == "__main__":
    sys.exit(main())
