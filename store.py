"""SQLite state for the follower: settings, templates, sends, clients, inbound, suppress list, log.

One small database at DATA_DIR/follower.db (WAL). Every helper opens its own short connection,
so the scheduler thread and the web threads can share it safely.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
DB_PATH = os.path.join(DATA_DIR, "follower.db")

_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, step INTEGER,
    body TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, step INTEGER NOT NULL,
    to_addr TEXT NOT NULL, from_addr TEXT NOT NULL, subject TEXT NOT NULL, message_id TEXT,
    body TEXT, status TEXT NOT NULL, error TEXT, landed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sends_live ON sends(thread_id, step) WHERE status = 'sent';
CREATE UNIQUE INDEX IF NOT EXISTS ux_sends_dry ON sends(thread_id, step) WHERE status = 'dry';
CREATE INDEX IF NOT EXISTS ix_sends_created ON sends(created_at);
CREATE TABLE IF NOT EXISTS clients (
    thread_id TEXT PRIMARY KEY, addr TEXT NOT NULL, name TEXT, domain TEXT, subject TEXT,
    stage TEXT NOT NULL, category TEXT, program TEXT, platform TEXT, platform_url TEXT,
    reward_amount REAL, currency TEXT, invoice_method TEXT, invoice_url TEXT,
    payment_status TEXT, payment_date TEXT, report_sent INTEGER NOT NULL DEFAULT 0,
    report_sent_date TEXT, report_count INTEGER NOT NULL DEFAULT 0, fix_status TEXT,
    pentest TEXT, next_action TEXT, notes TEXT, do_not_contact INTEGER NOT NULL DEFAULT 0,
    needs_reply INTEGER NOT NULL DEFAULT 0, last_reply_ts REAL, last_reply_key TEXT,
    last_reply_excerpt TEXT, draft_template TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inbound (
    key TEXT PRIMARY KEY, thread_id TEXT, from_addr TEXT NOT NULL, ts REAL NOT NULL,
    kind TEXT NOT NULL, category TEXT, platform TEXT, platform_url TEXT, amounts TEXT,
    excerpt TEXT, processed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS suppress (
    addr TEXT PRIMARY KEY, reason TEXT, source TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, level TEXT NOT NULL,
    event TEXT NOT NULL, thread_id TEXT, message TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS ix_log_ts ON log(ts);
"""

DEFAULT_SETTINGS = {
    "paused": False,
    "dry_run": True,
    "wait_days": [7, 7],
    "window": {"days": [0, 1, 2, 3, 4], "start": "13:00", "end": "03:00", "tz": "Asia/Kolkata"},
    "daily_cap": 150,
    "min_gap_seconds": 120,
    "queue_order": "newest",          # newest initial email first; or "oldest"
    "sender_mode": "initial",         # "initial" = same address as the initial email; or "fixed"
    "fixed_sender": "",               # used when sender_mode == "fixed", e.g. ramsha.mirza@vulgone.com
    "fixed_sender_name": "",
    "backlog_since": "2026-01-01",    # threads whose first email is older are left alone
    "bounce_guard": True,             # auto-pause if follow-ups start bouncing
    "backfilled": False,
}

