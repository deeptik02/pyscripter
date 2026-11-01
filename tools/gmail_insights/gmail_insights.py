#!/usr/bin/env python3
"""Daily insights from a Gmail label (folder).

Reads recent emails from one Gmail label and asks Claude for ONE important
insight to keep in mind today for each of five focus areas:

  1. Product management
  2. AI in the world of products
  3. Business insight as a consultant (MBA lens)
  4. Personal development / growth
  5. Product manager interview prep

Output is written as Markdown (and printed); optionally it is emailed back to you.

Usage:
    python gmail_insights.py --label "Newsletters" --days 7
    python gmail_insights.py --label "Newsletters" --email-me
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import sys
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Secrets and personal data live OUTSIDE the repo so they can never be committed.
DATA_DIR = Path(os.environ.get("GMAIL_INSIGHTS_HOME", Path.home() / ".config" / "gmail_insights"))
CREDENTIALS_FILE = DATA_DIR / "credentials.json"  # OAuth client from Google Cloud Console
HISTORY_FILE = DATA_DIR / "insights_history.json"
OUTPUT_DIR = DATA_DIR / "output"

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

MODEL = "claude-opus-5-5"
MAX_CHARS_PER_EMAIL = 20_000  # long newsletters are clipped per email; reported in output
HISTORY_DAYS = 14  # previous insights shown to Claude so it doesn't repeat itself

CATEGORIES = {
    "product_management": "Product Management",
    "ai_in_products": "AI in the World of Products",
    "business_consulting": "Business Insight as a Consultant (MBA)",
    "personal_growth": "Personal Development & Growth",
    "pm_interview_prep": "PM Interview Prep",
}

INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "A crisp one-line insight."},
        "insight": {"type": "string", "description": "2-4 sentences explaining the insight."},
        "why_it_matters": {"type": "string", "description": "Why this matters to the reader today."},
        "action_today": {"type": "string", "description": "One concrete thing to do or practice today."},
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Subjects of the emails this insight draws on (empty if general knowledge).",
        },
    },
    "required": ["title", "insight", "why_it_matters", "action_today", "sources"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {key: INSIGHT_SCHEMA for key in CATEGORIES},
    "required": list(CATEGORIES),
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are a personal coach for an aspiring/practising product manager with an MBA \
consulting background. Each morning you read the emails they saved to a Gmail folder \
and distil exactly ONE important insight per focus area that they should keep in mind \
today.

Focus areas:
- product_management: product management craft (discovery, prioritisation, metrics, execution, stakeholders).
- ai_in_products: how AI is changing products, product strategy, and the PM role.
- business_consulting: a business/strategy insight seen through an MBA consultant's lens \
(frameworks, market dynamics, unit economics, competitive moves).
- personal_growth: personal development, habits, communication, leadership, wellbeing.
- pm_interview_prep: something that directly improves PM interview performance \
(product sense, execution/metrics, estimation, strategy, behavioural/STAR answers). \
Where useful, include a practice question in action_today.

Rules:
- Ground insights in the emails whenever they contain relevant material, and list the \
subjects you used in `sources`. If the emails have nothing relevant for an area, draw on \
well-established knowledge and leave `sources` empty.
- Prefer the most important, non-obvious, actionable insight over a summary of everything.
- Do not repeat insights from the recent history provided; find a fresh angle.
- Be specific and concise. No filler.
"""


# --------------------------------------------------------------------------- Gmail

