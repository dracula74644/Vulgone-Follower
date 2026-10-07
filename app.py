"""Vulgone Follower - follow-up automation and client pipeline for the Zoho outreach mailbox.

Run:  python3 app.py   ->  http://127.0.0.1:5055   (or ./startup.sh for Docker)
Credentials and SMTP/IMAP hosts come from .env. The mailbox is only ever read; the only writes
are the follow-ups and human-approved replies sent through the SMTP relay (see sender.py).
"""
import base64
import csv
import io
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta

from flask import Flask, Response, abort, jsonify, request, send_from_directory

import automation
import mail
import sender
import store
import threads as th
from mail import (OWN_DOMAIN, SENDER_NAME, USER, addrs, build_criteria, collect, dec, fetch_summaries,
                  folder_names, leaf_parts, list_folders, mb, parse_message, part_text, q, since_from_days,
                  uid_search, with_attachments)

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
store.init()

STAGE_LABELS = {
    "sent": "Initial email only, no reply", "followup1": "1st follow-up sent, no reply",
    "followup2": "2nd follow-up sent, no reply (dropped)", "replied": "Replied", "bounced": "Bounced",
}
# The client journey, in order: they answered -> we send a sample from researcher@ -> the talk continues there.
CLIENT_STAGES = [
    ("answer", "Answer them"), ("sample", "Send a sample"), ("sample_sent", "Sample sent"),
    ("in_talks", "In conversation (researcher@)"), ("waiting", "Waiting on them"),
    ("program", "Submit via their program"), ("confirm_stop", "Confirm do-not-contact"),
    ("closed", "Closed"), ("stopped", "Stopped"),
]
SETTING_KEYS = {"paused", "dry_run", "wait_days", "window", "daily_cap", "min_gap_seconds", "queue_order",
                "sender_mode", "fixed_sender", "fixed_sender_name", "backlog_since", "bounce_guard"}


def current_threads(max_age=90):
    """The scheduler's thread model if it is fresh, else rebuild now."""
    s = automation.scheduler
    if s is not None:
        threads, unmatched = s.threads()
        if threads and time.time() - s.state["built_at"] < max_age:
            return threads, unmatched
        threads = s.refresh()
        return threads, s.threads()[1]
    settings = store.get_settings()
    with mb.lock:
        conn = mb.get()
        names = folder_names(conn)
        sent = collect(conn, [names["sent"]], None)
        received = collect(conn, names["reply"], None)
    unmatched = []
    built = [th.finish_thread(t, settings.get("wait_days") or [7, 7], time.time())
             for t in th.build_threads(sent, received, OWN_DOMAIN, unmatched)]
    th.mark_domain_engagement(built)
    th.attach_researcher(built, [], [], OWN_DOMAIN)
    return built, unmatched


def find_thread(thread_id):
    threads, _ = current_threads()
    return next((t for t in threads if t["id"] == thread_id), None)


def thread_summary(t):
    """A thread without its message list (for list views)."""
    return {k: v for k, v in t.items() if k != "msgs"} | {"msg_count": len(t["msgs"])}


# ---------------------------------------------------------------- errors / static

@app.errorhandler(Exception)
def on_error(exc):
    code = getattr(exc, "code", 500)
    if not isinstance(code, int):
        code = 500
    if isinstance(exc, LookupError):
        code = 404
    return jsonify(error=str(exc)), code


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/account")
def account():
    return jsonify(user=USER, name=SENDER_NAME, host=mail.HOST, smtp=mail.SMTP_HOST,
                   researcher=mail.RESEARCHER_USER or None)


# ---------------------------------------------------------------- automation: status & control

