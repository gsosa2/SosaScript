"""
D2L Quiz Bot (GGC / Brightspace)

Connects to your already-open Chrome window and answers each quiz question
using Claude AI with a random 45-90 second delay per question.
Handles text questions, image questions, and multi-page quizzes.

--- SETUP (one-time) ---
Quit Chrome, then relaunch with remote debugging:

  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome \
    --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &

  Log into D2L and open your quiz tab normally.

--- RUN ---
  python3 quiz_bot.py --url <D2L quiz URL>

Environment variables:
    ANTHROPIC_API_KEY  – required
"""

import argparse
import base64
import os
import random
import re
import time

import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

CDP_URL = "http://localhost:9222"


# ---------------------------------------------------------------------------
# JS helpers executed inside the quiz frame
# ---------------------------------------------------------------------------

JS_GET_QUESTION = """() => {
    // Question stem: first d2l-html-block NOT inside an answer row
    const blocks = document.querySelectorAll('d2l-html-block');
    for (const b of blocks) {
        if (!b.closest('tr.d2l-rowshadeonhover')) {
            const html = b.getAttribute('html') || b.innerHTML || '';
            const div = document.createElement('div');
            div.innerHTML = html;
            return div.innerText.trim();
        }
    }
    // Fallback: heading text
    const h = document.querySelector('h2.d2l-quiz-skip-nav-target');
    return h ? h.innerText.trim() : '';
}"""

JS_GET_CHOICES = """() => {
    const rows = document.querySelectorAll('tr.d2l-rowshadeonhover');
    return Array.from(rows).map(row => {
        // Answer text is in the wide td (d_tb or d_tw)
        const block = row.querySelector('td.d_tb d2l-html-block, td.d_tw d2l-html-block');
        if (block) {
            const html = block.getAttribute('html') || block.innerHTML || '';
            const div = document.createElement('div');
            div.innerHTML = html;
            return div.innerText.replace(/\\u00a0/g, ' ').trim();
        }
        // Fallback: plain text of td
        const td = row.querySelector('td.d_tb, td.d_tw');
        return td ? td.innerText.trim() : '';
    }).filter(Boolean);
}"""

JS_HAS_IMAGE = """() => {
    const blocks = document.querySelectorAll('d2l-html-block');
    for (const b of blocks) {
        if (!b.closest('tr.d2l-rowshadeonhover')) {
            const html = b.getAttribute('html') || b.innerHTML || '';
            if (html.toLowerCase().includes('<img')) return true;
        }
    }
    return false;
}"""

JS_PAGE_INFO = """() => {
    // Returns {current, total} from "Page X of Y" label
    const labels = document.querySelectorAll('label');
    for (const l of labels) {
        const m = l.innerText.match(/Page\\s+(\\d+)\\s+of\\s+(\\d+)/i);
        if (m) return {current: parseInt(m[1]), total: parseInt(m[2])};
    }
    // Fallback: hidden input pg
    const pg = document.querySelector('input[name="pg"]');
    const total = document.querySelector('input[name="z_d"]');
    return {
        current: pg ? parseInt(pg.value) : null,
        total: total ? parseInt(total.value) : null
    };
}"""

JS_IS_ANSWERED = """() => {
    // True if any answer row is selected
    return document.querySelector('tr.d2l-rowshadeonhover-selected') !== null
        || document.querySelector('input.d2l-radio:checked') !== null;
}"""

