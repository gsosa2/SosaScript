"""
D2L Quiz Bot — SINGLE PAGE version (GGC / Brightspace)

For quizzes where ALL questions are on one long page (no "Next Page" button).
It iterates through every question block on the page, answers each with Claude
(random 45-90s reading delay + short review delay), then submits at the end.

--- SETUP (one-time) ---
Quit Chrome, then relaunch with remote debugging:

  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome \
    --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &

  Log into D2L and open your quiz tab.

--- RUN ---
  python3 quiz_bot_singlepage.py --url <D2L quiz URL> --subject "Intro to Economics"

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
# JS: collect every question block on the page
# ---------------------------------------------------------------------------
# Each question is a <fieldset class="dfs_m"> with a stem (d2l-html-block
# before/above it) and answer rows (tr.d2l-rowshadeonhover) inside it.

JS_GET_ALL_QUESTIONS = """() => {
    const decode = (html) => {
        const div = document.createElement('div');
        div.innerHTML = html || '';
        return div.innerText.replace(/\\u00a0/g, ' ').trim();
    };

    const result = [];
    const fieldsets = document.querySelectorAll('fieldset.dfs_m');

    fieldsets.forEach((fs, qIndex) => {
        // Question stem: walk up from the fieldset and find the nearest
        // d2l-html-block that is NOT inside an answer row and NOT inside this fieldset.
        let stem = '';
        let wrapper = fs.parentElement;
        while (wrapper && wrapper !== document.body) {
            const blocks = wrapper.querySelectorAll('d2l-html-block');
            for (const b of blocks) {
                if (!b.closest('tr.d2l-rowshadeonhover') && !fs.contains(b)) {
                    stem = decode(b.getAttribute('html') || b.innerHTML);
                    break;
                }
            }
            if (stem) break;
            wrapper = wrapper.parentElement;
        }

        // Answer choices within this fieldset
        const rows = fs.querySelectorAll('tr.d2l-rowshadeonhover');
        const choices = Array.from(rows).map(row => {
            const block = row.querySelector('td.d_tb d2l-html-block, td.d_tw d2l-html-block');
            if (block) return decode(block.getAttribute('html') || block.innerHTML);
            const td = row.querySelector('td.d_tb, td.d_tw');
            return td ? td.innerText.trim() : '';
        }).filter(Boolean);

        result.push({ index: qIndex, stem, choices });
    });

    return result;
}"""

# Click answer `optIndex` within question `qIndex` (qIndex = fieldset order)
JS_CLICK_ANSWER = """({qIndex, optIndex}) => {
    const fieldsets = document.querySelectorAll('fieldset.dfs_m');
    const fs = fieldsets[qIndex];
    if (!fs) return false;
    const rows = fs.querySelectorAll('tr.d2l-rowshadeonhover');
    if (rows[optIndex]) { rows[optIndex].click(); return true; }
    const radios = fs.querySelectorAll('input.d2l-radio');
    if (radios[optIndex]) { radios[optIndex].click(); return true; }
    return false;
}"""

# Is question qIndex answered?
JS_IS_ANSWERED = """(qIndex) => {
    const fieldsets = document.querySelectorAll('fieldset.dfs_m');
    const fs = fieldsets[qIndex];
    if (!fs) return false;
    return fs.querySelector('tr.d2l-rowshadeonhover-selected') !== null
        || fs.querySelector('input.d2l-radio:checked') !== null;
}"""

# Count question blocks
JS_COUNT = "() => document.querySelectorAll('fieldset.dfs_m').length"

# Stem image check for a given question index
JS_STEM_HAS_IMAGE = """(qIndex) => {
    const fieldsets = document.querySelectorAll('fieldset.dfs_m');
    const fs = fieldsets[qIndex];
    if (!fs) return false;
    let wrapper = fs.parentElement;
    while (wrapper && wrapper !== document.body) {
        const blocks = wrapper.querySelectorAll('d2l-html-block');
        for (const b of blocks) {
            if (!b.closest('tr.d2l-rowshadeonhover') && !fs.contains(b)) {
                const html = b.getAttribute('html') || b.innerHTML || '';
                return html.toLowerCase().includes('<img');
            }
        }
        wrapper = wrapper.parentElement;
    }
    return false;
}"""


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def ask_claude(client, question_text, choices, screenshot_b64=None, subject=""):
    lettered = "\n".join(f"{chr(65 + i)}) {c}" for i, c in enumerate(choices))
    subject_line = f"This is a {subject} question.\n\n" if subject else ""
    text_prompt = (
        f"You are an expert {subject} professor. "
        f"Answer the following multiple-choice question carefully and accurately. "
        f"Think through each option before deciding. "
        f"Reply with ONLY the letter of the best answer (A, B, C, …).\n\n"
        f"{subject_line}"
        f"Question:\n{question_text}\n\n"
        f"Choices:\n{lettered}"
    )
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
    letter = re.sub(r'[^A-Z]', '', msg.content[0].text.strip().upper())
    return letter[0] if letter else "A"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def countdown(label, seconds):
    for remaining in range(seconds, 0, -1):
        mins, secs = divmod(remaining, 60)
        print(f"  {label}: {mins:02d}:{secs:02d} remaining...  ", end="\r", flush=True)
        time.sleep(1)
    print(f"  {label}: done!                    ")


def wait_for_page(frame):
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=10_000)
    except PWTimeoutError:
        pass
    time.sleep(1.5)


def find_quiz_page(context, url):
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
    frames = page.frames
    print(f"[+] Frames detected: {len(frames)}")
    for frame in reversed(frames):
        if frame == page.main_frame:
            continue
        try:
            count = frame.evaluate(
                "() => document.querySelectorAll('fieldset.dfs_m, tr.d2l-rowshadeonhover').length"
            )
            if count > 0:
                print(f"[+] Quiz frame found ({count} elements): {frame.url[:80]}")
                return frame
        except Exception:
            continue
    print("[*] No quiz iframe found – using main page frame.")
    return page.main_frame


def screenshot_question(frame, qIndex):
    """Screenshot the fieldset for question qIndex."""
    try:
        fieldsets = frame.query_selector_all('fieldset.dfs_m')
        if qIndex < len(fieldsets):
            png = fieldsets[qIndex].screenshot()
            return base64.b64encode(png).decode()
    except Exception as e:
        print(f"  [!] Screenshot failed: {e}")
    return None


def submit_quiz(frame):
    for sel in ['button:has-text("Submit Quiz")', 'a:has-text("Submit Quiz")']:
        for btn in frame.query_selector_all(sel):
            if btn.is_visible() and btn.is_enabled():
                btn.click()
                try:
                    frame.wait_for_selector(
                        'button:has-text("Yes"), .d2l-dialog-footer button', timeout=5_000)
                    for confirm in frame.query_selector_all(
                        'button:has-text("Yes"), .d2l-dialog-footer button'):
                        if confirm.is_visible():
                            confirm.click()
                            break
                except PWTimeoutError:
                    pass
                wait_for_page(frame)
                print("[+] Quiz submitted.")
                return True
    print("[!] Submit Quiz button not found.")
    return False


def dump_page(frame):
    path = os.path.expanduser("~/Downloads/quiz_debug.html")
    try:
        html = frame.content()
    except Exception:
        html = "<error>"
    with open(path, "w") as f:
        f.write(html)
    print(f"[debug] Saved to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_quiz(url, debug=False, subject="Intro to Economics", auto_submit=False):
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

        questions = frame.evaluate(JS_GET_ALL_QUESTIONS)
        total = len(questions)
        print(f"[+] Subject: {subject}")
        print(f"[+] Found {total} questions on this page.\n")

        if total == 0:
            print("[!] No questions found. If this quiz uses pages, use quiz_bot.py instead.")
            return

        for q in questions:
            qi = q["index"]
            stem = q["stem"]
            choices = q["choices"]

            print(f"[Q{qi + 1}/{total}]")
            if not choices:
                print("  [!] No answer choices found – skipping.")
                continue

            print(f"  Question : {stem[:150]}")
            for i, c in enumerate(choices):
                print(f"    {chr(65+i)}) {c}")

            has_img = frame.evaluate(JS_STEM_HAS_IMAGE, qi)
            screenshot_b64 = screenshot_question(frame, qi)
            if has_img:
                print("  [+] Image detected – screenshot sent to Claude.")

            delay = random.randint(45, 90)
            countdown("Reading", delay)

            letter = ask_claude(client, stem, choices, screenshot_b64, subject)
            ans_text = choices[ord(letter)-65] if ord(letter)-65 < len(choices) else "?"
            print(f"  Answer   : {letter}) {ans_text}")

            frame.evaluate(JS_CLICK_ANSWER, {"qIndex": qi, "optIndex": ord(letter)-65})
            time.sleep(0.8)
            if not frame.evaluate(JS_IS_ANSWERED, qi):
                print("  [!] Selection didn't register – retrying.")
                frame.evaluate(JS_CLICK_ANSWER, {"qIndex": qi, "optIndex": ord(letter)-65})

            countdown("Reviewing", random.randint(3, 8))
            print()

        print(f"[+] All {total} questions answered.")

        if auto_submit:
            submit_quiz(frame)
        else:
            print("[*] Auto-submit OFF. Review your answers, then submit manually.")
            print("    (Run with --submit to auto-submit next time.)")

        print("[+] Done.")


def main():
    parser = argparse.ArgumentParser(description="D2L single-page quiz bot.")
    parser.add_argument("--url", required=True, help="Full D2L quiz URL.")
    parser.add_argument("--subject", default="Intro to Economics", help="Subject context for Claude.")
    parser.add_argument("--submit", action="store_true", help="Auto-submit the quiz when finished.")
    parser.add_argument("--debug", action="store_true", help="Dump page HTML and exit.")
    args = parser.parse_args()
    run_quiz(args.url, debug=args.debug, subject=args.subject, auto_submit=args.submit)


if __name__ == "__main__":
    main()
