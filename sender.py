"""Compose and send threaded replies (follow-ups and human-approved answers) over Zoho SMTP.

A follow-up is a plain-text reply in the existing thread: same subject with "Re:", In-Reply-To
and References pointing at our previous messages, the previous message quoted below the text —
exactly how the manual follow-ups were sent. Dry-run renders everything and sends nothing.
"""
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, parsedate_to_datetime

import mail
import store
import threads as th

PERSONA_RE = re.compile(
    r"(?:I'?m|I am|this is)\s+([A-Z][a-z]+)(?:\s+[A-Z][a-z]+)?\s+(?:here\s+)?from\s+Vulgone"
    r"|([A-Z][a-z]+)\s+here\s+from\s+Vulgone",
)
SIGNOFF_RE = re.compile(r"(?:Regards|Thanks|Best regards|Best|Cheers),?\s*\n+\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*(?:\n|$)")


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(body, ctx):
    """Fill {company}, {name}, {persona}, {persona_first} and the old [Company/Team Name] token."""
    body = body.replace("[Company/Team Name]", "{company}")
    return body.format_map(_SafeDict(ctx))


def persona_from_text(text):
    m = PERSONA_RE.search(text or "")
    if m:
        return m.group(1) or m.group(2) or ""
    m = SIGNOFF_RE.search(text or "")
    return m.group(1) if m else ""


def company_from(thread):
    """Company name for templates: the '- X |' token of the subject, else the domain's first label."""
    m = re.search(r"-\s*([^-|]+?)\s*(?:-|\|)", thread["subject"] or "")
    token = m.group(1).strip() if m else ""
    if token and token.lower() not in ("gmail", "googlemail", "yahoo", "outlook", "hotmail"):
        return token
    label = thread["domain"].split(".")[0]
    return label.capitalize() if thread["domain"] not in th.GENERIC_DOMAINS else "there"


def choose_sender(thread, settings):
    """(address, display name) the reply goes out from."""
    if settings.get("sender_mode") == "fixed" and settings.get("fixed_sender"):
        return settings["fixed_sender"].lower(), settings.get("fixed_sender_name") or ""
    initial = thread.get("initial_from") or {}
    addr = (initial.get("addr") or mail.USER).lower()
    if not addr.endswith("@" + mail.OWN_DOMAIN):
        addr = mail.USER
    name = initial.get("name") or (mail.SENDER_NAME if addr == mail.USER else "")
    return addr, name


def previous_message(thread):
    """Raw parsed message + text of the last outgoing message in the thread (to quote and to thread on)."""
    outs = [m for m in thread["msgs"] if m["dir"] == "out"]
    if not outs:
        return None, ""
    last = outs[-1]
    try:
        _, raw = mail.fetch_raw(last["folder"], last["uid"])
    except LookupError:
        return None, ""
    msg = mail.parse_message(raw)
    return msg, mail.message_text(msg)


def quote(prev_msg, prev_text):
    if prev_msg is None:
        return ""
    try:
        when = parsedate_to_datetime(prev_msg.get("Date")).strftime("%a, %d %b %Y at %H:%M")
    except Exception:
        when = prev_msg.get("Date") or ""
    who = mail.dec(prev_msg.get("From"))
    body = "\n".join("> " + line if line else ">" for line in prev_text.strip().split("\n"))
    return f"\n\nOn {when}, {who} wrote:\n{body}\n"


def build_reply(thread, text, from_addr, from_name, kind, step=0):
    prev_msg, prev_text = previous_message(thread)
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = formataddr((thread.get("name") or "", thread["addr"])) if thread.get("name") else thread["addr"]
    subject = thread["subject"] or ""
    msg["Subject"] = subject if re.match(r"^\s*re\s*:", subject, re.I) else "Re: " + subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc).astimezone())
    msg["Message-ID"] = f"<vf-{uuid.uuid4().hex}@{mail.OWN_DOMAIN}>"
    refs = [m for m in thread.get("out_mids", []) if m]
    if prev_msg is not None and prev_msg.get("Message-ID"):
        last_mid = mail.msg_ids(prev_msg.get("Message-ID"))[0]
        if last_mid not in refs:
            refs.append(last_mid)
    if refs:
        msg["In-Reply-To"] = refs[-1]
        msg["References"] = " ".join(refs)
    msg["X-Vulgone-Follower"] = f"kind={kind}; step={step}; thread={thread['id']}"
    msg.set_content(text.rstrip() + "\n" + quote(prev_msg, prev_text), subtype="plain", charset="utf-8")
    return msg


