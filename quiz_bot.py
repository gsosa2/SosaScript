"""
D2L Quiz Bot

Connects to your already-open Chrome window (no new window opened, no login needed).
Answers each question using Claude AI with a random 45-90 second delay.

--- SETUP (one-time) ---
Quit Chrome completely, then launch it with remote debugging enabled:

  Mac:
    /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222

  Then open D2L, log in, and navigate to your quiz tab as normal.

--- RUN ---
  python quiz_bot.py --url <D2L quiz URL>

Environment variables:
    ANTHROPIC_API_KEY  – your Anthropic API key (required)
"""

import argparse
import os
import random
import time

import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


CDP_URL = "http://localhost:9222"

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
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PWTimeoutError:
        pass
    time.sleep(1)


def find_quiz_page(context, url: str):
    """Find the tab that matches the quiz URL."""
    for page in context.pages:
        if "quizzing" in page.url or url in page.url:
            print(f"[+] Found quiz tab: {page.url}")
            page.bring_to_front()
            return page
    # Not found – open in a new tab
    print("[*] Quiz tab not found – opening it now...")
    page = context.new_page()
    page.goto(url, timeout=30_000)
    wait_for_page(page)
    return page


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

def dump_page(page) -> None:
    """Save page HTML to a file for selector debugging."""
    path = os.path.expanduser("~/Downloads/quiz_debug.html")
    with open(path, "w") as f:
        f.write(page.content())
    print(f"[debug] Page HTML saved to {path}")


def run_quiz(url: str, debug: bool = False) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY before running.")

    client = anthropic.Anthropic(api_key=api_key)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(CDP_URL)
        except Exception:
            raise SystemExit(
                "\n[!] Could not connect to Chrome.\n"
                "    Make sure Chrome is running with remote debugging:\n\n"
                "    open -a 'Google Chrome' --args --remote-debugging-port=9222\n"
            )

        print("[+] Connected to your Chrome session.")
        context = browser.contexts[0]
        page = find_quiz_page(context, url)

        print("[+] Starting quiz...\n")

        if debug:
            dump_page(page)
            print("[debug] Exiting after page dump. Check ~/Downloads/quiz_debug.html")
            return

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

        print("[+] Done.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="D2L quiz bot – attaches to your open Chrome tab.")
    parser.add_argument("--url", required=True, help="Full URL of the D2L quiz page.")
    parser.add_argument("--debug", action="store_true", help="Save page HTML and exit (for fixing selectors).")
    args = parser.parse_args()
    run_quiz(args.url, debug=args.debug)


if __name__ == "__main__":
    main()
