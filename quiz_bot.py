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
import importlib.util
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


# ---------------------------------------------------------------------------
# Find Playwright's bundled Firefox binary
# ---------------------------------------------------------------------------

def find_bundled_firefox() -> str | None:
    spec = importlib.util.find_spec("playwright")
    if not spec:
        return None
    # Playwright stores browsers under ~/.cache/ms-playwright on macOS/Linux
    cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    if not cache_dir.exists():
        cache_dir = Path.home() / ".cache" / "ms-playwright"
    if not cache_dir.exists():
        return None
    # Look for firefox-*/firefox/firefox  or  firefox-*/firefox/Nightly.app/.../firefox
    for binary in sorted(cache_dir.glob("firefox-*/firefox/firefox"), reverse=True):
        if binary.exists():
            return str(binary)
    for binary in sorted(
        cache_dir.glob("firefox-*/firefox/Nightly.app/Contents/MacOS/firefox"), reverse=True
    ):
        if binary.exists():
            return str(binary)
    return None


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

def wait_for_page(page) -> None:
    """Wait for page to settle — avoids networkidle hanging on D2L."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PWTimeoutError:
        pass
    time.sleep(1)


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
            wait_for_page(page)
            return True

    for btn in page.query_selector_all(SEL_SUBMIT_BTN):
        if btn.is_visible() and btn.is_enabled():
            btn.click()
            try:
                page.wait_for_selector(SEL_CONFIRM_SUBMIT, timeout=5_000)
                page.click(SEL_CONFIRM_SUBMIT)
            except PWTimeoutError:
                pass
            wait_for_page(page)
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

    bundled_firefox = find_bundled_firefox()

    with sync_playwright() as pw:
        launch_kwargs: dict = {"headless": False}
        if bundled_firefox:
            print(f"[+] Using bundled Firefox: {bundled_firefox}")
            launch_kwargs["executable_path"] = bundled_firefox
        else:
            print("[*] Bundled Firefox not found – using system default.")

        browser = pw.firefox.launch(**launch_kwargs)
        page = browser.new_page()

        print(f"[+] Opening quiz URL...")
        page.goto(url, timeout=30_000)
        wait_for_page(page)
        print(f"[+] Page loaded: {page.url}")

        # If login form appears, let user log in manually
        try:
            page.wait_for_selector(SEL_LOGIN_USER, timeout=6_000)
            print("\n[!] Login required.")
            print("    Log in using the browser window, then press Enter here...")
            input()
            wait_for_page(page)
            if url not in page.url:
                print("[+] Navigating back to quiz...")
                page.goto(url, timeout=30_000)
                wait_for_page(page)
        except PWTimeoutError:
            print("[+] No login needed – already on quiz page.")

        print("[+] Starting quiz...\n")
        question_num = 0
        while True:
            question_num += 1
            print(f"[Q{question_num}] Reading question...")
            try:
                question_text, choices = scrape_question(page)
            except PWTimeoutError:
                print("[!] No question found – quiz may be complete.")
                break

            if not choices:
                print(f"  [!] No answer choices found – skipping.")
            else:
                delay = random.randint(45, 90)
                print(f"  Question : {question_text[:120]}...")
                print(f"  Choices  : {choices}")
                print(f"  Waiting  : {delay}s before answering...")
                time.sleep(delay)

                answer_letter = ask_claude(client, question_text, choices)
                print(f"  Answer   : {answer_letter}")
                select_answer(page, answer_letter, choices)
                time.sleep(1)

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
