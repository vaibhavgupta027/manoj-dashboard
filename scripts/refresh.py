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
DAYS_BACK_DIRECTIVE = 90
DAYS_BACK_REPLY = 90
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
    return body  # full body for directives; caller truncates replies


def fetch_emails_from_manoj():
    from datetime import timedelta

    user = os.environ.get("GMAIL_USER", "")
    pwd = os.environ.get("GMAIL_PASS", "")
    if not user or not pwd:
        print("ERROR: GMAIL_USER and GMAIL_PASS env vars required", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to IMAP as {user}…")
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(user, pwd)

    since90 = (datetime.now() - timedelta(days=DAYS_BACK_DIRECTIVE)).strftime("%d-%b-%Y")
    since30 = (datetime.now() - timedelta(days=DAYS_BACK_REPLY)).strftime("%d-%b-%Y")

    emails = []
    seen_ids = set()

    # ── Pass 1: Manoj's directives — search All Mail (includes archived) ──
    mail.select('"[Gmail]/All Mail"')
    _, data = mail.search(None, f'(FROM "{FROM_FILTER}" SINCE "{since90}")')
    ids = data[0].split()
    print(f"[Pass 1] Found {len(ids)} directives from {FROM_FILTER} in All Mail (last 90 days)")

    for eid in ids[-100:]:  # most recent 100
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        _, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        emails.append({
            "type": "DIRECTIVE",
            "subject": decode_str(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "cc": msg.get("Cc", ""),
            "body": get_body(msg),
        })

    # ── Pass 2: Team replies where Manoj is CC'd ──
    _, data = mail.search(None, f'(CC "{FROM_FILTER}" SINCE "{since30}")')
    ids = data[0].split()
    print(f"[Pass 2] Found {len(ids)} replies with Manoj CC'd in All Mail (last 30 days)")

    for eid in ids:
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        _, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        from_addr = msg.get("From", "").lower()
        if FROM_FILTER in from_addr:
            continue
        emails.append({
            "type": "REPLY",
            "subject": decode_str(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "cc": msg.get("Cc", ""),
            "body": get_body(msg),  # full body
        })
        if len(emails) >= 180:
            break

    # ── Pass 3: Direct replies TO manoj not caught by CC pass ──
    _, data = mail.search(None, f'(TO "{FROM_FILTER}" SINCE "{since30}")')
    ids = data[0].split()
    print(f"[Pass 3] Found {len(ids)} replies with Manoj in TO field (last 30 days)")

    for eid in ids:
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        _, msg_data = mail.fetch(eid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        from_addr = msg.get("From", "").lower()
        if FROM_FILTER in from_addr:
            continue
        emails.append({
            "type": "REPLY",
            "subject": decode_str(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "cc": msg.get("Cc", ""),
            "body": get_body(msg),  # full body
        })
        if len(emails) >= 220:
            break

    mail.logout()
    from email.utils import parsedate_to_datetime
    def _parse_date(d):
        try:
            return parsedate_to_datetime(d).astimezone(timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    emails.sort(key=lambda e: _parse_date(e["date"]))
    print(f"Total: {sum(1 for e in emails if e['type']=='DIRECTIVE')} directives + {sum(1 for e in emails if e['type']=='REPLY')} replies")
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

    def trim_body(body, max_chars):
        """Strip quoted reply chains (lines starting with >) and trim to max_chars."""
        lines = (body or '').splitlines()
        clean = [l for l in lines if not l.strip().startswith('>')]
        return '\n'.join(clean).strip()[:max_chars]

    emails_text = "\n\n---\n\n".join(
        f"[{e['type']}] {e['date']} | FROM: {e['from']} | TO: {e.get('to','')} | CC: {e.get('cc','')} | SUBJECT: {e['subject']}\n\n"
        + trim_body(e['body'], 3000 if e['type'] == 'REPLY' else 8000)
        for e in emails
    )[:100000]

    PROMPT_TEMPLATE = (
        'You are a chief of staff. Analyse ALL emails from Manoj Tulsani (CEO, manoj@raynatours.com) to his team. '
        '[DIRECTIVE] emails are his original briefs. [REPLY] emails are team responses — use them to determine status and reply dates.\n\n'
        'RULES:\n'
        '- Extract EVERY distinct action directive — do NOT merge separate topics into one card\n'
        '- One email with "Two New Partnership Tracks" = 2 separate items. "Two India Items" = 2 items.\n'
        '- [REPLY] present for a thread = at minimum "IN PROGRESS" (yellow). No reply = "NO RESPONSE" (red).\n'
        '- Extract ALL distinct action directives — do NOT cap the count. Coverage is more important than brevity.\n'
        '- Include subTasks (3-6 bullet points) and sheetLinks (empty [] if none) on every item.\n'
        '- For each item include "relatedSubjects": array of email subject strings this directive came from.\n'
        '- For each item include "lastReplyDate": "Mon DD" of the most recent [REPLY] on that thread, or null.\n'
        '- For the "people" array: list EVERY person assigned ANY task, with ALL their tasks, assigned date, and exact deadlines.\n\n'
        'Return ONLY valid JSON - no prose, no markdown fences:\n'
        '{\n'
        '  "stats": { "total": N, "onTrack": N, "inProgress": N, "notedOnly": N, "noResponse": N },\n'
        '  "sections": [{ "id": "slug", "title": "emoji + section name", "items": [{\n'
        '    "id": "kebab-slug",\n'
        '    "title": "specific directive title",\n'
        '    "statusLabel": "AT RISK|IN PROGRESS|ON TRACK|NO RESPONSE|ONLY NOTED|ACKNOWLEDGED|COMPLETED",\n'
        '    "color": "red|yellow|green|orange",\n'
        '    "deadline": "string or null",\n'
        '    "deadlineUrgency": "urgent|warning|ok|null",\n'
        '    "emailDate": "Mon DD",\n'
        '    "lastReplyDate": "Mon DD or null",\n'
        '    "owners": ["First name"],\n'
        '    "progress": 0,\n'
        '    "progressLabel": "one line on current state",\n'
        '    "response": "summary of team reply or null",\n'
        '    "noReplyWarning": "warning if no reply, else null",\n'
        '    "subTasks": ["action 1", "action 2"],\n'
        '    "sheetLinks": [],\n'
        '    "relatedSubjects": ["exact email subject line"]\n'
        '  }]}],\n'
        '  "people": [{ "name": "First name", "tasks": [{\n'
        '    "task": "specific task description",\n'
        '    "assignedDate": "Mon DD",\n'
        '    "deadline": "Jun DD or ASAP or TBD",\n'
        '    "deadlineUrgency": "urgent|warning|ok|null",\n'
        '    "color": "red|yellow|green|orange"\n'
        '  }]}]\n'
        '}\n\n'
        'Color: red=no response/at risk, orange=noted only, yellow=in progress, green=on track/done.\n'
        'Section IDs (use exactly): critical, tech-product, marketing-partnerships, pr-brand-content, operations-pricing, acknowledged-closed.\n'
        'People: include Vaibhav, Asad, Aparna, Manish, Gaurav, Deepak, Malik, Anket, Azhar, Rishi, Nihad, Pari, Rajkumar, Ranjan, Alok, Senthil, Anwar and any others with tasks.\n\n'
        'Emails:\n{emails_text}'
    )
    prompt = PROMPT_TEMPLATE.replace('{emails_text}', emails_text)

    print("Sending to Claude for analysis…")
    response_text = ""
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=24000,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            response_text += text
    print(f"Claude response: {len(response_text)} chars")

    # Dump raw response for debugging
    with open("/tmp/claude_response.txt", "w") as f:
        f.write(response_text)

    match = re.search(r'\{[\s\S]*\}', response_text)
    if not match:
        print("ERROR: No JSON found in Claude response", file=sys.stderr)
        print(response_text[:500])
        sys.exit(1)

    raw = match.group()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}", file=sys.stderr)
        # Try to find and report the problem area
        lines = raw.split('\n')
        err_line = e.lineno - 1
        context = '\n'.join(lines[max(0, err_line-2):err_line+3])
        print(f"Context around error:\n{context}", file=sys.stderr)
        print("Full response saved to /tmp/claude_response.txt", file=sys.stderr)
        raise


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

    emails_for_store = [
        {
            "type": e["type"],
            "subject": e["subject"],
            "date": e["date"],
            "from": e["from"],
            "to": e.get("to", ""),
            "cc": e.get("cc", ""),
            "body": e["body"],  # full body stored
        }
        for e in emails
    ]

    output = {
        "meta": {
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "source": f"Gmail · {sum(1 for e in emails if e.get('type')=='DIRECTIVE')} directives + {sum(1 for e in emails if e.get('type')=='REPLY')} replies",
            "googleSheetUrl": existing_sheet_url
        },
        "stats": analyzed.get("stats", {}),
        "sections": analyzed.get("sections", []),
        "people": analyzed.get("people", []),
        "emails": emails_for_store,
    }

    with open(data_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"✅ data.json updated — {len(emails)} emails processed, {output['stats'].get('total', '?')} directives found.")


if __name__ == "__main__":
    main()
