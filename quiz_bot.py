"""
D2L Quiz Bot (Zen Browser / Firefox)
- Reads each quiz question using Playwright with Firefox
- Sends the question to Claude to get the best answer
- Waits a random 45–90 seconds before selecting and submitting the answer
- Advances to the next question automatically

Usage:
    python quiz_bot.py --url <D2L quiz URL> --username <email> --password <pass>

Environment variables (alternative to CLI flags):
    D2L_USERNAME, D2L_PASSWORD, ANTHROPIC_API_KEY

Optional – point to your local Zen Browser binary instead of bundled Firefox:
    ZEN_PATH=/usr/bin/zen-browser  (or wherever Zen is installed)
"""

import argparse
import os
import random
import time

import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


# ---------------------------------------------------------------------------
# Selectors (D2L / Brightspace – adjust if your institution customises them)
# ---------------------------------------------------------------------------
SEL_USERNAME   = 'input[name="username"], input[type="email"], #userName'
SEL_PASSWORD   = 'input[name="password"], input[type="password"], #password'
SEL_LOGIN_BTN  = 'button[type="submit"], input[type="submit"], #loginButton'

# Quiz page
SEL_QUESTION_STEM  = '.d2l-htmleditor-container, .qnTitle, [class*="question-text"], .ds-question-stem'
SEL_ANSWERS        = (
    'input[type="radio"] + label, '
    'input[type="radio"] ~ span, '
    'label.d2l-label-text, '
    '.ds-answer-option label'
)
SEL_RADIO_INPUTS   = 'input[type="radio"]'
SEL_CHECKBOX_INPUT = 'input[type="checkbox"]'
SEL_NEXT_BTN       = (
    'button:has-text("Next"), '
    'button:has-text("Save & Next"), '
    '[data-keybinding="next-question"]'
)
SEL_SUBMIT_BTN     = (
    'button:has-text("Submit Quiz"), '
    'button:has-text("Submit"), '
    'a:has-text("Submit Quiz")'
)
SEL_CONFIRM_SUBMIT = (
    'button:has-text("Yes"), '
    'button:has-text("Submit"), '
    '.d2l-dialog-footer button:first-child'
)


# ---------------------------------------------------------------------------
# Claude helper
# ---------------------------------------------------------------------------

def ask_claude(client: anthropic.Anthropic, question_text: str, choices: list[str]) -> str:
    """Return the letter (A, B, C …) of the best answer according to Claude."""
    lettered = "\n".join(f"{chr(65 + i)}) {c}" for i, c in enumerate(choices))
    prompt = (
        f"Answer the following multiple-choice question. "
        f"Reply with ONLY the letter of the best answer (A, B, C, …).\n\n"
        f"Question:\n{question_text}\n\n"
        f"Choices:\n{lettered}"
    )
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=16,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip().upper()


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------

def login(page, username: str, password: str) -> None:
    page.wait_for_selector(SEL_USERNAME, timeout=15_000)
    page.fill(SEL_USERNAME, username)
    page.fill(SEL_PASSWORD, password)
    page.click(SEL_LOGIN_BTN)
    page.wait_for_load_state("networkidle", timeout=20_000)
    print("[+] Logged in.")


def scrape_question(page) -> tuple[str, list[str]]:
    """Return (question_text, [choice_text, ...])."""
    page.wait_for_selector(SEL_QUESTION_STEM, timeout=15_000)

    # Question stem
    stems = page.query_selector_all(SEL_QUESTION_STEM)
    question_text = " ".join(el.inner_text().strip() for el in stems if el.inner_text().strip())

    # Answer labels
    labels = page.query_selector_all(SEL_ANSWERS)
    choices = [lbl.inner_text().strip() for lbl in labels if lbl.inner_text().strip()]

    return question_text, choices