@app.get("/api/status")
def status():
    settings = store.get_settings()
    st = store.get_status()
    now = time.time()
    window = settings.get("window") or {}
    nxt = automation.next_window_start(now, window)
    counts = store.client_counts()
    needs_reply = sum(v["needs_reply"] for v in counts["stages"].values())
    week_ago = now - 7 * 86400
    with store.db() as conn:
        replies_7d = conn.execute("SELECT COUNT(*) AS n FROM inbound WHERE kind = 'reply' AND ts >= ?", (week_ago,)).fetchone()["n"]
        confirm_stop = conn.execute("SELECT COUNT(*) AS n FROM clients WHERE stage = 'confirm_stop'").fetchone()["n"]
    return jsonify(
        now=now, paused=bool(settings.get("paused")), dry_run=bool(settings.get("dry_run")),
        running=bool(automation.scheduler and automation.scheduler.is_alive()),
        window=window, window_open=automation.window_open(now, window),
        next_window_start=nxt.timestamp() if nxt else None,
        daily_cap=settings.get("daily_cap"), min_gap_seconds=settings.get("min_gap_seconds"),
        wait_days=settings.get("wait_days"), backfilled=bool(settings.get("backfilled")),
        status=st, needs_reply=needs_reply, replies_7d=replies_7d, confirm_stop=confirm_stop,
        stages=counts["stages"],
    )


@app.post("/api/control")
def control():
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action == "pause":
        store.set_setting("paused", True); store.log("control", "paused by user")
    elif action == "resume":
        store.set_setting("paused", False)
        if automation.scheduler:
            automation.scheduler.state["errors_in_row"] = 0
        store.log("control", "resumed by user")
    elif action == "go_live":
        if body.get("confirm") != "LIVE":
            abort(400, "confirm with LIVE")
        store.set_setting("dry_run", False); store.log("control", "switched to LIVE sending", level="warn")
    elif action == "dry_run":
        store.set_setting("dry_run", True); store.log("control", "switched to dry run")
    elif action == "test_email":
        mid = sender.send_test(store.get_settings(), body.get("to"))
        return jsonify(ok=True, message_id=mid)
    elif action == "refresh":
        if automation.scheduler:
            automation.scheduler.refresh()
    elif action == "mark_old_handled":
        days = int(body.get("days", 30))
        n = store.mark_handled_older_than(time.time() - days * 86400)
        store.log("control", f"marked {n} replies older than {days} days as handled")
        return jsonify(ok=True, count=n)
    else:
        abort(400, "unknown action")
    return jsonify(ok=True)


@app.get("/api/queue")
def queue():
    settings = store.get_settings()
    threads, _ = current_threads()
    done = store.sent_steps(("sent",)) | (store.sent_steps(("dry",)) if settings.get("dry_run") else set())
    q_, skipped = automation.compute_queue(threads, settings, store.suppress_set(), done, time.time())
    limit = min(int(request.args.get("limit", 200)), 2000)
    items = [thread_summary(c["thread"]) | {"step": c["step"]} for c in q_[:limit]]
    return jsonify(total=len(q_), items=items, skipped=skipped)


# ---------------------------------------------------------------- replies & pipeline

def _draft_for(client, t):
    tpl_id = client.get("draft_template")
    tpl = store.template(tpl_id) if tpl_id else None
    if not tpl:
        return ""
    return sender.render(tpl["body"], sender.template_context(t))


@app.get("/api/replies")
def replies():
    threads, unmatched = current_threads()
    by_id = {t["id"]: t for t in threads}
    items = []
    for c in store.clients(needs_reply=True):
        t = by_id.get(c["thread_id"])
        items.append({"client": c, "thread": thread_summary(t) if t else None,
                      "draft": _draft_for(c, t) if t else ""})
    pending_stop = [{"client": c, "thread": thread_summary(by_id[c["thread_id"]]) if c["thread_id"] in by_id else None}
                    for c in store.clients(stage="confirm_stop")]
    researcher = [{"client": c, "thread": thread_summary(by_id[c["thread_id"]]) if c["thread_id"] in by_id else None}
                  for c in store.clients(needs_reply_researcher=True)]
    un = sorted(unmatched, key=lambda m: -m["ts"])[:100]
    return jsonify(total=len(items), items=items, confirm_stop=pending_stop, researcher=researcher,
                   unmatched=[{"folder": m["folder"], "uid": m["uid"], "from": m["from"], "subject": m["subject"],
                               "date": m["date"], "ts": m["ts"]} for m in un])