# The team's own scripts, copied verbatim from the mailbox's Templates folder.
SEED_TEMPLATES = [
    ("followup_1", "1st follow-up", "followup", 2,
     "Hello Team,\n\nJust following up on our previous message regarding security vulnerability "
     "reporting confirmation.\n\nWe would appreciate your response when convenient.\n\nRegards,\n"
     "Ramsha from Vulgone"),
    ("followup_2", "2nd (final) follow-up", "followup", 3,
     "Hello Team,\n\nThis is a final follow-up regarding our earlier inquiry about your security "
     "vulnerability reporting process.\n\nIf you are not currently accepting such reports, a brief "
     "confirmation would be appreciated.\n\nRegards,\nRamsha from Vulgone"),
    ("reply_reward_probe", "Arrange reward for impactful findings?", "reply", None,
     "Hi,\n\nThanks for your response. I read your policy and saw that monetary rewards are not "
     "currently offered, so I just wanted to ask if there’s any flexibility for discretionary "
     "rewards in case of impactful findings.\n\nRegards,\nSahil Negi"),
    ("reply_no_bbp", "No formal bug bounty - discretionary recognition?", "reply", None,
     "Hello,\n\nThanks for the clarification.\n\nI understand that your organization does not "
     "currently run a formal bug bounty program. In many cases, public programs can also introduce "
     "a high volume of low-quality or duplicate reports, so a more focused and responsible disclosure "
     "approach often works more efficiently.\n\nI’m happy to responsibly share any validated "
     "security findings with your team via this channel.\n\nIn case any reported issue turns out to "
     "have significant impact, I’d appreciate if discretionary recognition (if possible) could be "
     "considered. Either way, my priority is to ensure responsible disclosure and remediation.\n\n"
     "I’ll proceed accordingly and share any relevant findings.\n\nBest regards,\nSahil Negi\nVulgone"),
    ("reply_hackerone_invite", "Request invitation to private HackerOne program", "reply", None,
     "Hi Team\n\nI noticed your bug bounty program is managed privately via HackerOne.\n\nI’m a "
     "security researcher interested in participating. I’d appreciate an invitation to your "
     "program so I can help identify and responsibly report any vulnerabilities.\n\nHackerOne "
     "profile: https://hackerone.com/dracula74644\n\nEmail: dracula74644@wearehackerone.com\n\n"
     "Thanks for your time.\n\nRegards,\nZeeshan\nVulgone"),
    ("reply_small_startup", "Small startup - predefined engagement", "reply", None,
     "Hi {company},\n\nThank you for your response.\n\nJust to clarify, we’re not specifically "
     "suggesting a full public bug bounty program. We’re referring to a directly approved "
     "pentest/security assessment engagement with your team, where the scope, rules, and even "
     "rewards/budget can be predefined based on severity levels or findings.\n\nWe’ve worked "
     "with multiple companies in a similar manner before, and it has been beneficial for them as "
     "well. Starting with a limited number of trusted testers usually helps reduce unnecessary "
     "traffic/noise and keeps the report volume manageable while still identifying critical "
     "security issues effectively. In several cases, companies later expanded into structured "
     "public programs after completing the initial testing and remediation phase.\n\nIf your team "
     "is open to such an arrangement, we can proceed strictly after approval and within the "
     "mutually agreed scope.\n\nRegards,\nSahil Negi"),
    ("reply_via_researcher", "Report sent via researcher@", "reply", None,
     "Hello Team,\n\nWe have forwarded the vulnerability details via researcher@vulgone.com.\n\n"
     "Please have a look & let us know about it.\n\nRegards\nVulgone Security"),
]

# Companies that asked us to stop, plus dead inboxes (from the mailbox review, 2026-10-06).
SEED_SUPPRESS = [
    ("@answerhq.co", "asked to unsubscribe"), ("@folk-contact.com", "not interested"),
    ("@infosistema.com", "not interested"), ("@nanocart.app", "not interested, do not contact"),
    ("@postopus.com", "not interested"), ("@provineer.com", "asked to unsubscribe"),
    ("@reelu.io", "not accepting unsolicited security outreach"), ("@secalerts.co", "not interested"),
    ("@stepcapture.com", "not interested"), ("hellobertrand@gmail.com", "not interested"),
    ("@fluidwave.com", "asked to unsubscribe"), ("@demandsphere.com", "legal: testing not authorized"),
    ("@m.io", "legal: testing not authorized"), ("@elektror.com", "legal: unsolicited testing not permitted"),
    ("@homelight.com", "inbox no longer monitored"), ("@proprietio.com", "mail deleted unread"),
    ("@secureelements.co.uk", "mail deleted unread"),
]


@contextmanager
def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with _write_lock, db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        now = time.time()
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))
        have = {r["name"] for r in conn.execute("PRAGMA table_info(clients)").fetchall()}
        for col, decl in (("researcher_category", "TEXT"), ("researcher_excerpt", "TEXT"), ("researcher_last_ts", "REAL"),
                          ("needs_reply_researcher", "INTEGER NOT NULL DEFAULT 0"), ("paid_total", "REAL")):
            if col not in have:
                conn.execute(f"ALTER TABLE clients ADD COLUMN {col} {decl}")
        # Stage names were simplified on 2026-10-07; map rows created under the old names once.
        if not conn.execute("SELECT 1 FROM settings WHERE key = 'stages_v2'").fetchone():
            for old, new in {"greenlit": "sample", "no_bounty": "answer", "acknowledged": "waiting", "rerouted": "answer",
                             "question": "answer", "review": "answer", "asked_reward": "answer", "report_sent": "sample_sent",
                             "fix_confirmed": "in_talks", "reward_agreed": "in_talks", "invoiced": "in_talks",
                             "paid": "in_talks", "pentest_lead": "in_talks", "declined": "closed"}.items():
                conn.execute("UPDATE clients SET stage = ? WHERE stage = ?", (new, old))
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('stages_v2', 'true')")
        if conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO templates(id, name, kind, step, body, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(i, n, k, s, b, now) for i, n, k, s, b in SEED_TEMPLATES],
            )
        if conn.execute("SELECT COUNT(*) FROM suppress").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO suppress(addr, reason, source, created_at) VALUES (?, ?, 'research', ?)",
                [(a, r, now) for a, r in SEED_SUPPRESS],
            )


