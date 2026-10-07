"""Outreach thread reconstruction: group our sent mail per client and attach what came back.

Pure functions over the cached header summaries (see app.sync_folder); no network, no Flask.
"""
import re
from collections import defaultdict

# Two sends closer together than this are one step of the sequence (a resend, not a follow-up).
RESEND_WINDOW = 24 * 3600
# A fresh email with the same subject this long after a thread went quiet is a new outreach, not a step.
RECONTACT_GAP = 60 * 86400

GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "proton.me", "protonmail.com", "live.com", "aol.com", "me.com", "msn.com", "yandex.com",
    "zoho.com", "zohomail.in", "zohomail.com", "mail.com", "gmx.com", "gmx.de", "pm.me",
}

# The team's own test inboxes and personas: never clients, and their mail is "ours".
INTERNAL_ADDRS = {
    "scopesync.inquiry@gmail.com", "scopesync.researcher@gmail.com",
    "zeeshanbeg2000@gmail.com", "zeeshanbeg2001@gmail.com", "zeeshanbeg2002@gmail.com",
    "zeeshanbeg2003@gmail.com", "zeeshanbeg2004@gmail.com", "zeeshanmirzav2004@gmail.com",
    "privacybeg2004@gmail.com", "negis7650@gmail.com", "testertester7650@gmail.com",
    "testertester7651@gmail.com", "ramshakhanz2004@gmail.com", "mirzasahab204@gmail.com",
    "binbin@gmail.com", "binbinbin@gmail.com", "sudosu001@wearehackerone.com",
    "jpap1iv3@parser.zohocrm.in", "dracula74644@wearehackerone.com",
}
INTERNAL_RE = re.compile(r"^scopesync\.|^zeeshanbeg20\d\d@|^testertester\d*@", re.I)

AUTOMATED_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounces?|notifications?|alerts?"
    r"|\w*dmarc\w*|reports?)\b",
    re.I,
)
BOUNCE_FROM_RE = re.compile(r"^(mailer-daemon|mail-daemon|postmaster|bounces?)\b", re.I)
BOUNCE_SUBJ_RE = re.compile(
    r"undeliver|delivery (status|failure)|mail delivery failed|failure notice|returned mail"
    r"|could not be delivered|delivery has failed",
    re.I,
)
AUTOREPLY_SUBJ_RE = re.compile(r"^\s*(automatic reply|auto-?reply|autoreply|out of (the )?office)", re.I)
# Helpdesk noise that is not a person answering us.
TICKET_NOISE_RE = re.compile(
    r"how (would you|did we|were we)|rate (the|your|our) (support|service)|feedback|survey"
    r"|ticket (is )?(closed|resolved|updated|reminder)|(request|ticket) received|received your"
    r"|new ticket has been created|any updates\?|following up!|final follow up|auto follow-up"
    r"|pending request|share your|waiting to hear from you|resolving ticket|we.d love",
    re.I,
)


def is_internal(addr, own_domain):
    addr = (addr or "").lower()
    return (addr in INTERNAL_ADDRS or bool(INTERNAL_RE.match(addr))
            or addr.endswith("@" + own_domain))


def norm_subject(subject):
    return re.sub(r"^\s*((re|fwd?|fw)\s*:\s*)+", "", subject or "", flags=re.I).strip().lower()


def real_name(a):
    """Display names like "support" or "pingen" for support@pingen.com add nothing; drop them."""
    name = (a.get("name") or "").strip()
    local, _, domain = a["addr"].partition("@")
    if name.lower() in ("", local.lower(), a["addr"], domain.split(".")[0].lower()):
        return ""
    return name


def source_of(m):
    """Who produced one of our sent messages: the outreach platform, a person, or the follower."""
    mid = m.get("message_id") or ""
    if mid.startswith("<vf-"):
        return "follower"
    if mid.endswith("@zoho.in>"):
        return "platform"
    return "manual"


def classify_incoming(r, by_thread_header, answered, minutes_after_send):
    """bounce / auto / reply for a message received in an outreach thread."""
    if BOUNCE_FROM_RE.match(r["from"]["addr"]) or BOUNCE_SUBJ_RE.search(r["subject"]):
        return "bounce"
    if r["message_id"] and r["message_id"] in answered:
        return "reply"  # we wrote back to it, so a person must have been on the other end
    if AUTOREPLY_SUBJ_RE.search(r["subject"]) or TICKET_NOISE_RE.search(r["subject"]):
        return "auto"
    if r.get("auto_submitted"):
        # Helpdesks stamp agent replies as auto-submitted too; only instant ones are acks.
        return "auto" if minutes_after_send is None or minutes_after_send <= 60 else "reply"
    # Matched only by sender address: don't let newsletters count as replies.
    if not by_thread_header and r["automated"]:
        return "auto"
    return "reply"