@app.post("/api/replies/send")
def reply_send():
    body = request.get_json(silent=True) or {}
    t = find_thread(body.get("id", ""))
    text = (body.get("text") or "").strip()
    if not t or not text:
        abort(400, "thread id and text required")
    settings = store.get_settings()
    gap = int(settings.get("min_gap_seconds") or 120)
    with mb.lock:
        conn = mb.get()
        newest = mail.newest_sent_ts(conn, folder_names(conn)["sent"])
    last = max(newest, store.last_send_ts(("sent",)))
    wait = gap - (time.time() - last)
    if wait > 0:
        return jsonify(error=f"rate limit: wait {int(wait)} s", retry_in=int(wait)), 429
    result = sender.send_reply(t, text, settings, dry_run=False)
    if result["status"] != "sent":
        return jsonify(error=result.get("error", "send failed")), 502
    store.upsert_client(t["id"], needs_reply=0, stage=body.get("stage") or None) if body.get("stage") \
        else store.upsert_client(t["id"], needs_reply=0)
    store.log("reply_sent", f"answered {t['addr']}", t["id"], data={"subject": result["subject"]})
    return jsonify(ok=True, message_id=result["message_id"])


@app.post("/api/replies/handle")
def reply_handle():
    body = request.get_json(silent=True) or {}
    thread_id = body.get("id", "")
    if not store.get_client(thread_id):
        abort(404, "unknown client")
    fields = {"needs_reply_researcher": 0} if body.get("researcher") else {"needs_reply": 0}
    if body.get("stage"):
        fields["stage"] = body["stage"]
    store.upsert_client(thread_id, **fields)
    return jsonify(ok=True)


@app.post("/api/clients/confirm_stop")
def confirm_stop():
    body = request.get_json(silent=True) or {}
    c = store.get_client(body.get("id", ""))
    if not c:
        abort(404, "unknown client")
    if body.get("decision") == "block":
        store.add_suppress(c["addr"], "asked us to stop", source="reply")
        if c["domain"] not in th.GENERIC_DOMAINS:
            store.add_suppress("@" + c["domain"], "asked us to stop", source="reply")
        store.upsert_client(c["thread_id"], stage="stopped", do_not_contact=1, needs_reply=0)
        store.log("suppress", f"blocked {c['addr']} and @{c['domain']}", c["thread_id"], level="warn")
    else:
        store.upsert_client(c["thread_id"], stage="review", needs_reply=1)
    return jsonify(ok=True)


@app.get("/api/clients")
def clients():
    stage = request.args.get("stage") or None
    rows = store.clients(stage=stage)
    return jsonify(items=rows, counts=store.client_counts(), stages=CLIENT_STAGES)


@app.post("/api/clients/update")
def client_update():
    body = request.get_json(silent=True) or {}
    thread_id = body.pop("id", "")
    if not store.get_client(thread_id):
        t = find_thread(thread_id)
        if not t:
            abort(404, "unknown client")
        store.upsert_client(thread_id, addr=t["addr"], name=t["name"], domain=t["domain"], subject=t["subject"],
                            stage=body.get("stage") or "review")
    allowed = {"stage", "notes", "do_not_contact", "platform_url", "needs_reply", "needs_reply_researcher"}
    fields = {k: v for k, v in body.items() if k in allowed}
    store.upsert_client(thread_id, **fields)
    return jsonify(ok=True, client=store.get_client(thread_id))


@app.get("/api/thread")
def thread():
    t = find_thread(request.args.get("id", ""))
    if not t:
        abort(404, "unknown thread")
    return jsonify(thread=t, client=store.get_client(t["id"]))


# ---------------------------------------------------------------- settings, templates, suppress, activity

@app.get("/api/settings")
def settings_get():
    s = store.get_settings()
    return jsonify({k: s.get(k) for k in SETTING_KEYS})


