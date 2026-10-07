"""The follow-up scheduler: sync the mailbox, rebuild threads, read new replies, send what is due.

Runs as one daemon thread (see start()). Everything that decides *whether* to send is a pure
function here so it can be unit-tested without a mailbox: window_open, compute_queue, send_gate,
classify_reply.
"""
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import os

import mail
import sender
import store
import threads as th

TICK_SECONDS = 30
BACKOFF_AFTER_ERROR = 600
ERRORS_BEFORE_PAUSE = 3
BACKFILL_BATCH = 150
# Bounce guard: pause if, among the last day's follower sends, the bounce rate is this high.
BOUNCE_WINDOW = 24 * 3600
BOUNCE_MIN_SENDS = 20
BOUNCE_RATE_PAUSE = 0.3

# ---------------------------------------------------------------- reply classification

STOP_RE = re.compile(
    r"not interested|unsubscribe|stop (emailing|contacting|sending|reaching)|do not (contact|email|reach)"
    r"|remove (me|us) from|not authori[sz]ed|unauthori[sz]ed (testing|penetration|scanning|probing)"
    r"|cease and desist|legal action|computer (crime|fraud|misuse)|refrain from (any )?(further|testing)"
    r"|not accepting unsolicited|do not permit unsolicited|please (stop|refrain)",
    re.I,
)
PLATFORM_NAMES = ["hackerone", "bugcrowd", "intigriti", "yeswehack", "huntr", "openbugbounty", "immunefi"]
PROGRAM_RE = re.compile(
    r"hackerone|bugcrowd|intigriti|yeswehack|huntr|openbugbounty|immunefi|security\.txt"
    r"|responsible[- ]disclosure|disclosure (policy|program|page)|bug bounty program|\bvdp\b"
    r"|submit (it |them |your (report|findings?) )?(via|through|on|at|using) (our|the) (form|portal|page|platform|program)"
    r"|https?://\S*(security|disclosure|bounty|vulnerab)\S*",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s<>\")\]]+", re.I)