JS_CLICK_ANSWER = """(index) => {
    const rows = document.querySelectorAll('tr.d2l-rowshadeonhover');
    if (rows[index]) {
        rows[index].click();
        return true;
    }
    // Fallback: click the radio directly
    const radios = document.querySelectorAll('input.d2l-radio');
    if (radios[index]) { radios[index].click(); return true; }
    return false;
}"""


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def ask_claude(
    client: anthropic.Anthropic,
    question_text: str,
    choices: list[str],
    screenshot_b64: str | None = None,
) -> str:
    lettered = "\n".join(f"{chr(65 + i)}) {c}" for i, c in enumerate(choices))
    text_prompt = (
        "Answer the following multiple-choice question. "
        "Reply with ONLY the letter of the best answer (A, B, C, …).\n\n"
        f"Question:\n{question_text}\n\n"
        f"Choices:\n{lettered}"
    )
    content: list | str
    if screenshot_b64:
        content = [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": screenshot_b64}},
            {"type": "text", "text": text_prompt},
        ]
    else:
        content = text_prompt

    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=16,
        messages=[{"role": "user", "content": content}],
    )
    letter = msg.content[0].text.strip().upper()
    # In case Claude returns "A)" or "A." strip trailing punctuation
    letter = re.sub(r'[^A-Z]', '', letter)
    return letter[0] if letter else "A"


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def countdown(label: str, seconds: int) -> None:
    """Print a live updating countdown timer in the terminal."""
    for remaining in range(seconds, 0, -1):
        mins, secs = divmod(remaining, 60)
        print(f"  {label}: {mins:02d}:{secs:02d} remaining...  ", end="\r", flush=True)
        time.sleep(1)
    print(f"  {label}: done!                    ")


def wait_for_page(frame) -> None:
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=10_000)
    except PWTimeoutError:
        pass
    time.sleep(1.5)


def find_quiz_page(context, url: str):
    for page in context.pages:
        if "quizzing" in page.url or url in page.url:
            print(f"[+] Found quiz tab: {page.url}")
            page.bring_to_front()
            return page
    print("[*] Opening quiz URL in new tab...")
    page = context.new_page()
    page.goto(url, timeout=30_000)
    wait_for_page(page)
    return page


def get_quiz_frame(page):
    """Find the frame that actually contains quiz question elements."""
    all_frames = page.frames
    print(f"[+] Frames detected: {len(all_frames)}")
    for f in all_frames:
        print(f"      {f.url[:100]}")

    # Walk frames deepest-first, find one with quiz content
    for frame in reversed(all_frames):
        if frame == page.main_frame:
            continue
        try:
            count = frame.evaluate(
                "() => document.querySelectorAll("
                "'d2l-html-block, tr.d2l-rowshadeonhover, .dfs_m, input.d2l-radio').length"
            )
            if count > 0:
                print(f"[+] Quiz frame found ({count} elements): {frame.url[:80]}")
                return frame
        except Exception:
            continue

    print("[*] No quiz iframe found – using main page frame.")
    return page.main_frame


