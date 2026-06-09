"""
D2L Quiz Bot

Connects to your already-open Chrome window (no new window opened, no login needed).
Answers each question using Claude AI with a random 45-90 second delay.
Handles both text-only and image-based questions by screenshotting the question area.

--- SETUP (one-time) ---
Launch Chrome with remote debugging:

  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &

  Then open D2L, log in, and navigate to your quiz tab.

--- RUN ---
  python3 quiz_bot.py --url <D2L quiz URL>

Environment variables:
    ANTHROPIC_API_KEY  – your Anthropic API key (required)
"""

import argparse
import base64
import os
import random
import time

import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


CDP_URL = "http://localhost:9222"

# ---------------------------------------------------------------------------
# Selectors (GGC D2L / Brightspace)
# ---------------------------------------------------------------------------

# Question text lives inside a shadow DOM inside <d2l-html-block>
SEL_QUESTION_BLOCK = 'd2l-html-block'

# Answer rows – each row has onclick="SetRadioButtonAsSelected(...)"
SEL_ANSWER_ROWS    = 'tr.d2l-rowshadeonhover'

# The text of each answer is in the second td (class d_tb or d_tw)
SEL_ANSWER_TEXT_TD = 'td.d_tb, td.d_tw'

SEL_NEXT_BTN       = (
    'button:has-text("Next Page"), '
    'button:has-text("Next"), '
    'button:has-text("Save & Next")'
)
SEL_SUBMIT_BTN     = (
    'button:has-text("Submit Quiz"), '
    'a:has-text("Submit Quiz")'
)
SEL_CONFIRM_SUBMIT = (
    'button:has-text("Yes"), '
    '.d2l-dialog-footer button:first-child'
)


# ---------------------------------------------------------------------------
# Claude helper – handles text-only and image questions
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

    if screenshot_b64:
        # Send both the screenshot and the text so Claude can see any images
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            },
            {"type": "text", "text": text_prompt},
        ]
    else:
        content = text_prompt

    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=16,
        messages=[{"role": "user", "content": content}],
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
    for page in context.pages:
        if "quizzing" in page.url or url in page.url:
            print(f"[+] Found quiz tab: {page.url}")
            page.bring_to_front()
            return page
    print("[*] Quiz tab not found – opening it now...")
    page = context.new_page()
    page.goto(url, timeout=30_000)
    wait_for_page(page)
    return page


def get_quiz_frame(page):
    """
    D2L nests the quiz in one or more iframes. Walk all frames and pick
    the deepest one that contains quiz question content.
    """
    print(f"[+] All frames found:")
    for frame in page.frames:
        print(f"    {frame.url}")

    # Prefer the innermost frame with actual question content
    for frame in reversed(page.frames):
        if frame == page.main_frame:
            continue
        try:
            has_content = frame.evaluate("""() => {
                return document.querySelectorAll('d2l-html-block, tr.d2l-rowshadeonhover, .dfs_m').length > 0;
            }""")
            if has_content:
                print(f"[+] Using quiz frame: {frame.url}")
                return frame
        except Exception:
            continue

    print("[*] No quiz frame found – using main page.")
    return page


def html_attr_to_text(html: str) -> str:
    """Strip HTML tags from a d2l-html-block html attribute value."""
    import re
    return re.sub(r'<[^>]+>', '', html).replace('&amp;', '&').replace('&#160;', ' ').replace('&lt;', '<').replace('&gt;', '>').strip()


def scrape_question(frame) -> tuple[str, list[str]]:
    frame.wait_for_selector(SEL_QUESTION_BLOCK, timeout=15_000)

    # Question text is in the html attribute of the first d2l-html-block
    question_text = frame.evaluate("""() => {
        const blocks = document.querySelectorAll('d2l-html-block');
        // First block is the question stem (not inside an answer row)
        for (const b of blocks) {
            if (!b.closest('tr.d2l-rowshadeonhover')) {
                const html = b.getAttribute('html') || '';
                const div = document.createElement('div');
                div.innerHTML = html;
                return div.innerText.trim();
            }
        }
        return '';
    }""")

    # Answer text: each answer row has a d2l-html-block in the second td
    choices = frame.evaluate("""() => {
        const rows = document.querySelectorAll('tr.d2l-rowshadeonhover');
        return Array.from(rows).map(row => {
            const block = row.querySelector('td.d_tb d2l-html-block, td.d_tw d2l-html-block');
            if (!block) return '';
            const html = block.getAttribute('html') || '';
            const div = document.createElement('div');
            div.innerHTML = html;
            return div.innerText.trim();
        }).filter(Boolean);
    }""")

    return question_text, choices