def select_answer(page, letter: str, choices: list[str]) -> None:
    """Click the radio / checkbox that corresponds to `letter`."""
    index = ord(letter) - ord("A")
    if index < 0 or index >= len(choices):
        print(f"  [!] Letter {letter} out of range – defaulting to A.")
        index = 0

    radios = page.query_selector_all(SEL_RADIO_INPUTS)
    if radios:
        if index < len(radios):
            radios[index].click()
            return

    checkboxes = page.query_selector_all(SEL_CHECKBOX_INPUT)
    if checkboxes and index < len(checkboxes):
        checkboxes[index].click()


def advance(page) -> bool:
    """Click Next if present; return False when we reach Submit."""
    next_btns = page.query_selector_all(SEL_NEXT_BTN)
    for btn in next_btns:
        if btn.is_visible() and btn.is_enabled():
            btn.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
            return True

    submit_btns = page.query_selector_all(SEL_SUBMIT_BTN)
    for btn in submit_btns:
        if btn.is_visible() and btn.is_enabled():
            btn.click()
            try:
                page.wait_for_selector(SEL_CONFIRM_SUBMIT, timeout=5_000)
                page.click(SEL_CONFIRM_SUBMIT)
            except PWTimeoutError:
                pass
            page.wait_for_load_state("networkidle", timeout=15_000)
            print("[+] Quiz submitted.")
            return False

    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_quiz(url: str, username: str, password: str, headless: bool) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY environment variable not set.")

    client = anthropic.Anthropic(api_key=api_key)

    # Zen Browser is Firefox-based; pass --zen-path to use your local Zen install,
    # otherwise Playwright falls back to its own bundled Firefox.
    zen_path = os.environ.get("ZEN_PATH")  # e.g. /usr/bin/zen-browser

    with sync_playwright() as pw:
        launch_kwargs: dict = {"headless": headless}
        if zen_path:
            launch_kwargs["executable_path"] = zen_path
        browser = pw.firefox.launch(**launch_kwargs)
        context = browser.new_context()
        page = context.new_page()

        print(f"[+] Navigating to {url}")
        page.goto(url, timeout=30_000)

        # Login if a login form appears
        try:
            page.wait_for_selector(SEL_USERNAME, timeout=8_000)
            login(page, username, password)
            # Navigate to the quiz URL again after login if redirected elsewhere
            if url not in page.url:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeoutError:
            print("[*] No login form detected – assuming already authenticated.")

        question_num = 0
        while True:
            question_num += 1
            try:
                question_text, choices = scrape_question(page)
            except PWTimeoutError:
                print("[!] Could not find question stem – quiz may be complete.")
                break

            if not choices:
                print(f"  [!] Q{question_num}: No answer choices found – skipping.")
            else:
                delay = random.randint(45, 90)
                print(f"\n[Q{question_num}] {question_text[:120]}...")
                print(f"  Choices: {choices}")
                print(f"  Waiting {delay}s before answering...")
                time.sleep(delay)

                answer_letter = ask_claude(client, question_text, choices)
                print(f"  Claude says: {answer_letter}")
                select_answer(page, answer_letter, choices)

            has_next = advance(page)
            if not has_next:
                break

        browser.close()
        print("[+] Done.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="D2L quiz bot powered by Claude.")
    parser.add_argument("--url",      required=True,  help="Full URL of the D2L quiz page.")
    parser.add_argument("--username", default=os.environ.get("D2L_USERNAME", ""), help="D2L login username/email.")
    parser.add_argument("--password", default=os.environ.get("D2L_PASSWORD", ""), help="D2L login password.")
    parser.add_argument("--headed",   action="store_true", help="Run with a visible browser window.")
    args = parser.parse_args()

    if not args.username or not args.password:
        raise SystemExit("Provide --username / --password or set D2L_USERNAME / D2L_PASSWORD.")

    run_quiz(
        url=args.url,
        username=args.username,
        password=args.password,
        headless=not args.headed,
    )


if __name__ == "__main__":
    main()