def screenshot_frame(frame) -> str | None:
    try:
        # Screenshot just the question block if possible
        el = frame.query_selector('d2l-html-block:not(tr.d2l-rowshadeonhover d2l-html-block)')
        if not el:
            el = frame.query_selector('.dfs_m, fieldset')
        png = el.screenshot() if el else frame.screenshot()
        return base64.b64encode(png).decode()
    except Exception as e:
        print(f"  [!] Screenshot failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Quiz logic
# ---------------------------------------------------------------------------

def scrape_question(frame) -> tuple[str, list[str]]:
    frame.wait_for_selector('d2l-html-block, tr.d2l-rowshadeonhover', timeout=15_000)
    question_text: str = frame.evaluate(JS_GET_QUESTION)
    choices: list[str] = frame.evaluate(JS_GET_CHOICES)
    return question_text, choices


def select_answer(frame, letter: str, choices: list[str]) -> bool:
    index = ord(letter) - ord("A")
    if index < 0 or index >= len(choices):
        print(f"  [!] Letter '{letter}' out of range (have {len(choices)} choices) – using A.")
        index = 0
    clicked = frame.evaluate(JS_CLICK_ANSWER, index)
    time.sleep(0.8)
    # Verify selection registered
    answered = frame.evaluate(JS_IS_ANSWERED)
    if not answered:
        print("  [!] Answer may not have registered – retrying click.")
        frame.evaluate(JS_CLICK_ANSWER, index)
        time.sleep(0.8)
    return answered


def get_page_info(frame) -> dict:
    try:
        return frame.evaluate(JS_PAGE_INFO)
    except Exception:
        return {"current": None, "total": None}


def advance(frame) -> bool:
    """Click Next Page if available; Submit if on last page. Returns False when done."""
    # Try Next Page button (use :first-of-type to avoid double-clicking)
    for text in ["Next Page", "Next", "Save & Next"]:
        btns = frame.query_selector_all(f'button:has-text("{text}")')
        for btn in btns:
            if btn.is_visible() and btn.is_enabled():
                btn.click()
                wait_for_page(frame)
                return True

    # Try Submit Quiz
    for text in ["Submit Quiz"]:
        btns = frame.query_selector_all(f'button:has-text("{text}"), a:has-text("{text}")')
        for btn in btns:
            if btn.is_visible() and btn.is_enabled():
                btn.click()
                # Confirm dialog if it appears
                try:
                    frame.wait_for_selector(
                        'button:has-text("Yes"), .d2l-dialog-footer button',
                        timeout=5_000
                    )
                    for confirm in frame.query_selector_all(
                        'button:has-text("Yes"), .d2l-dialog-footer button'
                    ):
                        if confirm.is_visible():
                            confirm.click()
                            break
                except PWTimeoutError:
                    pass
                wait_for_page(frame)
                print("[+] Quiz submitted.")
                return False

    return False


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------

def dump_page(frame) -> None:
    path = os.path.expanduser("~/Downloads/quiz_debug.html")
    try:
        html = frame.content()
    except Exception:
        html = "<error: could not get frame content>"
    with open(path, "w") as f:
        f.write(html)
    print(f"[debug] Saved to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
                "    Launch Chrome with remote debugging first:\n\n"
                "    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
                "--remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &\n"
            )

        print("[+] Connected to Chrome.")
        context = browser.contexts[0]
        page = find_quiz_page(context, url)
        frame = get_quiz_frame(page)

        if debug:
            dump_page(frame)
            print("[debug] Done. Check ~/Downloads/quiz_debug.html")
            return

        print("\n[+] Starting quiz...\n")
        question_num = 0

        while True:
            question_num += 1
            info = get_page_info(frame)
            page_label = (
                f"page {info['current']}/{info['total']}"
                if info["current"] else f"question {question_num}"
            )
            print(f"[Q{question_num}] Reading question ({page_label})...")

            try:
                question_text, choices = scrape_question(frame)
            except PWTimeoutError:
                print("[!] No question found – quiz may be finished.")
                break

            if not question_text and not choices:
                print("  [!] Empty question – quiz may be complete.")
                break

            if not choices:
                print(f"  [!] No answer choices found – skipping.")
            else:
                delay = random.randint(45, 90)
                print(f"  Question : {question_text[:150]}")
                for i, c in enumerate(choices):
                    print(f"    {chr(65+i)}) {c}")

                has_img = frame.evaluate(JS_HAS_IMAGE)
                screenshot_b64 = screenshot_frame(frame)
                if has_img:
                    print("  [+] Image detected – screenshot sent to Claude.")

                # Live countdown before answering (simulates reading time)
                countdown("Reading", delay)

                answer_letter = ask_claude(client, question_text, choices, screenshot_b64)
                print(f"  Answer   : {answer_letter}) {choices[ord(answer_letter)-65] if ord(answer_letter)-65 < len(choices) else '?'}")
                select_answer(frame, answer_letter, choices)

                # Short pause after selecting before clicking Next (simulates review)
                post_delay = random.randint(3, 8)
                countdown("Reviewing", post_delay)

            if not advance(frame):
                break

        print("\n[+] Done.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="D2L quiz bot – attaches to open Chrome tab.")
    parser.add_argument("--url", required=True, help="Full D2L quiz URL.")
    parser.add_argument("--debug", action="store_true", help="Dump page HTML and exit.")
    args = parser.parse_args()
    run_quiz(args.url, debug=args.debug)


if __name__ == "__main__":
    main()