def screenshot_question(frame) -> str | None:
    """Screenshot the question block and return base64 PNG, or None on failure."""
    try:
        el = frame.query_selector(SEL_QUESTION_BLOCK)
        png_bytes = el.screenshot() if el else frame.screenshot()
        return base64.b64encode(png_bytes).decode()
    except Exception as e:
        print(f"  [!] Screenshot failed: {e}")
        return None


def question_has_image(frame) -> bool:
    """Return True if the question stem's html attribute contains an <img> tag."""
    try:
        return frame.evaluate("""() => {
            const blocks = document.querySelectorAll('d2l-html-block');
            for (const b of blocks) {
                if (!b.closest('tr.d2l-rowshadeonhover')) {
                    return (b.getAttribute('html') || '').includes('<img');
                }
            }
            return false;
        }""")
    except Exception:
        return False


def select_answer(frame, letter: str, choices: list[str]) -> None:
    index = ord(letter) - ord("A")
    if index < 0 or index >= len(choices):
        print(f"  [!] Letter '{letter}' out of range – defaulting to A.")
        index = 0

    rows = frame.query_selector_all(SEL_ANSWER_ROWS)
    if rows and index < len(rows):
        rows[index].click()


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
# Debug helper
# ---------------------------------------------------------------------------

def dump_page(page, frame=None) -> None:
    path = os.path.expanduser("~/Downloads/quiz_debug.html")
    # Try the iframe first; fall back to full page
    saved = False
    if frame and frame != page:
        try:
            html = frame.content()
            with open(path, "w") as f:
                f.write(html)
            saved = True
        except Exception:
            pass
    if not saved:
        # Pull HTML from every frame via JS and concatenate
        all_html = page.evaluate("""() => {
            let out = '<!-- MAIN PAGE -->' + document.documentElement.outerHTML;
            for (const iframe of document.querySelectorAll('iframe')) {
                try {
                    out += '\\n\\n<!-- IFRAME: ' + iframe.src + ' -->\\n';
                    out += iframe.contentDocument.documentElement.outerHTML;
                } catch(e) {
                    out += '<!-- could not access iframe: ' + e + ' -->';
                }
            }
            return out;
        }""")
        with open(path, "w") as f:
            f.write(all_html)
    print(f"[debug] HTML saved to {path}")


# ---------------------------------------------------------------------------
# Main loop
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
                "    Launch Chrome with:\n\n"
                '    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
                "--remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &\n"
            )

        print("[+] Connected to your Chrome session.")
        context = browser.contexts[0]
        page = find_quiz_page(context, url)
        frame = get_quiz_frame(page)

        print("[+] Starting quiz...\n")

        if debug:
            dump_page(page, frame)
            print("[debug] Exiting after page dump. Check ~/Downloads/quiz_debug.html")
            return

        question_num = 0
        while True:
            question_num += 1
            print(f"[Q{question_num}] Reading question...")
            try:
                question_text, choices = scrape_question(frame)
            except PWTimeoutError:
                print("[!] No question found – quiz may be complete.")
                break

            if not choices:
                print(f"  [!] No answer choices found – skipping.")
            else:
                delay = random.randint(45, 90)
                print(f"  Question : {question_text[:120]}...")
                print(f"  Choices  : {choices}")

                # Always screenshot — captures images if present, harmless if not
                screenshot_b64 = screenshot_question(frame)
                has_img = question_has_image(frame)
                if has_img:
                    print(f"  [+] Image detected — sending screenshot to Claude.")

                print(f"  Waiting  : {delay}s before answering...")
                time.sleep(delay)

                answer_letter = ask_claude(client, question_text, choices, screenshot_b64)
                print(f"  Answer   : {answer_letter}")
                select_answer(frame, answer_letter, choices)
                time.sleep(1)

            if not advance(frame):
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