# ---------------------------------------------------------------- settings / status

def get_settings():
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except ValueError:
            pass
    return out


def set_setting(key, value):
    with _write_lock, db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))


def get_status():
    return get_settings().get("status") or {}


def set_status(**fields):
    status = get_status()
    status.update(fields)
    set_setting("status", status)
    return status


# ---------------------------------------------------------------- log

def log(event, message, thread_id=None, level="info", data=None):
    with _write_lock, db() as conn:
        conn.execute(
            "INSERT INTO log(ts, level, event, thread_id, message, data) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), level, event, thread_id, message, json.dumps(data) if data is not None else None),
        )


def activity(limit=200, since=None):
    with db() as conn:
        if since:
            rows = conn.execute("SELECT * FROM log WHERE ts > ? ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- templates

def templates():
    with db() as conn:
        rows = conn.execute("SELECT * FROM templates ORDER BY kind, step, name").fetchall()
    return [dict(r) for r in rows]


def template(template_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return dict(row) if row else None


def followup_template(step):
    with db() as conn:
        row = conn.execute("SELECT * FROM templates WHERE kind = 'followup' AND step = ?", (step,)).fetchone()
    return dict(row) if row else None


def save_template(template_id, body, name=None):
    with _write_lock, db() as conn:
        if name is None:
            conn.execute("UPDATE templates SET body = ?, updated_at = ? WHERE id = ?", (body, time.time(), template_id))
        else:
            conn.execute("UPDATE templates SET body = ?, name = ?, updated_at = ? WHERE id = ?",
                         (body, name, time.time(), template_id))


# ---------------------------------------------------------------- sends

def record_send(thread_id, step, to_addr, from_addr, subject, message_id, body, status, error=None):
    with _write_lock, db() as conn:
        try:
            conn.execute(
                "INSERT INTO sends(thread_id, step, to_addr, from_addr, subject, message_id, body, status, error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (thread_id, step, to_addr, from_addr, subject, message_id, body, status, error, time.time()),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def sends(status=None, since=None, limit=500):
    with db() as conn:
        where, args = [], []
        if status:
            where.append("status = ?"); args.append(status)
        if since:
            where.append("created_at >= ?"); args.append(since)
        sql = "SELECT * FROM sends" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = conn.execute(sql, (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def sent_steps(statuses=("sent",)):
    """(thread_id, step) pairs already produced by the follower."""
    with db() as conn:
        marks = ",".join("?" * len(statuses))
        rows = conn.execute(f"SELECT thread_id, step FROM sends WHERE status IN ({marks})", tuple(statuses)).fetchall()
    return {(r["thread_id"], r["step"]) for r in rows}


def last_send_ts(statuses=("sent",)):
    with db() as conn:
        marks = ",".join("?" * len(statuses))
        row = conn.execute(f"SELECT MAX(created_at) AS t FROM sends WHERE status IN ({marks})", tuple(statuses)).fetchone()
    return row["t"] or 0


def sends_since(ts, statuses=("sent",)):
    with db() as conn:
        marks = ",".join("?" * len(statuses))
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM sends WHERE created_at >= ? AND status IN ({marks})", (ts, *statuses)
        ).fetchone()
    return row["n"]


def mark_landed(message_ids):
    if not message_ids:
        return
    with _write_lock, db() as conn:
        conn.executemany("UPDATE sends SET landed = 1 WHERE message_id = ? AND landed = 0",
                         [(m,) for m in message_ids])


def unlanded_message_ids():
    with db() as conn:
        rows = conn.execute("SELECT message_id FROM sends WHERE status = 'sent' AND landed = 0 AND message_id IS NOT NULL").fetchall()
    return [r["message_id"] for r in rows]


# ---------------------------------------------------------------- suppress list

def suppress_list():
    with db() as conn:
        rows = conn.execute("SELECT * FROM suppress ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def suppress_set():
    with db() as conn:
        rows = conn.execute("SELECT addr FROM suppress").fetchall()
    return {r["addr"].lower() for r in rows}


def is_suppressed(addr, suppressed=None):
    addr = (addr or "").lower()
    suppressed = suppress_set() if suppressed is None else suppressed
    return addr in suppressed or ("@" + addr.split("@")[-1]) in suppressed


def add_suppress(addr, reason, source="manual"):
    with _write_lock, db() as conn:
        conn.execute("INSERT OR REPLACE INTO suppress(addr, reason, source, created_at) VALUES (?, ?, ?, ?)",
                     (addr.lower(), reason, source, time.time()))


def remove_suppress(addr):
    with _write_lock, db() as conn:
        conn.execute("DELETE FROM suppress WHERE addr = ?", (addr.lower(),))


# ---------------------------------------------------------------- inbound / clients

def inbound_keys():
    with db() as conn:
        rows = conn.execute("SELECT key FROM inbound").fetchall()
    return {r["key"] for r in rows}


def record_inbound(key, thread_id, from_addr, ts, kind, category=None, platform=None,
                   platform_url=None, amounts=None, excerpt=None):
    with _write_lock, db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO inbound(key, thread_id, from_addr, ts, kind, category, platform, platform_url,"
            " amounts, excerpt, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, thread_id, from_addr, ts, kind, category, platform, platform_url,
             json.dumps(amounts) if amounts else None, excerpt, time.time()),
        )


def get_client(thread_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM clients WHERE thread_id = ?", (thread_id,)).fetchone()
    return dict(row) if row else None


CLIENT_FIELDS = {
    "addr", "name", "domain", "subject", "stage", "category", "program", "platform", "platform_url",
    "reward_amount", "currency", "invoice_method", "invoice_url", "payment_status", "payment_date",
    "report_sent", "report_sent_date", "report_count", "fix_status", "pentest", "next_action", "notes",
    "do_not_contact", "needs_reply", "last_reply_ts", "last_reply_key", "last_reply_excerpt", "draft_template",
    "researcher_category", "researcher_excerpt", "researcher_last_ts", "needs_reply_researcher", "paid_total",
}


def upsert_client(thread_id, **fields):
    fields = {k: v for k, v in fields.items() if k in CLIENT_FIELDS}
    now = time.time()
    with _write_lock, db() as conn:
        row = conn.execute("SELECT thread_id FROM clients WHERE thread_id = ?", (thread_id,)).fetchone()
        if row:
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE clients SET {sets}, updated_at = ? WHERE thread_id = ?",
                             (*fields.values(), now, thread_id))
        else:
            fields.setdefault("stage", "review")
            cols = ", ".join(["thread_id", *fields, "created_at", "updated_at"])
            marks = ", ".join("?" * (len(fields) + 3))
            conn.execute(f"INSERT INTO clients({cols}) VALUES ({marks})", (thread_id, *fields.values(), now, now))


def clients(stage=None, needs_reply=None, needs_reply_researcher=None, limit=2000):
    with db() as conn:
        where, args = [], []
        if stage:
            where.append("stage = ?"); args.append(stage)
        if needs_reply is not None:
            where.append("needs_reply = ?"); args.append(1 if needs_reply else 0)
        if needs_reply_researcher is not None:
            where.append("needs_reply_researcher = ?"); args.append(1 if needs_reply_researcher else 0)
        sql = "SELECT * FROM clients" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY COALESCE(last_reply_ts, updated_at) DESC LIMIT ?"
        rows = conn.execute(sql, (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def client_counts():
    with db() as conn:
        rows = conn.execute("SELECT stage, COUNT(*) AS n, SUM(needs_reply) AS nr FROM clients GROUP BY stage").fetchall()
    return {"stages": {r["stage"]: {"n": r["n"], "needs_reply": r["nr"] or 0} for r in rows}}


def bounces_since(ts):
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM inbound WHERE kind = 'bounce' AND ts >= ?", (ts,)).fetchone()
    return row["n"]


def backup(keep=14):
    """Online snapshot of the database to DATA_DIR/backups, keeping the most recent `keep`."""
    import glob
    bdir = os.path.join(DATA_DIR, "backups")
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, time.strftime("follower-%Y%m%d.db"))
    with _write_lock, db() as src:
        with sqlite3.connect(dst) as out:
            src.backup(out)
    old = sorted(glob.glob(os.path.join(bdir, "follower-*.db")))[:-keep]
    for f in old:
        try:
            os.remove(f)
        except OSError:
            pass
    return dst


def mark_handled_older_than(ts):
    with _write_lock, db() as conn:
        cur = conn.execute("UPDATE clients SET needs_reply = 0, updated_at = ? WHERE needs_reply = 1 AND COALESCE(last_reply_ts, 0) < ?",
                           (time.time(), ts))
    return cur.rowcount
