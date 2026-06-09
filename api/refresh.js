// Vercel serverless function — triggered by Vercel Cron twice daily (03:00 + 15:00 UTC)
// Flow: Gmail IMAP (All Mail) → Claude analysis → commit data.json to GitHub → Vercel auto-deploys

const { ImapFlow } = require('imapflow');
const { simpleParser } = require('mailparser');
const Anthropic = require('@anthropic-ai/sdk');
const https = require('https');

const GITHUB_REPO   = 'vaibhavgupta027/manoj-dashboard';
const GITHUB_FILE   = 'data.json';
const GITHUB_BRANCH = 'main';
const MANOJ         = 'manoj@raynatours.com';

// ── GitHub API helpers ──────────────────────────────────────────────────────

function githubRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = https.request({
      hostname: 'api.github.com',
      path,
      method,
      headers: {
        Authorization: `Bearer ${process.env.GH_PAT_FOR_REFRESH}`,
        'User-Agent': 'manoj-dashboard-refresh',
        'Content-Type': 'application/json',
        Accept: 'application/vnd.github+json',
        ...(data ? { 'Content-Length': Buffer.byteLength(data) } : {}),
      },
    }, (res) => {
      let buf = '';
      res.on('data', c => (buf += c));
      res.on('end', () => resolve(JSON.parse(buf)));
    });
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

async function commitDataJson(content) {
  const current = await githubRequest('GET',
    `/repos/${GITHUB_REPO}/contents/${GITHUB_FILE}?ref=${GITHUB_BRANCH}`
  );
  const encoded = Buffer.from(content, 'utf8').toString('base64');
  const result = await githubRequest('PUT',
    `/repos/${GITHUB_REPO}/contents/${GITHUB_FILE}`,
    {
      message: `chore: refresh data.json ${new Date().toISOString()}`,
      content: encoded,
      sha: current.sha,
      branch: GITHUB_BRANCH,
    }
  );
  return result?.commit?.sha || 'unknown';
}

// ── Gmail IMAP ──────────────────────────────────────────────────────────────
// Three passes on [Gmail]/All Mail — catches everything regardless of archive status:
//   1. FROM manoj  → his original directives (90 days, up to 100)
//   2. CC manoj    → team replies where manoj is CC'd (30 days)
//   3. TO manoj    → direct replies to manoj not caught by CC pass (30 days)

async function fetchEmails() {
  const client = new ImapFlow({
    host: 'imap.gmail.com',
    port: 993,
    secure: true,
    auth: { user: process.env.GMAIL_USER, pass: process.env.GMAIL_PASS },
    logger: false,
    tls: { rejectUnauthorized: false },
  });

  await client.connect();
  const emails = [];
  const seenIds = new Set();

  const since90 = new Date();
  since90.setDate(since90.getDate() - 90);
  const since30 = new Date();
  since30.setDate(since30.getDate() - 90);

  // ── Pass 1: Manoj's directives ──
  const lock1 = await client.getMailboxLock('[Gmail]/All Mail');
  try {
    for await (const msg of client.fetch(
      { from: MANOJ, since: since90 },
      { envelope: true, source: true }
    )) {
      if (emails.length >= 100) break;
      const parsed = await simpleParser(msg.source);
      const mid = parsed.messageId || String(msg.uid);
      if (seenIds.has(mid)) continue;
      seenIds.add(mid);
      emails.push({
        type: 'DIRECTIVE',
        subject: parsed.subject || '',
        date: parsed.date?.toISOString() || '',
        from: parsed.from?.text || '',
        to: parsed.to?.text || '',
        cc: parsed.cc?.text || '',
        body: parsed.text || '',   // full body — no truncation
      });
    }
  } finally { lock1.release(); }

  // ── Pass 2: Replies where Manoj is CC'd ──
  const lock2 = await client.getMailboxLock('[Gmail]/All Mail');
  try {
    for await (const msg of client.fetch(
      { cc: MANOJ, since: since30 },
      { envelope: true, source: true }
    )) {
      if (emails.length >= 180) break;
      const parsed = await simpleParser(msg.source);
      const mid = parsed.messageId || String(msg.uid);
      if (seenIds.has(mid)) continue;
      seenIds.add(mid);
      const fromAddr = (parsed.from?.value?.[0]?.address || '').toLowerCase();
      if (fromAddr === MANOJ) continue;
      emails.push({
        type: 'REPLY',
        subject: parsed.subject || '',
        date: parsed.date?.toISOString() || '',
        from: parsed.from?.text || '',
        to: parsed.to?.text || '',
        cc: parsed.cc?.text || '',
        body: parsed.text || '',   // full body
      });
    }
  } finally { lock2.release(); }

  // ── Pass 3: Direct replies TO manoj not caught above ──
  const lock3 = await client.getMailboxLock('[Gmail]/All Mail');
  try {
    for await (const msg of client.fetch(
      { to: MANOJ, since: since30 },
      { envelope: true, source: true }
    )) {
      if (emails.length >= 220) break;
      const parsed = await simpleParser(msg.source);
      const mid = parsed.messageId || String(msg.uid);
      if (seenIds.has(mid)) continue;
      seenIds.add(mid);
      const fromAddr = (parsed.from?.value?.[0]?.address || '').toLowerCase();
      if (fromAddr === MANOJ) continue;
      emails.push({
        type: 'REPLY',
        subject: parsed.subject || '',
        date: parsed.date?.toISOString() || '',
        from: parsed.from?.text || '',
        to: parsed.to?.text || '',
        cc: parsed.cc?.text || '',
        body: parsed.text || '',   // full body
      });
    }
  } finally {
    lock3.release();
    await client.logout();
  }

  return emails.sort((a, b) => new Date(a.date) - new Date(b.date));
}

