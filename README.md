# SosaScript – D2L Quiz Bot

Automates D2L (Brightspace) quizzes using Playwright for browser control and Claude for answering questions.

## Setup

```bash
pip install -r requirements.txt
playwright install firefox
```

> **Zen Browser users:** Zen is Firefox-based, so set `ZEN_PATH` to point at your Zen binary and the bot will launch Zen instead of Playwright's bundled Firefox:
> ```bash
> export ZEN_PATH="/usr/lib/zen-browser/zen"   # Linux example
> export ZEN_PATH="/Applications/Zen Browser.app/Contents/MacOS/zen"  # macOS example
> ```

Set your keys:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export D2L_USERNAME="you@school.edu"
export D2L_PASSWORD="yourpassword"
```

## Run

```bash
python quiz_bot.py --url "https://your.school.d2l.com/d2l/lms/quizzing/..."
```

### Options

| Flag | Description |
|------|-------------|
| `--url` | Full URL to the D2L quiz page (required) |
| `--username` | D2L login email (or set `D2L_USERNAME`) |
| `--password` | D2L password (or set `D2L_PASSWORD`) |
| `--headed` | Show the browser window (useful for debugging) |

## How it works

1. Opens the quiz URL in a Chromium browser
2. Logs in automatically if a login form is present
3. For each question:
   - Reads the question stem and all answer choices
   - Waits a **random 45–90 seconds** before answering
   - Sends the question to Claude, which picks the best answer
   - Selects that answer and clicks **Next**
4. Submits the quiz when the last question is reached