@app.put("/api/settings")
def settings_put():
    body = request.get_json(silent=True) or {}
    for k, v in body.items():
        if k not in SETTING_KEYS:
            continue
        if k == "wait_days":
            v = [max(0, int(v[0])), max(0, int(v[1]))]
        elif k in ("daily_cap", "min_gap_seconds"):
            v = max(0, int(v))
            if k == "min_gap_seconds":
                v = max(v, 120)  # the Zoho pace is a hard floor
        elif k == "window":
            v = {"days": [int(d) for d in v.get("days", [0, 1, 2, 3, 4])], "start": v.get("start", "13:00"),
                 "end": v.get("end", "03:00"), "tz": v.get("tz", "Asia/Kolkata")}
        elif k in ("paused", "dry_run", "bounce_guard"):
            v = bool(v)
        elif k == "fixed_sender" and v and not str(v).lower().endswith("@" + OWN_DOMAIN):
            abort(400, "sender must be on " + OWN_DOMAIN)
        store.set_setting(k, v)
    store.log("settings", "settings changed", data={k: body[k] for k in body if k in SETTING_KEYS})
    return settings_get()


@app.get("/api/templates")
def templates_get():
    return jsonify(items=store.templates())


@app.put("/api/templates")
def templates_put():
    body = request.get_json(silent=True) or {}
    if not store.template(body.get("id", "")):
        abort(404, "unknown template")
    store.save_template(body["id"], body.get("body", ""), body.get("name"))
    store.log("template", f"template {body['id']} edited")
    return jsonify(ok=True)