// ── Claude analysis ─────────────────────────────────────────────────────────

async function analyzeWithClaude(emails) {
  const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

  const emailsText = emails
    .map(e => `[${e.type}] ${e.date} | FROM: ${e.from} | TO: ${e.to} | CC: ${e.cc} | SUBJECT: ${e.subject}\n\n${e.body}`)
    .join('\n\n---\n\n')
    .slice(0, 95000);

  const msg = await client.messages.create({
    model: 'claude-opus-4-8',
    max_tokens: 16000,
    messages: [{
      role: 'user',
      content: `You are a chief of staff. Analyse ALL emails from Manoj Tulsani (CEO, manoj@raynatours.com) to his team. [DIRECTIVE] emails are his original briefs. [REPLY] emails are team responses — use them to determine status and reply dates.

RULES:
- Extract EVERY distinct action directive — do NOT merge separate topics into one card
- One email with "Two New Partnership Tracks" = 2 separate items. "Two India Items" = 2 items.
- [REPLY] present for a thread = at minimum "IN PROGRESS" (yellow). No reply = "NO RESPONSE" (red).
- Aim for 35–50 items total across all sections.
- Include subTasks (3–6 bullet points) and sheetLinks (empty [] if none) on every item.
- For each item include "relatedSubjects": array of email subject strings that this directive came from.
- For each item include "lastReplyDate": "Mon DD" of the most recent [REPLY] on the thread, or null.
- For the "people" array: list EVERY person assigned ANY task, with ALL their tasks, exact deadlines, and assigned date.

Return ONLY valid JSON — no prose, no markdown fences:
{
  "stats": { "total": N, "onTrack": N, "inProgress": N, "notedOnly": N, "noResponse": N },
  "sections": [{ "id": "slug", "title": "emoji + section name", "items": [{
    "id": "kebab-slug",
    "title": "specific directive title",
    "statusLabel": "AT RISK|IN PROGRESS|ON TRACK|NO RESPONSE|ONLY NOTED|ACKNOWLEDGED|COMPLETED",
    "color": "red|yellow|green|orange",
    "deadline": "string or null",
    "deadlineUrgency": "urgent|warning|ok|null",
    "emailDate": "Mon DD",
    "lastReplyDate": "Mon DD or null",
    "owners": ["First name"],
    "progress": 0-100,
    "progressLabel": "one line on current state",
    "response": "summary of team reply or null",
    "noReplyWarning": "warning if no reply, else null",
    "subTasks": ["action 1", "action 2"],
    "sheetLinks": [],
    "relatedSubjects": ["exact email subject line"]
  }]}],
  "people": [{ "name": "First name", "tasks": [{
    "task": "specific task description",
    "assignedDate": "Mon DD",
    "deadline": "Jun DD or ASAP or TBD",
    "deadlineUrgency": "urgent|warning|ok|null",
    "color": "red|yellow|green|orange"
  }]}]
}

Color: red=no response/at risk, orange=noted only, yellow=in progress, green=on track/done.
Section IDs (use exactly): critical, tech-product, marketing-partnerships, pr-brand-content, operations-pricing, acknowledged-closed.
People: include Vaibhav, Asad, Aparna, Manish, Gaurav, Deepak, Malik, Anket, Azhar, Rishi, Nihad, Pari, Rajkumar, Ranjan, Alok, Senthil, Anwar and any others with tasks.

Emails:
${emailsText}`,
    }],
  });

  const text = msg.content[0].text;
  const match = text.match(/\{[\s\S]*\}/);
  if (!match) throw new Error('No JSON in Claude response');
  return JSON.parse(match[0]);
}

// ── Main handler ────────────────────────────────────────────────────────────

module.exports = async function handler(req, res) {
  const auth = req.headers['authorization'];
  if (process.env.CRON_SECRET && auth !== `Bearer ${process.env.CRON_SECRET}`) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  try {
    console.log('[refresh] Fetching emails (from/cc/to manoj)…');
    const emails = await fetchEmails();

    if (!emails.length) {
      return res.status(200).json({ message: 'No emails found.' });
    }
    const dirCount = emails.filter(e => e.type === 'DIRECTIVE').length;
    const repCount = emails.filter(e => e.type === 'REPLY').length;
    console.log(`[refresh] ${dirCount} directives + ${repCount} replies. Sending to Claude…`);

    const analyzed = await analyzeWithClaude(emails);

    let sheetUrl = '';
    try {
      const r = await fetch(`https://${req.headers.host}/data.json`);
      if (r.ok) sheetUrl = (await r.json())?.meta?.googleSheetUrl || '';
    } catch (_) {}

    const newData = {
      meta: {
        lastUpdated: new Date().toISOString(),
        source: `Gmail · ${dirCount} directives + ${repCount} replies`,
        googleSheetUrl: sheetUrl,
      },
      stats: analyzed.stats,
      sections: analyzed.sections,
      people: analyzed.people || [],
      emails: emails.map(e => ({
        type: e.type,
        subject: e.subject,
        date: e.date,
        from: e.from,
        to: e.to,
        cc: e.cc,
        body: e.body,
      })),
    };

    console.log('[refresh] Committing data.json to GitHub…');
    const commitSha = await commitDataJson(JSON.stringify(newData, null, 2));
    console.log('[refresh] Committed:', commitSha);

    return res.status(200).json({
      success: true,
      directives: dirCount,
      replies: repCount,
      total: newData.stats.total,
      commitSha,
      lastUpdated: newData.meta.lastUpdated,
    });
  } catch (err) {
    console.error('[refresh] Error:', err.message);
    return res.status(500).json({ error: err.message });
  }
};
