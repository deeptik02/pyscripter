# Gmail Daily Insights

Reads the emails in one Gmail label (folder) and uses Claude to give you **one important
insight to keep in mind today** for each of:

1. Product Management
2. AI in the World of Products
3. Business Insight as a Consultant (MBA)
4. Personal Development & Growth
5. PM Interview Prep

Each insight has a title, a short explanation, why it matters, one action for today, and
the email subjects it came from. The tool remembers the last 14 days of insights so it
doesn't repeat itself.

## Setup (one time)

1. **Install dependencies** (Python 3.10+):
   ```bash
   cd tools/gmail_insights
   pip install -r requirements.txt
   ```
2. **Anthropic API key:** `export ANTHROPIC_API_KEY=sk-ant-...`
3. **Gmail API access:**
   - Go to <https://console.cloud.google.com/>, create (or pick) a project.
   - Enable the **Gmail API** (APIs & Services → Library).
   - Configure the **OAuth consent screen** (External, add yourself as a test user).
   - Create **Credentials → OAuth client ID → Desktop app**, download the JSON and save it
     as `~/.config/gmail_insights/credentials.json` (outside this repo — see Security below).
4. **Create the Gmail label** you want to use (e.g. `Insights`) and add a filter so your
   newsletters (Lenny's Newsletter, Stratechery, HBR, etc.) are labelled automatically.

The first run opens a browser so you can approve read-only access. The token is saved
to `~/.config/gmail_insights/token.json` (owner-only permissions) and reused afterwards.

## Run

```bash
python gmail_insights.py --label "Insights"              # last 7 days
python gmail_insights.py --label "Insights" --days 3     # shorter window
python gmail_insights.py --label "Insights" --email-me   # also email the result to yourself
```

Nested labels use a slash: `--label "PM/Reading"`.

Results print to the terminal and are saved to `~/.config/gmail_insights/output/insights_YYYY-MM-DD.md`.
`--email-me` asks for an extra Gmail *send* permission the first time it is used.

## Run it every morning

**macOS / Linux (cron)**: `crontab -e`, then add (7:45am daily):
```
45 7 * * * cd /path/to/tools/gmail_insights && ANTHROPIC_API_KEY=sk-ant-... /usr/bin/python3 gmail_insights.py --label "Insights" --email-me >> run.log 2>&1
```
Run the script once by hand first so the Gmail token exists.

**Windows**: Task Scheduler → Create Basic Task → Daily → Start a program:
`python.exe` with arguments `gmail_insights.py --label "Insights" --email-me` and
"Start in" set to this folder.

## Notes

- Access is read-only unless you use `--email-me`.
- Very long emails are clipped to 20,000 characters each (noted in the output); use
  `--max-emails` to cap how many emails are read (default 50).
- Uses the `claude-opus-5-5` model; change `MODEL` in `gmail_insights.py` to use another.

## Security

This code is safe to keep in a **public** repo: it contains no keys, passwords or tokens.
Your secrets only exist on your own computer, and the tool keeps them **outside the repo**
in `~/.config/gmail_insights/` (override with `GMAIL_INSIGHTS_HOME`), so they can't be
committed by accident. Never commit or share:

- `credentials.json` (your Google OAuth client)
- `token.json` / `token_send.json` — **anyone with this file can read your Gmail**
- your `ANTHROPIC_API_KEY`

If one ever leaks: revoke the app at <https://myaccount.google.com/permissions>, delete the
OAuth client in Google Cloud Console, and rotate the API key at
<https://console.anthropic.com/settings/keys>. Also turn on 2-Step Verification for your
Google account.