NO_BOUNTY_RE = re.compile(
    r"(no|not|don'?t|do not|does not|doesn'?t|without|never|unable to|cannot|can'?t|isn'?t|aren'?t)"
    r"[^.\n]{0,60}(bount|reward|monetary|compensat|budget|cash|pay)|no (bug )?bounty|not (offering|paying)"
    r"|free of charge|no (financial|monetary)",
    re.I,
)
GO_AHEAD_RE = re.compile(
    r"(send|share|forward|submit|email|provide|report|disclose) (it|them|us|me|over|the (details|report|findings?|issue|vulnerabilit\w*|information))"
    r"|feel free to (share|send|submit|reach)|go ahead|please (send|share|forward|submit|provide)|you can (send|share|email|submit)"
    r"|happy to (take a look|receive|review|hear)|let us know what you (found|have)|what did you find|more details"
    r"|^(yes|confirmed|sure|correct|absolutely)\b|right (contact|place|person|channel)|this (is|would be) the (correct|right)",
    re.I,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
FORWARD_RE = re.compile(
    r"forward(ed|ing)? (this |it |your (message|email|report) )?to|(contact|reach out to|email|write to|send (it|this|them) to)"
    r"|right person is|should be directed to|handled by|responsible for",
    re.I,
)
ESCALATED_RE = re.compile(r"escalated to|passed (this|it) (on|along) to|forwarded (this|it|your (message|email)) to (our|the|my)", re.I)
QUESTION_RE = re.compile(
    r"who are you|which (site|website|account|platform|domain|product)|what (company|service|exactly)|are you (selling|offering)"
    r"|(could|can) you (please )?(clarify|elaborate|specify|tell us|explain)|what is this (about|regarding)|what do you mean",
    re.I,
)
ACK_RE = re.compile(
    r"(we|i) (have )?received (your|the)|will (get back|review|look into|respond|revert|be in touch)|thank you for (contacting|reaching out|your (email|message))"
    r"|ticket (has been |was )?(created|opened|logged|raised)|our team (is|will)|has been forwarded to (our|the) (team|security)",
    re.I,
)
SMALL_STARTUP_RE = re.compile(r"small (company|team|startup|business)|bootstrap|solo founder|no (budget|revenue)|early[- ]stage", re.I)

R_STOP_RE = re.compile(r"do not (send|waste|authori[sz]e)|no longer waste|please do not send|will not (be )?able to (pay|consider)", re.I)


def classify_researcher_reply(text):
    """A client's reply in the researcher mailbox: did they ask us to stop, or is the talk continuing?"""
    t = (text or "").strip()
    category = "stop" if (STOP_RE.search(t) or R_STOP_RE.search(t)) else "reply"
    return {"category": category, "excerpt": re.sub(r"\s+", " ", t)[:400]}


# category -> (stage, needs_reply, draft template id or "")
CATEGORY_EFFECTS = {
    "stop": ("confirm_stop", 0, ""),
    "program": ("program", 0, ""),
    "no_bounty": ("answer", 1, "reply_no_bbp"),
    "go_ahead": ("sample", 0, ""),       # they said "report it to this email" -> send a sample from researcher@
    "forwarded": ("answer", 1, ""),
    "question": ("answer", 1, ""),
    "ack": ("waiting", 0, ""),
    "other": ("answer", 1, ""),
}


def classify_reply(subject, text, from_addr=""):
    """Category + extracted facts for one client reply (own words only; pass quoted history stripped)."""
    t = (text or "").strip()
    low = t.lower()
    urls = URL_RE.findall(t)
    platform = next((p for p in PLATFORM_NAMES if p in low), "")
    platform_url = next((u for u in urls if re.search(r"security|disclosure|bounty|vulnerab|hackerone|bugcrowd|intigriti", u, re.I)), "")
    sender_domain = (from_addr or "").split("@")[-1].lower()
    # "contact security@other-company.com" is a reroute; "send it to security@<their own domain>" is a go-ahead.
    other_contacts = [e for e in EMAIL_RE.findall(t)
                      if not e.lower().endswith("@" + mail.OWN_DOMAIN) and e.split("@")[-1].lower() != sender_domain]
    rerouted = bool(other_contacts) and bool(FORWARD_RE.search(t)) or bool(ESCALATED_RE.search(t))

    if STOP_RE.search(t):
        category = "stop"
    elif platform or platform_url:
        category = "program"  # a concrete place to submit beats a "no bounty" aside
    elif NO_BOUNTY_RE.search(t):
        category = "no_bounty"
    elif PROGRAM_RE.search(t):
        category = "program"
    elif rerouted:
        category = "forwarded"
    elif GO_AHEAD_RE.search(t):
        category = "go_ahead"
    elif QUESTION_RE.search(t) or (t.count("?") >= 1 and len(t) < 300):
        category = "question"
    elif ACK_RE.search(t) and len(t) < 600:
        category = "ack"
    else:
        category = "other"

    stage, needs_reply, draft = CATEGORY_EFFECTS[category]
    if category == "no_bounty" and SMALL_STARTUP_RE.search(t):
        draft = "reply_small_startup"
    if category == "program" and platform == "hackerone":
        draft = "reply_hackerone_invite"
    return {
        "category": category, "stage": stage, "needs_reply": needs_reply, "draft_template": draft,
        "platform": platform, "platform_url": platform_url, "excerpt": re.sub(r"\s+", " ", t)[:400],
    }


# ---------------------------------------------------------------- time window

def _tz(window):
    try:
        return ZoneInfo(window.get("tz") or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("UTC")


def _hm(value, default):
    try:
        h, m = (value or default).split(":")
        return int(h), int(m)
    except Exception:
        return [int(x) for x in default.split(":")]


def window_open(now, window):
    """Is `now` (aware datetime or epoch) inside the sending window?

    The window starts at `start` on each listed weekday and ends at `end`; when end <= start it
    runs past midnight into the next day (the weekday check applies to the day it started).
    """
    tz = _tz(window)
    if isinstance(now, (int, float)):
        now = datetime.fromtimestamp(now, tz)
    now = now.astimezone(tz)
    sh, sm = _hm(window.get("start"), "13:00")
    eh, em = _hm(window.get("end"), "03:00")
    days = set(window.get("days") or [0, 1, 2, 3, 4])
    start_today = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    overnight = (eh, em) <= (sh, sm)
    for start_day in (start_today, start_today - timedelta(days=1)):
        end = start_day.replace(hour=eh, minute=em) + (timedelta(days=1) if overnight else timedelta())
        if start_day <= now < end and start_day.weekday() in days:
            return True
    return False


def next_window_start(now, window):
    tz = _tz(window)
    now = datetime.fromtimestamp(now, tz) if isinstance(now, (int, float)) else now.astimezone(tz)
    sh, sm = _hm(window.get("start"), "13:00")
    days = set(window.get("days") or [0, 1, 2, 3, 4])
    for d in range(0, 8):
        day = (now + timedelta(days=d)).replace(hour=sh, minute=sm, second=0, microsecond=0)
        if day > now and day.weekday() in days:
            return day
    return None


# ---------------------------------------------------------------- queue

def compute_queue(threads, settings, suppressed, done_steps, now):
    """Threads that should get their next follow-up now, in sending order, plus skip counts.

    `suppressed` is the set of suppressed addresses/'@domains'; `done_steps` the (thread_id, step)
    pairs the follower already sent. Rules 1-6 of the plan.
    """
    waits = settings.get("wait_days") or [7, 7]
    waits = (waits, waits) if isinstance(waits, (int, float)) else tuple(waits)
    try:
        backlog_since = datetime.fromisoformat(settings.get("backlog_since") or "2026-01-01").timestamp()
    except ValueError:
        backlog_since = 0
    queue, skipped = [], {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for t in threads:
        if t["inbound"] or t["sends"] == 0:
            skip("client wrote first"); continue
        if th.is_internal(t["addr"], mail.OWN_DOMAIN):
            skip("internal address"); continue
        if t["addr"] in suppressed or ("@" + t["domain"]) in suppressed:
            skip("suppressed"); continue
        if t["replies"] or t["stage"] in ("replied", "bounced"):
            skip("replied or bounced"); continue
        if t.get("engaged_at_domain"):
            skip("engaged at domain"); continue
        if t.get("researcher"):
            skip("talking on researcher@"); continue
        if t["stage"] not in ("sent", "followup1"):
            skip("sequence finished"); continue
        if t["first_ts"] < backlog_since:
            skip("older than backlog start"); continue
        step = t["steps"] + 1
        if (t["id"], step) in done_steps:
            skip("already sent by follower"); continue
        wait = waits[0] if step == 2 else waits[1]
        if (now - t["last_out_ts"]) / 86400 < wait:
            skip("waiting period not over"); continue
        queue.append({"thread": t, "step": step})

    newest_first = settings.get("queue_order", "newest") != "oldest"
    queue.sort(key=lambda c: (-c["thread"]["first_ts"] if newest_first else c["thread"]["first_ts"], c["thread"]["last_out_ts"]))
    return queue, skipped


def send_gate(settings, now, sent_today, last_any_send_ts, errors_in_row, last_error_ts):
    """(allowed, reason, retry_at). Checks every gate except the per-thread rules."""
    if settings.get("paused"):
        return False, "paused", None
    window = settings.get("window") or {}
    if not window_open(now, window):
        nxt = next_window_start(now, window)
        return False, "outside sending window", nxt.timestamp() if nxt else None
    if sent_today >= int(settings.get("daily_cap") or 0):
        return False, "daily cap reached", None
    gap = int(settings.get("min_gap_seconds") or 120)
    if now - last_any_send_ts < gap:
        return False, "rate limit", last_any_send_ts + gap
    if errors_in_row and now - last_error_ts < BACKOFF_AFTER_ERROR:
        return False, "backing off after error", last_error_ts + BACKOFF_AFTER_ERROR
    return True, "", None


def day_start_ts(settings, now):
    tz = _tz(settings.get("window") or {})
    d = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


# ---------------------------------------------------------------- the scheduler

class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(name="follower-scheduler", daemon=True)
        self.stop_event = threading.Event()
        self.state = {"threads": [], "unmatched": [], "built_at": 0, "errors_in_row": 0, "last_error_ts": 0}
        self.lock = threading.Lock()

    # -- data -------------------------------------------------------------
    def refresh(self, wait_days=None):
        """Sync every folder and rebuild the thread model. Called each tick and on demand."""
        settings = store.get_settings()
        waits = wait_days or settings.get("wait_days") or [7, 7]
        with mail.mb.lock:
            conn = mail.mb.get()
            names = mail.folder_names(conn)
            sent = mail.collect(conn, [names["sent"]], None)
            received = mail.collect(conn, names["reply"], None)
        unmatched = []
        now = time.time()
        built = [th.finish_thread(t, waits, now) for t in th.build_threads(sent, received, mail.OWN_DOMAIN, unmatched)]
        th.mark_domain_engagement(built)
        r_sent, r_received = self.researcher_mail()
        th.attach_researcher(built, r_sent, r_received, mail.OWN_DOMAIN)
        with self.lock:
            self.state.update(threads=built, unmatched=unmatched, built_at=now)
        # Our own sends that have shown up in Sent.
        pending = set(store.unlanded_message_ids())
        if pending:
            landed = [m["message_id"] for m in sent if m["message_id"] in pending]
            store.mark_landed(landed)
        return built

    def threads(self):
        with self.lock:
            return list(self.state["threads"]), list(self.state["unmatched"])

    def researcher_mail(self):
        """(sent, received) summaries of the researcher mailbox, or empty lists when it is off/failing."""
        box = mail.researcher
        if box is None:
            return [], []
        try:
            with box.lock:
                conn = box.get()
                names = mail.folder_names(conn, box)
                r_sent = mail.collect(conn, [names["sent"]], None, box)
                r_received = mail.collect(conn, names["reply"], None, box)
            self.state["researcher_error"] = ""
            return r_sent, r_received
        except Exception as exc:  # noqa: BLE001 - a second mailbox must never stop the follower
            msg = str(exc)
            if self.state.get("researcher_error") != msg:
                store.log("researcher_error", f"researcher mailbox: {msg}", level="warn")
            self.state["researcher_error"] = msg
            return [], []

    def sync_reports(self, threads):
        """The sample went out from researcher@ -> stage "Sample sent"; then follow the talk there."""
        for t in threads:
            if not t.get("report_sent_ts"):
                continue
            c = store.get_client(t["id"])
            if c is None:
                store.upsert_client(t["id"], addr=t["addr"], name=t["name"], domain=t["domain"], subject=t["subject"],
                                    stage="sample_sent")
                c = store.get_client(t["id"])
            if not (c["report_sent"] and c["report_count"] == t["report_count"]):
                fields = {"report_sent": 1, "report_count": t["report_count"],
                          "report_sent_date": datetime.fromtimestamp(t["report_sent_ts"]).strftime("%Y-%m-%d")}
                if c["stage"] in ("answer", "sample", "waiting", "program"):
                    fields["stage"] = "sample_sent"
                store.upsert_client(t["id"], **fields)
                store.log("sample_sent", f"sample/report to {t['addr']} seen in researcher@ ({t['report_count']} email(s))", t["id"])
        self.ingest_researcher(threads)

    def ingest_researcher(self, threads):
        """New client replies in the researcher mailbox: the talk is on, and is it our turn?"""
        seen = store.inbound_keys()
        for t in threads:
            msgs = t.get("researcher") or []
            if not msgs:
                continue
            last_out = max((m["ts"] for m in msgs if m["dir"] == "out"), default=0)
            for m in sorted(msgs, key=lambda x: x["ts"]):
                if m["dir"] != "in" or m["kind"] != "reply":
                    continue
                key = f"researcher:{m['folder']}#{m['uid']}"
                if key in seen:
                    continue
                try:
                    c = classify_researcher_reply(mail.fetch_text(m["folder"], m["uid"], "researcher"))
                except Exception as exc:  # noqa: BLE001
                    store.log("inbound_error", f"researcher mail {t['addr']}: {exc}", t["id"], level="error")
                    continue
                store.record_inbound(key, t["id"], m["who"]["addr"], m["ts"], "reply", c["category"], None, None, None, c["excerpt"])
                row = store.get_client(t["id"]) or {}
                f = {"researcher_category": c["category"], "researcher_excerpt": c["excerpt"], "researcher_last_ts": m["ts"],
                     "needs_reply_researcher": 1 if (m["ts"] > last_out and c["category"] != "stop") else 0}
                if c["category"] == "stop":
                    f["stage"] = "confirm_stop"
                elif row.get("stage") in (None, "answer", "sample", "waiting", "program", "sample_sent"):
                    f["stage"] = "in_talks"
                if not row:
                    f.update(addr=t["addr"], name=t["name"], domain=t["domain"], subject=t["subject"])
                store.upsert_client(t["id"], **f)
                store.log("researcher_reply", f"{m['who']['addr']} wrote to researcher@" + (" (asked to stop)" if c["category"] == "stop" else ""), t["id"])

    # -- inbound ----------------------------------------------------------
    def ingest_inbound(self, threads, limit=None):
        """Classify client replies we have not processed yet and update the pipeline rows."""
        seen = store.inbound_keys()
        todo = []
        for t in threads:
            for m in t["msgs"]:
                if m["dir"] == "in" and m["kind"] in ("reply", "bounce"):
                    key = f"{m['folder']}#{m['uid']}"
                    if key not in seen:
                        todo.append((t, m, key))
        todo.sort(key=lambda x: x[1]["ts"])
        total = len(todo)
        if limit:
            todo = todo[:limit]
        for t, m, key in todo:
            try:
                self._process_inbound(t, m, key)
            except Exception as exc:  # noqa: BLE001
                store.log("inbound_error", f"{t['addr']}: {exc}", t["id"], level="error")
                store.record_inbound(key, t["id"], m["who"]["addr"], m["ts"], m["kind"], category="error")
        return total - len(todo)  # how many are still waiting

    def _process_inbound(self, t, m, key):
        if m["kind"] == "bounce":
            store.record_inbound(key, t["id"], m["who"]["addr"], m["ts"], "bounce")
            store.log("bounce", f"bounce from {m['who']['addr']}", t["id"], level="warn")
            return
        text = mail.fetch_text(m["folder"], m["uid"])
        c = classify_reply(m["subject"], text, m["who"]["addr"])
        store.record_inbound(key, t["id"], m["who"]["addr"], m["ts"], "reply", c["category"], c["platform"],
                             c["platform_url"], None, c["excerpt"])
        replies = [x for x in t["msgs"] if x["dir"] == "in" and x["kind"] == "reply"]
        later = any(x["ts"] > m["ts"] for x in t["msgs"] if x["dir"] == "out")
        existing = store.get_client(t["id"])
        fields = {
            "addr": t["addr"], "name": t["name"], "domain": t["domain"], "subject": t["subject"],
            "category": c["category"], "last_reply_ts": m["ts"], "last_reply_key": key,
            "last_reply_excerpt": c["excerpt"],
        }
        # Once a sample went out the conversation lives in researcher@; don't move the stage back.
        if not existing or existing["stage"] not in ("sample_sent", "in_talks", "closed", "stopped"):
            fields["stage"] = c["stage"]
        # A second or later reply always deserves a look; so does anything we never answered.
        first = len(replies) < 2
        fields["needs_reply"] = 0 if (later or (first and c["category"] in ("stop", "ack", "program", "go_ahead"))) else 1
        if c["draft_template"]:
            fields["draft_template"] = c["draft_template"]
        if c["platform_url"] and not (existing and existing.get("platform_url")):
            fields["platform_url"] = c["platform_url"]
        store.upsert_client(t["id"], **fields)
        store.log("reply", f"{c['category']} reply from {m['who']['addr']}", t["id"],
                  data={"category": c["category"], "needs_reply": fields["needs_reply"]})

    # -- sending ----------------------------------------------------------
    def try_send(self, threads, settings, now):
        """Send at most one due follow-up if every gate allows it. Returns a status dict."""
        done = store.sent_steps(("sent",)) | (store.sent_steps(("dry",)) if settings.get("dry_run") else set())
        suppressed = store.suppress_set()
        queue, skipped = compute_queue(threads, settings, suppressed, done, now)
        day0 = day_start_ts(settings, now)
        sent_today = store.sends_since(day0, ("sent",) if not settings.get("dry_run") else ("sent", "dry"))
        last_follower = store.last_send_ts(("sent",) if not settings.get("dry_run") else ("sent", "dry"))
        newest_sent = 0
        if not settings.get("dry_run"):
            try:
                with mail.mb.lock:
                    conn = mail.mb.get()
                    newest_sent = mail.newest_sent_ts(conn, mail.folder_names(conn)["sent"])
            except Exception as exc:  # noqa: BLE001
                store.log("imap_error", f"could not read Sent: {exc}", level="error")
        allowed, reason, retry_at = send_gate(settings, now, sent_today, max(last_follower, newest_sent),
                                              self.state["errors_in_row"], self.state["last_error_ts"])
        status = {"queue": len(queue), "skipped": skipped, "sent_today": sent_today,
                  "gate": reason, "retry_at": retry_at, "next": None}
        if queue:
            c = queue[0]
            status["next"] = {"addr": c["thread"]["addr"], "step": c["step"], "subject": c["thread"]["subject"]}
        if not allowed or not queue:
            return status
        c = queue[0]
        # Rule 6: look again right before sending — a reply or a manual send may have just arrived.
        fresh = next((t for t in self.refresh() if t["id"] == c["thread"]["id"]), None)
        if not fresh or fresh["replies"] or fresh["stage"] in ("replied", "bounced") or fresh["steps"] + 1 != c["step"]:
            store.log("skip", f"{c['thread']['addr']}: changed just before sending", c["thread"]["id"])
            return status
        result = sender.send_followup(fresh, c["step"], settings, bool(settings.get("dry_run")))
        if result["status"] == "error":
            self.state["errors_in_row"] += 1
            self.state["last_error_ts"] = now
            store.log("send_error", f"{fresh['addr']} step {c['step']}: {result['error']}", fresh["id"], level="error")
            if self.state["errors_in_row"] >= ERRORS_BEFORE_PAUSE:
                store.set_setting("paused", True)
                store.log("auto_pause", f"paused after {ERRORS_BEFORE_PAUSE} consecutive send errors", level="error")
        else:
            self.state["errors_in_row"] = 0
            verb = "would send" if result["status"] == "dry" else "sent"
            store.log("send" if result["status"] == "sent" else "dry_send",
                      f"{verb} follow-up {c['step'] - 1} to {fresh['addr']}", fresh["id"],
                      data={"subject": result["subject"], "from": result["from"]})
        status["sent_today"] = sent_today + (1 if result["status"] != "error" else 0)
        status["last"] = result
        return status

    # -- loop ---------------------------------------------------------------
    def tick(self):
        now = time.time()
        settings = store.get_settings()
        threads = self.refresh()
        if not settings.get("backfilled"):
            remaining = self.ingest_inbound(threads, limit=BACKFILL_BATCH)
            store.set_status(backfill_remaining=remaining)
            if remaining == 0:
                store.set_setting("backfilled", True)
                store.log("backfill_done", "historical replies classified")
        else:
            self.ingest_inbound(threads)
        self.sync_reports(threads)
        self.bounce_guard(settings, now)
        self.daily_tasks(now)
        st = self.try_send(threads, settings, now)
        store.set_status(
            last_tick=now, ok=True, error="", queue=st["queue"], skipped=st["skipped"], sent_today=st["sent_today"],
            gate=st["gate"], retry_at=st["retry_at"], next=st["next"], threads=len(threads),
            unmatched=len(self.state["unmatched"]), dry_run=bool(settings.get("dry_run")),
            researcher="off" if mail.researcher is None else (self.state.get("researcher_error") or "ok"),
            paused=bool(settings.get("paused")), window_open=window_open(now, settings.get("window") or {}),
        )

    def bounce_guard(self, settings, now):
        """Auto-pause live sending if recent follow-ups are bouncing too often."""
        if settings.get("paused") or settings.get("dry_run") or not settings.get("bounce_guard", True):
            return
        cutoff = now - BOUNCE_WINDOW
        sent = store.sends_since(cutoff, ("sent",))
        if sent < BOUNCE_MIN_SENDS:
            return
        bounces = store.bounces_since(cutoff)
        if bounces / sent >= BOUNCE_RATE_PAUSE:
            store.set_setting("paused", True)
            store.log("auto_pause", f"paused: {bounces} bounces against {sent} sends in 24h "
                      f"({bounces / sent:.0%} >= {BOUNCE_RATE_PAUSE:.0%})", level="error")

    def daily_tasks(self, now):
        """Once per calendar day: back up the database and log a digest of what needs attention."""
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self.state.get("daily_day") == today:
            return
        self.state["daily_day"] = today
        try:
            path = store.backup()
            store.log("backup", f"database backed up to {os.path.basename(path)}")
        except Exception as exc:  # noqa: BLE001
            store.log("backup_error", f"{exc}", level="error")
        day = now - 24 * 3600
        counts = store.client_counts()["stages"]
        nr = sum(v["needs_reply"] for v in counts.values())
        with store.db() as conn:
            rr = conn.execute("SELECT COUNT(*) AS n FROM clients WHERE needs_reply_researcher = 1").fetchone()["n"]
            sent24 = store.sends_since(day, ("sent",))
        store.log("digest", f"last 24h: {sent24} follow-ups sent, {store.bounces_since(day)} bounces · "
                  f"waiting on you: {nr} replies, {rr} on researcher@, {counts.get('sample', {}).get('n', 0)} samples to send")

    def run(self):
        store.log("scheduler", "started")
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                store.set_status(last_tick=time.time(), ok=False, error=str(exc))
                store.log("tick_error", f"{exc}", level="error", data={"trace": traceback.format_exc()[-2000:]})
            self.stop_event.wait(TICK_SECONDS)


scheduler = None


def start():
    global scheduler
    if scheduler is None or not scheduler.is_alive():
        store.init()
        scheduler = Scheduler()
        scheduler.start()
    return scheduler