def gmail_service(scopes: list[str]):
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = DATA_DIR / ("token_send.json" if SEND_SCOPE in scopes else "token.json")
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                sys.exit(
                    f"Missing {CREDENTIALS_FILE}. Create an OAuth 'Desktop app' client in "
                    "Google Cloud Console (Gmail API enabled) and save its JSON there. See README.md."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), scopes)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())
        token_file.chmod(0o600)  # the token grants Gmail access: owner-only
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def find_label_id(service, label_name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == label_name.lower():
            return label["id"]
    available = ", ".join(sorted(l["name"] for l in labels))
    sys.exit(f"Label '{label_name}' not found. Available labels: {available}")


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _extract_body(payload: dict) -> str:
    """Prefer text/plain; fall back to text/html converted to text."""
    plain, html_parts = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html_parts.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    return _html_to_text("\n".join(html_parts))


def fetch_emails(service, label_id: str, days: int, max_emails: int) -> list[dict]:
    query = f"newer_than:{days}d"
    ids, page_token = [], None
    while len(ids) < max_emails:
        resp = service.users().messages().list(
            userId="me", labelIds=[label_id], q=query,
            maxResults=min(100, max_emails - len(ids)), pageToken=page_token,
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    emails = []
    for msg_id in ids:
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        body = _extract_body(msg["payload"]) or msg.get("snippet", "")
        clipped = len(body) > MAX_CHARS_PER_EMAIL
        emails.append({
            "subject": headers.get("subject", "(no subject)"),
            "from": headers.get("from", ""),
            "date": headers.get("date", ""),
            "body": body[:MAX_CHARS_PER_EMAIL],
            "clipped": clipped,
        })
    return emails


def send_to_self(service, subject: str, markdown: str) -> None:
    profile = service.users().getProfile(userId="me").execute()
    message = MIMEText(markdown, "plain", "utf-8")
    message["to"] = profile["emailAddress"]
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# --------------------------------------------------------------------------- History

def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    return [h for h in json.loads(HISTORY_FILE.read_text()) if h["date"] >= cutoff]


def save_history(history: list[dict], today: str, insights: dict) -> None:
    history = [h for h in history if h["date"] != today]
    history.append({"date": today, "titles": {k: v["title"] for k, v in insights.items()}})
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


# --------------------------------------------------------------------------- Claude

def generate_insights(emails: list[dict], history: list[dict], today: str) -> dict:
    email_blocks = "\n\n".join(
        f'<email index="{i}">\nSubject: {e["subject"]}\nFrom: {e["from"]}\nDate: {e["date"]}\n\n{e["body"]}\n</email>'
        for i, e in enumerate(emails, 1)
    ) or "(No emails in this folder for the selected period.)"
    history_text = json.dumps(history, indent=2) if history else "(none)"

    user_prompt = (
        f"Today is {today}.\n\n"
        f"<recent_insight_history>\n{history_text}\n</recent_insight_history>\n\n"
        f"<emails>\n{email_blocks}\n</emails>\n\n"
        "Give me today's one insight for each of the five focus areas."
    )

    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        betas=["server-side-fallback-2026-07-01"],
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        fallbacks="default",
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        sys.exit(f"Claude declined the request: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        sys.exit("Response hit max_tokens before finishing; try fewer emails (--max-emails).")

    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# --------------------------------------------------------------------------- Output

def to_markdown(insights: dict, today: str, label: str, emails: list[dict]) -> str:
    lines = [f"# Daily Insights — {today}", "", f"_From {len(emails)} email(s) in Gmail label **{label}**._", ""]
    for key, heading in CATEGORIES.items():
        item = insights[key]
        lines += [
            f"## {heading}",
            f"**{item['title']}**",
            "",
            item["insight"],
            "",
            f"- **Why it matters:** {item['why_it_matters']}",
            f"- **Do today:** {item['action_today']}",
        ]
        if item["sources"]:
            lines.append(f"- **Sources:** {'; '.join(item['sources'])}")
        lines.append("")
    clipped = [e["subject"] for e in emails if e["clipped"]]
    if clipped:
        lines += [f"_Note: {len(clipped)} long email(s) were clipped to {MAX_CHARS_PER_EMAIL:,} characters._", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="Gmail label/folder name, e.g. 'Newsletters' or 'PM/Reading'")
    parser.add_argument("--days", type=int, default=7, help="Look back this many days (default 7)")
    parser.add_argument("--max-emails", type=int, default=50, help="Max emails to read (default 50)")
    parser.add_argument("--email-me", action="store_true", help="Also email the insights to your own inbox")
    args = parser.parse_args()

    scopes = [READ_SCOPE] + ([SEND_SCOPE] if args.email_me else [])
    service = gmail_service(scopes)
    label_id = find_label_id(service, args.label)
    emails = fetch_emails(service, label_id, args.days, args.max_emails)
    print(f"Read {len(emails)} email(s) from '{args.label}'. Generating insights...", file=sys.stderr)

    today = dt.date.today().isoformat()
    history = load_history()
    insights = generate_insights(emails, history, today)
    save_history(history, today, insights)

    markdown = to_markdown(insights, today, args.label, emails)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / f"insights_{today}.md"
    out_file.write_text(markdown)
    print(markdown)
    print(f"\nSaved to {out_file}", file=sys.stderr)

    if args.email_me:
        send_to_self(service, f"Your 5 daily insights — {today}", markdown)
        print("Emailed to your inbox.", file=sys.stderr)


if __name__ == "__main__":
    main()