@app.get("/api/suppress.csv")
def suppress_csv():
    """The do-not-contact list for the outreach platform: one address or @domain per row."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["address_or_domain", "reason", "source", "added"])
    for x in store.suppress_list():
        w.writerow([x["addr"], x["reason"] or "", x["source"], datetime.fromtimestamp(x["created_at"]).strftime("%Y-%m-%d")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=do-not-contact.csv"})


@app.get("/api/suppress")
def suppress_get():
    return jsonify(items=store.suppress_list())


@app.post("/api/suppress")
def suppress_post():
    body = request.get_json(silent=True) or {}
    addr = (body.get("addr") or "").strip().lower()
    if not addr or ("@" not in addr):
        abort(400, "address or @domain required")
    store.add_suppress(addr, body.get("reason") or "manual")
    return jsonify(ok=True)


@app.delete("/api/suppress")
def suppress_delete():
    store.remove_suppress((request.args.get("addr") or "").strip().lower())
    return jsonify(ok=True)


@app.get("/api/activity")
def activity():
    since = request.args.get("since", type=float)
    return jsonify(items=store.activity(limit=int(request.args.get("limit", 200)), since=since))


# ---------------------------------------------------------------- sequence (outreach) views

def outreach_query(args):
    """Filter and sort outreach threads from the request args (shared by JSON and CSV)."""
    days = int(args.get("days", 0))
    stage = args.get("stage", "")
    if stage == "dropped":  # older links: dropped is now part of the 2nd follow-up stage
        stage = "followup2"
    campaign = args.get("campaign", "")
    due_only = args.get("due") == "1"
    turn = args.get("turn", "")
    since = args.get("since", "")
    before = args.get("before", "")
    text = (args.get("q") or "").strip().lower()
    frm = (args.get("from") or "").strip().lower()
    sort = args.get("sort", "activity")

    threads, _ = current_threads()
    now = time.time()
    cutoff = now - days * 86400 if days else 0
    threads = [t for t in threads if t["first_ts"] >= cutoff]

    titles = {}
    for t in threads:
        titles.setdefault(t["campaign"], t["subject"])
    campaign_list = [
        {"key": k, "subject": titles[k], "count": n}
        for k, n in Counter(t["campaign"] for t in threads).most_common(40)
    ]

    if campaign:
        threads = [t for t in threads if t["campaign"] == campaign]
    if frm:
        threads = [t for t in threads if frm in t["addr"] or frm in t["name"].lower()]
    if text:
        threads = [t for t in threads if text in t["addr"] or text in t["name"].lower()
                   or text in t["subject"].lower()]
    if since:
        lo = datetime.fromisoformat(since).timestamp()
        threads = [t for t in threads if t["last_ts"] >= lo]
    if before:
        hi = (datetime.fromisoformat(before) + timedelta(days=1)).timestamp()
        threads = [t for t in threads if t["last_ts"] < hi]

    counts = Counter(t["stage"] for t in threads)
    counts["due"] = sum(1 for t in threads if t["due"])
    counts["needs_reply"] = sum(1 for t in threads if t["turn"] == "us")
    counts["all"] = len(threads)

    if stage:
        threads = [t for t in threads if t["stage"] == stage]
    if turn:
        threads = [t for t in threads if t["turn"] == turn]
    if due_only:
        threads = [t for t in threads if t["due"]]
    threads.sort(key=th.SORTS.get(sort, th.SORTS["activity"]))
    return threads, counts, campaign_list, days


@app.get("/api/outreach")
def outreach():
    """Every client we emailed, grouped into threads with their follow-up stage."""
    offset = max(int(request.args.get("offset", 0)), 0)
    limit = min(max(int(request.args.get("limit", 100)), 1), 1000)
    threads, counts, campaign_list, days = outreach_query(request.args)
    return jsonify(
        total=len(threads), offset=offset, limit=limit, items=threads[offset:offset + limit],
        counts=counts, campaigns=campaign_list, days=days,
    )


@app.get("/api/outreach.csv")
def outreach_csv():
    """The same list as /api/outreach, one row per client, for spreadsheets and mailers."""
    threads, _, _, _ = outreach_query(request.args)
    fmt = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""  # noqa: E731
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "name", "domain", "subject", "stage", "follow_up_due", "needs_reply",
                "emails_sent", "sequence_step", "replies", "first_sent", "last_sent",
                "last_reply", "days_since_last_email"])
    for t in threads:
        w.writerow([
            t["addr"], t["name"], t["domain"], t["subject"], STAGE_LABELS.get(t["stage"], t["stage"]),
            "yes" if t["due"] else "", "yes" if t["turn"] == "us" else "",
            t["sends"], t["steps"], t["replies"], fmt(t["first_ts"]), fmt(t["last_out_ts"]),
            fmt(t["last_in_ts"]) if t["replies"] else "", int(t["waiting_days"]),
        ])
    name = "outreach" + (f"-{request.args['stage']}" if request.args.get("stage") else "") + ".csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


# ---------------------------------------------------------------- plain mailbox views

@app.get("/api/folders")
def folders():
    with mb.lock:
        conn = mb.get()
        result = []
        for f in list_folders(conn):
            typ, data = conn.status(q(f["name"]), "(MESSAGES UNSEEN)")
            text = data[0].decode(errors="replace") if typ == "OK" and data and data[0] else ""
            m = re.search(r"MESSAGES (\d+)", text)
            u = re.search(r"UNSEEN (\d+)", text)
            f["total"] = int(m.group(1)) if m else 0
            f["unseen"] = int(u.group(1)) if u else 0
            result.append(f)
    order = {"inbox": 0, "": 1, "sent": 2, "drafts": 3, "templates": 4, "spam": 5, "trash": 6}
    result.sort(key=lambda f: (
        0 if f["name"].upper().startswith("INBOX") else order.get(f["role"], 1) + 1,
        f["name"].lower(),
    ))
    return jsonify(folders=result)


@app.get("/api/messages")
def messages():
    folder = request.args.get("folder", "INBOX")
    offset = max(int(request.args.get("offset", 0)), 0)
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    crit, literal = build_criteria(request.args)
    with mb.lock:
        conn = mb.get(folder)
        uids = uid_search(conn, crit, literal)
        if request.args.get("attach") == "1" and uids:
            uids = with_attachments(conn, uids)
        uids.sort(key=int, reverse=True)
        page = uids[offset:offset + limit]
        items = fetch_summaries(conn, page, folder) if page else []
    items.sort(key=lambda m: m["ts"], reverse=True)
    return jsonify(total=len(uids), offset=offset, limit=limit, items=items)


@app.get("/api/unreplied")
def unreplied():
    """Received mail with no reply from us (flag, thread headers, or later mail to sender)."""
    days = int(request.args.get("days", 30))
    hide_auto = request.args.get("hide_auto", "1") == "1"
    since = since_from_days(days)
    with mb.lock:
        conn = mb.get()
        names = folder_names(conn)
        wanted = request.args.get("folders")
        scan = wanted.split(",") if wanted else [f["name"] for f in names["all"] if f["name"].upper().startswith("INBOX")]
        inbox = collect(conn, scan, since)
        sent = collect(conn, [names["sent"]], since)

    replied_ids, last_sent_to = set(), Counter()
    for s in sent:
        replied_ids.update(s["in_reply_to"] + s["references"])
        for a in s["to"] + s["cc"]:
            last_sent_to[a["addr"]] = max(last_sent_to[a["addr"]], s["ts"])

    items = []
    for m in inbox:
        if m["answered"] or m["from"]["addr"] == USER:
            continue
        if m["message_id"] and m["message_id"] in replied_ids:
            continue
        if last_sent_to.get(m["from"]["addr"], 0) > m["ts"]:
            continue
        if hide_auto and m["automated"]:
            continue
        items.append(m)
    items.sort(key=lambda m: m["ts"], reverse=True)
    return jsonify(total=len(items), items=items, scanned=scan, days=days)


@app.get("/api/message")
def message():
    folder = request.args.get("folder", "INBOX")
    uid = request.args.get("uid", "")
    meta, raw = mail.fetch_raw(folder, uid, request.args.get("account", ""))
    msg = parse_message(raw)

    html = text = None
    attachments, cids = [], {}
    for idx, part in enumerate(leaf_parts(msg)):
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
        filename = dec(part.get_filename())
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if cid and ctype.startswith("image/"):
            data = base64.b64encode(part.get_payload(decode=True) or b"").decode()
            cids[cid] = f"data:{ctype};base64,{data}"
        if filename or disp == "attachment":
            attachments.append({
                "idx": idx,
                "filename": filename or f"attachment-{idx}",
                "type": ctype,
                "size": len(part.get_payload(decode=True) or b""),
                "inline": bool(cid) and disp != "attachment",
            })
        elif ctype == "text/html" and html is None:
            html = part_text(part)
        elif ctype == "text/plain" and text is None:
            text = part_text(part)

    if html and cids:
        html = re.sub(r"cid:([^\"'\s)>]+)", lambda m: cids.get(m.group(1), m.group(0)), html)

    flags = re.search(r"FLAGS \(([^)]*)\)", meta)
    head_end = raw.find(b"\r\n\r\n")
    raw_headers = raw[: head_end if head_end > 0 else 4000].decode(errors="replace")
    return jsonify(
        uid=uid, folder=folder, flags=flags.group(1).split() if flags else [],
        subject=dec(msg.get("Subject")) or "(no subject)",
        **{"from": addrs(msg.get("From"))},
        to=addrs(msg.get("To")), cc=addrs(msg.get("Cc")), reply_to=addrs(msg.get("Reply-To")),
        date=dec(msg.get("Date")), message_id=dec(msg.get("Message-ID")),
        html=html, text=text, attachments=attachments, raw_headers=raw_headers,
    )


@app.get("/api/attachment")
def attachment():
    folder = request.args.get("folder", "INBOX")
    uid = request.args.get("uid", "")
    idx = int(request.args.get("idx", -1))
    _, raw = mail.fetch_raw(folder, uid, request.args.get("account", ""))
    parts = leaf_parts(parse_message(raw))
    if not 0 <= idx < len(parts):
        abort(404)
    part = parts[idx]
    name = dec(part.get_filename()) or f"attachment-{idx}"
    from urllib.parse import quote
    return Response(
        part.get_payload(decode=True) or b"",
        mimetype=part.get_content_type(),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@app.get("/api/raw")
def raw_message():
    folder = request.args.get("folder", "INBOX")
    uid = request.args.get("uid", "")
    _, raw = mail.fetch_raw(folder, uid, request.args.get("account", ""))
    return Response(raw, mimetype="message/rfc822",
                    headers={"Content-Disposition": f"attachment; filename=message-{uid}.eml"})


if os.environ.get("AUTOMATION", "1") == "1":
    automation.start()


if __name__ == "__main__":
    # Bound to localhost by default: the portal has no login of its own.
    app.run(host=os.environ.get("BIND_HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5055)), threaded=True)