def smtp_send(msg):
    """Hand one message to the Zoho relay. Raises on any failure."""
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(mail.SMTP_HOST, mail.SMTP_PORT, timeout=60, context=context) as s:
        s.login(mail.USER, mail.PASS)
        s.send_message(msg)


def template_context(thread, persona_hint=""):
    first_name = (thread.get("name") or "").split(" ")[0] if thread.get("name") else "there"
    return {
        "company": company_from(thread),
        "name": first_name,
        "persona": persona_hint or "Vulgone Security",
        "persona_first": (persona_hint or "Vulgone").split(" ")[0],
    }


def send_followup(thread, step, settings, dry_run):
    """Send (or dry-run) follow-up `step` (2 or 3) for a thread. Returns the send record dict."""
    tpl = store.followup_template(step)
    if not tpl:
        raise ValueError(f"no template for step {step}")
    from_addr, from_name = choose_sender(thread, settings)
    text = render(tpl["body"], template_context(thread))
    msg = build_reply(thread, text, from_addr, from_name, kind="followup", step=step)
    return _dispatch(msg, thread, step, dry_run)


def send_reply(thread, text, settings, dry_run=False):
    """Send a human-approved answer in the thread (kind=reply, step 0 = not part of the sequence)."""
    from_addr, from_name = choose_sender(thread, settings)
    msg = build_reply(thread, text, from_addr, from_name, kind="reply", step=0)
    return _dispatch(msg, thread, 0, dry_run)


def _dispatch(msg, thread, step, dry_run):
    body = msg.get_content()
    record = {
        "thread_id": thread["id"], "step": step, "to": thread["addr"], "from": msg["From"],
        "subject": msg["Subject"], "message_id": msg["Message-ID"], "body": body,
    }
    if dry_run:
        store.record_send(thread["id"], step, thread["addr"], msg["From"], msg["Subject"],
                          msg["Message-ID"], body, "dry")
        record["status"] = "dry"
        return record
    try:
        smtp_send(msg)
    except Exception as exc:  # noqa: BLE001 - any SMTP failure must be recorded, not hidden
        store.record_send(thread["id"], step, thread["addr"], msg["From"], msg["Subject"],
                          msg["Message-ID"], body, "error", error=str(exc))
        record.update(status="error", error=str(exc))
        return record
    store.record_send(thread["id"], step, thread["addr"], msg["From"], msg["Subject"],
                      msg["Message-ID"], body, "sent")
    record["status"] = "sent"
    return record


def send_test(settings, to_addr=None):
    """Send the step-2 template to ourselves so threading and the Sent copy can be checked."""
    to_addr = (to_addr or mail.USER).lower()
    tpl = store.followup_template(2)
    msg = EmailMessage()
    msg["From"] = formataddr((mail.SENDER_NAME, mail.USER)) if mail.SENDER_NAME else mail.USER
    msg["To"] = to_addr
    msg["Subject"] = "Re: Vulgone Follower test"
    msg["Date"] = format_datetime(datetime.now(timezone.utc).astimezone())
    msg["Message-ID"] = f"<vf-test-{uuid.uuid4().hex}@{mail.OWN_DOMAIN}>"
    msg["X-Vulgone-Follower"] = "kind=test; step=0"
    msg.set_content(render(tpl["body"], {"company": "Vulgone", "name": "there", "persona": "Vulgone Security",
                                         "persona_first": "Vulgone"}) + "\n\n(test message from Vulgone Follower)\n",
                    subtype="plain", charset="utf-8")
    smtp_send(msg)
    store.log("test_email", f"test email sent to {to_addr}", data={"message_id": msg["Message-ID"]})
    return msg["Message-ID"]
