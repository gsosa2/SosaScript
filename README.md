# SosaScript – D2L Quiz Bot

Automates D2L (Brightspace) quizzes using your **existing Zen Browser session** — no login needed. Uses Claude AI to read and answer each question, with a random 45–90 second pause before each answer.

---

## Mac Setup (one-time)

You do **not** need IntelliJ. Just a Terminal and Python.

### 1. Check Python is installed
```bash
python3 --version
```
If it prints a version (3.9+), you're good. If not, download it from [python.org](https://www.python.org/downloads/).

### 2. Download this project
```bash
cd ~/Desktop
git clone https://github.com/gsosa2/sosascript.git
cd sosascript
```

### 3. Install dependencies
```bash
pip3 install -r requirements.txt
playwright install firefox
```

### 4. Set your Anthropic API key
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
Get a key at [console.anthropic.com](https://console.anthropic.com).

> **Tip:** Add the export line to `~/.zshrc` so you don't have to re-enter it each time.

---

## Running the bot

1. **Open Zen Browser** and navigate to your D2L quiz tab (log in manually as usual)
2. **Close all other Zen windows** — the script attaches to your profile, so fewer open windows = less confusion
3. In Terminal:

```bash
cd ~/Desktop/sosascript
python3 quiz_bot.py --url "https://yourschool.brightspace.com/d2l/lms/quizzing/..."
```

The script will:
- Launch a new Zen window attached to your existing session (you'll already be logged in)
- Find the quiz tab or open the URL
- For each question: wait 45–90 seconds → ask Claude → select the answer → click Next
- Submit automatically on the last question

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Zen Browser not found` | Set `export ZEN_PATH="/Applications/Zen Browser.app/Contents/MacOS/zen"` |
| `No Zen profile found` | Set `export ZEN_PROFILE="$HOME/Library/Application Support/zen/Profiles/your-profile-dir"` |
| Answers not being selected | Run with `--url` and watch — D2L selector may differ; open an issue with a screenshot |
