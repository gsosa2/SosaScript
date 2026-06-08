"""
D2L Quiz Bot (Zen Browser / Firefox – existing session)

Attaches to your existing Zen Browser session by using your real Zen profile,
so you stay logged in. Open the quiz tab in Zen first, then run this script
with the quiz URL. It will find the tab (or open the URL) and answer each
question automatically.

Usage:
    python quiz_bot.py --url <D2L quiz URL>

Environment variables:
    ANTHROPIC_API_KEY   – your Anthropic API key (required)
    ZEN_PROFILE         – path to your Zen profile directory (auto-detected on macOS)
    ZEN_PATH            – path to the Zen binary (auto-detected on macOS)
"""

import argparse
import os
import random
import time
from pathlib import Path

import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError, BrowserContext


# ---------------------------------------------------------------------------
# macOS default paths for Zen Browser
# ---------------------------------------------------------------------------
ZEN_APP_DEFAULT   = "/Applications/Zen Browser.app/Contents/MacOS/zen"
ZEN_PROFILE_BASE  = Path.home() / "Library/Application Support/zen/Profiles"


# ---------------------------------------------------------------------------
# Selectors  (D2L / Brightspace)
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
# Zen profile detection
# ---------------------------------------------------------------------------

def find_zen_profile() -> str | None:
    """Return the path to the most-recently-used Zen profile on macOS."""
    env_override = os.environ.get("ZEN_PROFILE")
    if env_override:
        return env_override

    if not ZEN_PROFILE_BASE.exists():
        return None

    profiles = [p for p in ZEN_PROFILE_BASE.iterdir() if p.is_dir()]
    if not profiles:
        return None

    # Pick the profile modified most recently (the active one)
    profiles.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(profiles[0])


def find_zen_binary() -> str | None:
    env_override = os.environ.get("ZEN_PATH")
    if env_override:
        return env_override
    if Path(ZEN_APP_DEFAULT).exists():
        return ZEN_APP_DEFAULT
    return None


# ---------------------------------------------------------------------------
# Claude helper
# ---------------------------------------------------------------------------

def ask_claude(client: anthropic.Anthropic, question_text: str, choices: list[str]) -> str:
    """Return the letter (A, B, C …) of the best answer according to Claude."""
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

def get_quiz_page(context: BrowserContext, url: str):
    """Return the page that matches the quiz URL, or open it in a new tab."""
    for page in context.pages:
        if url in page.url or page.url in url:
            print(f"[+] Found existing tab: {page.url}")
            page.bring_to_front()
            return page

    # Not found – open a new tab
    print(f"[*] Quiz tab not found; opening {url}")
    page = context.new_page()
    page.goto(url, timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=20_000)
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
    """Click Next/Submit. Returns False when the quiz is finished."""
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

    zen_binary  = find_zen_binary()
    zen_profile = find_zen_profile()

    if not zen_binary:
        raise SystemExit(
            "Zen Browser not found at /Applications/Zen Browser.app – "
            "set ZEN_PATH to the correct binary path."
        )
    if not zen_profile:
        raise SystemExit(
            "No Zen profile found – set ZEN_PROFILE to your profile directory."
        )

    print(f"[+] Using Zen binary : {zen_binary}")
    print(f"[+] Using Zen profile: {zen_profile}")

    with sync_playwright() as pw:
        # launch_persistent_context opens Zen with your real profile (cookies/session intact)
        context = pw.firefox.launch_persistent_context(
            user_data_dir=zen_profile,
            executable_path=zen_binary,
            headless=False,   # must be visible to attach to existing session
        )

        page = get_quiz_page(context, url)

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
                print(f"  Waiting : {delay}s before answering…")
                time.sleep(delay)

                answer_letter = ask_claude(client, question_text, choices)
                print(f"  Answer  : {answer_letter}")
                select_answer(page, answer_letter, choices)

            if not advance(page):
                break

        context.close()
        print("[+] Done.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="D2L quiz bot – uses your open Zen Browser session.")
    parser.add_argument("--url", required=True, help="Full URL of the D2L quiz page.")
    args = parser.parse_args()
    run_quiz(args.url)


if __name__ == "__main__":
    main()