def msg_ref(m, direction, kind=""):
    counterpart = m["from"] if direction == "in" else (m["to"] or m["cc"] or [{"name": "", "addr": ""}])[0]
    return {
        "folder": m["folder"], "uid": m["uid"], "dir": direction, "kind": kind,
        "ts": m["ts"], "date": m["date"], "subject": m["subject"], "seen": m["seen"],
        "who": counterpart, "has_attachments": m["has_attachments"],
        "mid": m.get("message_id") or "", "sender": m["from"],
        "source": source_of(m) if direction == "out" else "",
    }


def _as_outgoing(r, own_domain):
    """A mail from one of our own addresses that landed in the inbox (CC'd to us): treat as sent."""
    rcpts = [a for a in r["to"] + r["cc"] if not is_internal(a["addr"], own_domain)]
    if not rcpts:
        return None
    m = dict(r)
    m["to"], m["cc"] = [rcpts[0]], []
    return m


def build_threads(sent, received, own_domain, unmatched=None):
    """Group our sent mail into per-client threads and attach what came back.

    `unmatched`, if given, collects received messages that look like client mail but
    match no thread.
    """
    outgoing = list(sent)
    inbound = []
    for r in received:
        if is_internal(r["from"]["addr"], own_domain):
            m = _as_outgoing(r, own_domain)
            if m:
                outgoing.append(m)
        else:
            inbound.append(r)

    by_addr = defaultdict(list)
    mid_index = {}
    recv_by_mid = {r["message_id"]: r for r in inbound if r["message_id"]}
    claimed = set()  # message-ids of received mail already placed in a thread
    answered = set()  # message-ids our sent mail replied to
    for m in outgoing:
        answered.update(m["in_reply_to"] + m["references"])

    def minutes_since_send(t, ts):
        outs = [x["ts"] for x in t["msgs"] if x["dir"] == "out" and x["ts"] <= ts]
        return (ts - max(outs)) / 60 if outs else None

    for m in sorted(outgoing, key=lambda x: x["ts"]):
        # Internal mail (to ourselves, colleagues or test inboxes) is not outreach.
        rcpts = [a for a in m["to"] + m["cc"] if not is_internal(a["addr"], own_domain)]
        if not rcpts:
            continue
        a = rcpts[0]
        key = norm_subject(m["subject"])
        refs = set(m["in_reply_to"] + m["references"])
        threads = by_addr[a["addr"]]
        last = threads[-1] if threads else None
        recent = last is not None and m["ts"] - max(x["ts"] for x in last["msgs"]) <= RECONTACT_GAP
        if last is not None and ((refs & last["mids"]) or (key == last["key"] and recent)):
            t = last
        else:
            t = next((c for c in threads if refs & c["mids"]), None)
        if t is None:
            t = {"addr": a["addr"], "name": real_name(a), "key": key, "subject": m["subject"],
                 "mids": set(), "msgs": []}
            threads.append(t)
        if not t["name"]:
            t["name"] = real_name(a)
        # A sent mail answering something we received first: pull that message in too.
        for x in refs:
            r = recv_by_mid.get(x)
            if r and x not in claimed:
                claimed.add(x)
                t["msgs"].append(msg_ref(r, "in", classify_incoming(r, True, answered, minutes_since_send(t, r["ts"]))))
        t["msgs"].append(msg_ref(m, "out"))
        if m["message_id"]:
            t["mids"].add(m["message_id"])
            mid_index[m["message_id"]] = t

    by_domain = defaultdict(list)
    for addr, threads in by_addr.items():
        domain = addr.split("@")[-1]
        if domain not in GENERIC_DOMAINS:
            by_domain[domain] += threads
    for threads in by_domain.values():
        threads.sort(key=lambda t: t["msgs"][0]["ts"])

    for r in sorted(inbound, key=lambda x: x["ts"]):
        if r["message_id"] and r["message_id"] in claimed:
            continue
        refs = set(r["in_reply_to"] + r["references"])
        t = next((mid_index[x] for x in refs if x in mid_index), None)
        by_header = t is not None
        if t is None:
            sender = r["from"]["addr"]
            cands = by_addr.get(sender) or by_domain.get(sender.split("@")[-1], [])
            before = [c for c in cands if c["msgs"][0]["ts"] <= r["ts"]]
            t = before[-1] if before else None
        if t is None:
            if unmatched is not None and not r["automated"] and not BOUNCE_FROM_RE.match(r["from"]["addr"]):
                unmatched.append(r)
            continue
        if r["message_id"]:
            claimed.add(r["message_id"])
        t["msgs"].append(msg_ref(r, "in", classify_incoming(r, by_header, answered, minutes_since_send(t, r["ts"]))))

    return [t for threads in by_addr.values() for t in threads]


