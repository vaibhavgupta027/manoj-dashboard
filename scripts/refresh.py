#!/usr/bin/env python3
"""
Daily refresh script — fetches emails from manoj@raynatours.com via IMAP,
sends them to Claude for analysis, and writes updated data.json.

Run locally:   python3 scripts/refresh.py
Run via CI:    same command, credentials come from env vars
"""

import imaplib
import email
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.header import decode_header

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASS = os.environ.get("GMAIL_PASS", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FROM_FILTER = "manoj@raynatours.com"
DAYS_BACK = 60
DATA_JSON = os.path.join(os.path.dirname(__file__), "..", "data.json")


# ---------------------------------------------------------------------------
# Load .env if running locally
# ---------------------------------------------------------------------------

def load_env():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            pass
    return body[:4000]  # Truncate to avoid token overload


def fetch_emails_from_manoj():
    user = os.environ.get("GMAIL_USER", "")
    pwd = os.environ.get("GMAIL_PASS", "")
    if not user or not pwd:
        print("ERROR: GMAIL_USER and GMAIL_PASS env vars required", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to IMAP as {user}…")
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(user, pwd)
    mail.select("inbox")

    from_date = (datetime.now() - __import__("timedelta", fromlist=["timedelta"]).__class__(days=DAYS_BACK)).strftime("%d-%b-%Y")

    # Use timedelta properly
    from datetime import timedelta
    since_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%d-%b-%Y")

    _, data = mail.search(None, f'(FROM "{FROM_FILTER}" SINCE "{since_date}")')
    ids = data[0].split()
    print(f"Found {len(ids)} emails from {FROM_FILTER} in last {DAYS_BACK} days.")

    emails = []
    for eid in ids[-80:]:  # Cap at 80 most recent
        _, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        subject = decode_str(msg.get("Subject", ""))
        date_str = msg.get("Date", "")
        body = get_body(msg)
        emails.append({"subject": subject, "date": date_str, "body": body})

    mail.logout()
    return emails


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

def analyze_with_claude(emails):
    try:
        import anthropic
    except ImportError:
        print("anthropic package not installed. Run: pip install anthropic", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY env var required", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    emails_text = "\n\n---\n\n".join(
        f"DATE: {e['date']}\nSUBJECT: {e['subject']}\n\n{e['body']}"
        for e in emails
    )

    prompt = f"""You are analysing emails sent by Manoj Tulsani (CEO of Rayna Tours, manoj@raynatours.com) to his team.

Extract all ACTION DIRECTIVES and TASKS from these emails. For each directive, identify:
- The specific task or directive
- Who is assigned (owners)
- Deadline if mentioned
- Evidence of response from the team (any replies visible in the email thread)

Return a JSON object with this exact structure:
{{
  "stats": {{
    "total": <number>,
    "onTrack": <number>,
    "inProgress": <number>,
    "notedOnly": <number>,
    "noResponse": <number>
  }},
  "sections": [
    {{
      "id": "<slug>",
      "title": "<emoji + section name>",
      "items": [
        {{
          "id": "<kebab-slug>",
          "title": "<directive title>",
          "statusLabel": "<AT RISK|IN PROGRESS|ON TRACK|NO RESPONSE|ONLY NOTED|ACKNOWLEDGED|COMPLETED>",
          "color": "<red|yellow|green|orange>",
          "deadline": "<deadline string or null>",
          "deadlineUrgency": "<urgent|warning|ok|null>",
          "emailDate": "<date like 'Jun 7'>",
          "owners": ["<name>"],
          "progress": <0-100>,
          "progressLabel": "<short progress note>",
          "response": "<summary of team reply or null>",
          "noReplyWarning": "<warning string if no response, else null>"
        }}
      ]
    }}
  ]
}}

Color rules:
- red: no response, at risk, missed deadline
- orange: only noted/acknowledged with no action
- yellow: in progress, partial response
- green: completed, on track, strong response

Group into sections: Critical (urgent deadlines), Tech & Product, Marketing & Partnerships, PR/Brand/Content, Operations & Pricing, Acknowledged/Closed.

Emails:
{emails_text[:30000]}"""

    print("Sending to Claude for analysis…")
    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text
    # Extract JSON from response
    match = re.search(r'\{[\s\S]*\}', response_text)
    if not match:
        print("ERROR: No JSON found in Claude response", file=sys.stderr)
        print(response_text[:500])
        sys.exit(1)

    return json.loads(match.group())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_env()

    # Reload env vars after loading .env
    global GMAIL_USER, GMAIL_PASS, ANTHROPIC_API_KEY
    GMAIL_USER = os.environ.get("GMAIL_USER", "")
    GMAIL_PASS = os.environ.get("GMAIL_PASS", "")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    emails = fetch_emails_from_manoj()

    if not emails:
        print("No emails found. Keeping existing data.json unchanged.")
        sys.exit(0)

    analyzed = analyze_with_claude(emails)

    # Read existing data.json to preserve googleSheetUrl
    existing_sheet_url = ""
    data_path = os.path.normpath(DATA_JSON)
    if os.path.exists(data_path):
        try:
            with open(data_path) as f:
                existing = json.load(f)
            existing_sheet_url = existing.get("meta", {}).get("googleSheetUrl", "")
        except Exception:
            pass

    output = {
        "meta": {
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "source": f"Gmail · from:{FROM_FILTER}",
            "googleSheetUrl": existing_sheet_url
        },
        "stats": analyzed.get("stats", {}),
        "sections": analyzed.get("sections", [])
    }

    with open(data_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"✅ data.json updated — {len(emails)} emails processed, {output['stats'].get('total', '?')} directives found.")


if __name__ == "__main__":
    main()
