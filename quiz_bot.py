"""
D2L Quiz Bot (Firefox)

Opens a Firefox browser window, navigates to the quiz URL, and answers
each question using Claude AI with a random 45-90 second delay per question.

Usage:
    python quiz_bot.py --url <D2L quiz URL>

Environment variables:
    ANTHROPIC_API_KEY  – your Anthropic API key (required)
"""

import argparse
import os
import random
import time
from pathlib import Path

import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


# ---------------------------------------------------------------------------
# Selectors (D2L / Brightspace)
# ---------------------------------------------------------------------------
SEL_QUESTION_STEM = (
    '.d2l-htmleditor-container, '
    '.qnTitle, '
    '[class*="question-text"], '
    '.ds-question-stem, '
    'd2l-html-block'
)
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
    '.d2l-dialog-footer button:first-child'
)
SEL_LOGIN_USER = 'input[name="username"], input[type="email"], #userName'
SEL_LOGIN_PASS = 'input[name="password"], input[type="password"], #password'
SEL_LOGIN_BTN  = 'button[type="submit"], input[type="submit"], #loginButton'


# ---------------------------------------------------------------------------
# Claude helper
# ---------------------------------------------------------------------------

def ask_claude(client: anthropic.Anthropic, question_text: str, choices: list[str]) -> str:
    lettered = "\n".join(f"{chr(65 + i)}) {c}" for i, c in enumerate(choices))
    prompt = (
        "Answer the following multiple-choice question. "
        "Reply with ONLY the letter of the best answer (A, B, C, …).\n\n"
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

def scrape_question(page) -> tuple[str, list[str]]:
    page.wait_for_selector(SEL_QUESTION_STEM, timeout=15_000)

    stems = page.query_selector_all(SEL_QUESTION_STEM)
    question_text = " ".join(
        el.inner_text().strip() for el in stems if el.inner_text().strip()
    )

    labels = page.query_selector_all(SEL_ANSWERS)
    choices = [lbl.inner_text().strip() for lbl in labels if lbl.inner_text().strip()]

    return question_text, choices


def select_answer(page, letter: str, choices: list[str]) -> None:
    index = ord(letter) - ord("A")
    if index < 0 or index >= len(choices):
        print(f"  [!] Letter '{letter}' out of range – defaulting to A.")
        index = 0

    radios = page.query_selector_all(SEL_RADIO_INPUTS)
    if radios and index < len(radios):
        radios[index].click()
        return

    checkboxes = page.query_selector_all(SEL_CHECKBOX_INPUT)
    if checkboxes and index < len(checkboxes):
        checkboxes[index].click()


def advance(page) -> bool:
    for btn in page.query_selector_all(SEL_NEXT_BTN):
        if btn.is_visible() and btn.is_enabled():
            btn.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
            return True

    for btn in page.query_selector_all(SEL_SUBMIT_BTN):
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

def run_quiz(url: str) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY before running.")

    client = anthropic.Anthropic(api_key=api_key)

    # Resolve Playwright's own bundled Firefox so macOS doesn't open Zen/system Firefox
    import subprocess, sys
    try:
        pw_firefox = subprocess.check_output(
            [sys.executable, "-m", "playwright", "run-driver"],
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pw_firefox = None

    # Find the bundled firefox binary from the playwright package location
    import importlib.util
    spec = importlib.util.find_spec("playwright")
    pw_path = Path(spec.origin).parent if spec else None
    bundled_firefox = None
    if pw_path:
        for candidate in pw_path.rglob("firefox/firefox"):
            bundled_firefox = str(candidate)
            break
        if not bundled_firefox:
            # macOS binary name
            for candidate in pw_path.rglob("firefox/Nightly.app/Contents/MacOS/firefox"):
                bundled_firefox = str(candidate)
                break

    with sync_playwright() as pw:
        launch_kwargs: dict = {"headless": False}
        if bundled_firefox and Path(bundled_firefox).exists():
            print(f"[+] Using bundled Firefox: {bundled_firefox}")
            launch_kwargs["executable_path"] = bundled_firefox
        else:
            print("[*] Could not locate bundled Firefox – using default.")
        browser = pw.firefox.launch(**launch_kwargs)
        page = browser.new_page()

        print(f"[+] Opening {url}")
        page.goto(url, timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=20_000)

        # If a login form appears, pause and let the user log in manually
        try:
            page.wait_for_selector(SEL_LOGIN_USER, timeout=6_000)
            print("\n[!] Login page detected.")
            print("    Please log in manually in the browser window, then press Enter here to continue...")
            input()
            page.wait_for_load_state("networkidle", timeout=20_000)
            # Re-navigate to quiz if login redirected elsewhere
            if url not in page.url:
                page.goto(url, timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeoutError:
            pass  # No login form – already on the quiz

        question_num = 0
        while True:
            question_num += 1
            try:
                question_text, choices = scrape_question(page)
            except PWTimeoutError:
                print("[!] No question found – quiz may be complete.")
                break

            if not choices:
                print(f"  [!] Q{question_num}: No answer choices detected – skipping.")
            else:
                delay = random.randint(45, 90)
                print(f"\n[Q{question_num}] {question_text[:120]}...")
                print(f"  Choices : {choices}")
                print(f"  Waiting : {delay}s before answering...")
                time.sleep(delay)

                answer_letter = ask_claude(client, question_text, choices)
                print(f"  Answer  : {answer_letter}")
                select_answer(page, answer_letter, choices)

            if not advance(page):
                break

        browser.close()
        print("[+] Done.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="D2L quiz bot powered by Claude.")
    parser.add_argument("--url", required=True, help="Full URL of the D2L quiz page.")
    args = parser.parse_args()
    run_quiz(args.url)


if __name__ == "__main__":
    main()