def finish_thread(t, wait_days, now):
    """Derive stage, step count and timing for one thread. `wait_days` may be an int or (step2, step3)."""
    msgs = sorted(t["msgs"], key=lambda x: x["ts"])
    outs = [x for x in msgs if x["dir"] == "out"]
    ins = [x for x in msgs if x["dir"] == "in"]
    replies = [x for x in ins if x["kind"] == "reply"]
    bounces = [x for x in ins if x["kind"] == "bounce"]
    last_out = outs[-1]["ts"] if outs else msgs[-1]["ts"]
    waiting = (now - last_out) / 86400
    steps = sum(1 for i, x in enumerate(outs) if i == 0 or x["ts"] - outs[i - 1]["ts"] >= RESEND_WINDOW)
    waits = (wait_days, wait_days) if isinstance(wait_days, (int, float)) else tuple(wait_days)
    turn = ""
    if replies:
        stage = "replied"
        turn = "us" if replies[-1]["ts"] > last_out else "them"
    elif bounces:
        stage = "bounced"
    elif steps <= 1:
        stage = "sent"
    elif steps == 2:
        stage = "followup1"
    else:
        stage = "followup2"  # end of the sequence: the client is dropped
    wait_needed = waits[0] if stage == "sent" else waits[1]
    due = stage in ("sent", "followup1") and waiting >= wait_needed
    first_out = outs[0] if outs else None
    return {
        "id": f"{t['addr']}|{t['key']}",
        "addr": t["addr"], "name": t["name"], "domain": t["addr"].split("@")[-1],
        "subject": t["subject"], "campaign": t["key"],
        "stage": stage, "due": due, "turn": turn,
        "inbound": bool(msgs) and msgs[0]["dir"] == "in",
        "sends": len(outs), "steps": steps, "received": len(ins), "replies": len(replies),
        "first_ts": msgs[0]["ts"], "last_ts": msgs[-1]["ts"], "last_out_ts": last_out,
        "last_in_ts": ins[-1]["ts"] if ins else None,
        "waiting_days": round(waiting, 1),
        "initial_from": first_out["sender"] if first_out else None,
        "initial_source": first_out["source"] if first_out else "",
        "out_mids": [x["mid"] for x in outs if x["mid"]],
        "engaged_at_domain": False,
        "msgs": msgs,
    }


def mark_domain_engagement(threads):
    """Flag threads whose company already replied to us through another address/thread."""
    latest_reply = defaultdict(float)
    for t in threads:
        if t["replies"] and t["domain"] not in GENERIC_DOMAINS:
            last = max(x["ts"] for x in t["msgs"] if x["dir"] == "in" and x["kind"] == "reply")
            latest_reply[t["domain"]] = max(latest_reply[t["domain"]], last)
    for t in threads:
        if not t["replies"] and latest_reply.get(t["domain"], 0) > t["first_ts"]:
            t["engaged_at_domain"] = True
    return threads


def attach_researcher(threads, r_sent, r_received, own_domain):
    """Add the researcher mailbox's mail with each client to its thread (reports, payment talk).

    Messages are matched by the client's address, or by company domain for non-generic domains,
    and must be newer than the thread's first email. They do not change the follow-up stage.
    """
    by_addr, by_domain = defaultdict(list), defaultdict(list)
    for t in threads:
        t["researcher"], t["report_sent_ts"], t["report_count"] = [], None, 0
        by_addr[t["addr"]].append(t)
        if t["domain"] not in GENERIC_DOMAINS:
            by_domain[t["domain"]].append(t)

    def target(addr, ts):
        cands = by_addr.get(addr) or by_domain.get(addr.split("@")[-1], [])
        before = [c for c in cands if c["first_ts"] <= ts]
        return before[-1] if before else None

    for m in r_sent:
        for a in m["to"] + m["cc"]:
            if is_internal(a["addr"], own_domain):
                continue
            t = target(a["addr"], m["ts"])
            if t is not None:
                t["researcher"].append(msg_ref(m, "out") | {"account": "researcher"})
                break
    for m in r_received:
        if is_internal(m["from"]["addr"], own_domain):
            continue
        t = target(m["from"]["addr"], m["ts"])
        if t is not None:
            kind = "bounce" if BOUNCE_FROM_RE.match(m["from"]["addr"]) or BOUNCE_SUBJ_RE.search(m["subject"]) else ("auto" if m["automated"] else "reply")
            t["researcher"].append(msg_ref(m, "in", kind) | {"account": "researcher"})

    for t in threads:
        if not t["researcher"]:
            continue
        t["researcher"].sort(key=lambda x: x["ts"])
        outs = [x for x in t["researcher"] if x["dir"] == "out"]
        if outs:
            t["report_sent_ts"], t["report_count"] = outs[0]["ts"], len(outs)
    return threads


SORTS = {
    "activity": lambda t: -t["last_ts"],
    "waiting": lambda t: t["last_out_ts"],
    "sends": lambda t: (-t["sends"], -t["last_ts"]),
    "client": lambda t: (t["addr"], -t["last_ts"]),
}
