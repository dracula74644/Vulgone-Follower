"""Unit tests for the follow-up rules: thread stages, queue eligibility, sending gates, reply
classification and template rendering. No mailbox, no network, no database."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("MAIL_USER", "security@vulgone.com")
os.environ.setdefault("MAIL_PASS", "x")
os.environ.setdefault("DATA_DIR", "/tmp/vulgone-follower-tests")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import automation  # noqa: E402
import sender  # noqa: E402
import threads as th  # noqa: E402

DAY = 86400
T0 = 1_760_000_000  # some reference epoch
OWN = "vulgone.com"


def msg(uid, frm, to, subject, ts, mid, folder="Sent", irt=(), refs=(), **extra):
    base = {
        "uid": str(uid), "folder": folder, "from": {"name": "", "addr": frm},
        "to": [{"name": "", "addr": to}], "cc": [], "subject": subject,
        "date": datetime.fromtimestamp(ts).isoformat(), "ts": ts, "size": 100, "message_id": mid,
        "in_reply_to": list(irt), "references": list(refs), "has_attachments": False,
        "automated": False, "auto_submitted": False, "flags": [], "seen": True, "answered": False, "flagged": False,
    }
    base.update(extra)
    return base


def sent(uid, to, ts, mid, subject="Regarding Your Website's Security", **kw):
    return msg(uid, "security@vulgone.com", to, subject, ts, mid, **kw)


def received(uid, frm, ts, mid, irt, subject="Re: Regarding Your Website's Security", **kw):
    return msg(uid, frm, "security@vulgone.com", subject, ts, mid, folder="INBOX", irt=[irt], refs=[irt], **kw)


def build(sent_msgs, recv_msgs, now, waits=(7, 7)):
    unmatched = []
    threads = [th.finish_thread(t, waits, now) for t in th.build_threads(sent_msgs, recv_msgs, OWN, unmatched)]
    th.mark_domain_engagement(threads)
    return threads, unmatched


def test_manual_followups_count_as_steps():
    s = [sent(1, "a@acme.com", T0, "<m1@zoho.in>"),
         sent(2, "a@acme.com", T0 + 8 * DAY, "<m2@vulgone.com>", subject="Re: Regarding Your Website's Security", irt=["<m1@zoho.in>"])]
    (t,), _ = build(s, [], T0 + 16 * DAY)
    assert t["steps"] == 2 and t["stage"] == "followup1" and t["due"]
    assert t["initial_source"] == "platform" and t["out_mids"] == ["<m1@zoho.in>", "<m2@vulgone.com>"]


def test_recontact_after_long_gap_starts_a_new_thread():
    s = [sent(1, "a@acme.com", T0 - 100 * DAY, "<m1@zoho.in>"),
         sent(2, "a@acme.com", T0 - 92 * DAY, "<m2@zoho.in>", subject="Re: Regarding Your Website's Security", irt=["<m1@zoho.in>"]),
         sent(3, "a@acme.com", T0 - 85 * DAY, "<m3@zoho.in>", subject="Re: Regarding Your Website's Security", irt=["<m2@zoho.in>"]),
         sent(4, "a@acme.com", T0, "<m4@zoho.in>")]  # same subject, 85 days later: a new outreach
    threads, _ = build(s, [], T0 + 8 * DAY)
    assert sorted(t["steps"] for t in threads) == [1, 3]
    new = next(t for t in threads if t["steps"] == 1)
    assert new["stage"] == "sent" and new["due"]


def test_resend_within_a_day_is_one_step():
    s = [sent(1, "a@acme.com", T0, "<m1@zoho.in>"), sent(2, "a@acme.com", T0 + 3600, "<m2@zoho.in>")]
    (t,), _ = build(s, [], T0 + 8 * DAY)
    assert t["sends"] == 2 and t["steps"] == 1 and t["stage"] == "sent"


def test_reply_and_bounce_stop_the_sequence_but_auto_ack_does_not():
    s = [sent(1, "a@acme.com", T0, "<m1@zoho.in>")]
    reply = received(10, "a@acme.com", T0 + DAY, "<r1@acme>", "<m1@zoho.in>")
    (t,), _ = build(s, [reply], T0 + 10 * DAY)
    assert t["stage"] == "replied" and t["replies"] == 1 and t["turn"] == "us"

    bounce = received(11, "mailer-daemon@zoho.in", T0 + 60, "<b1@zoho>", "<m1@zoho.in>", subject="Delivery Status Notification (Failure)")
    (t,), _ = build(s, [bounce], T0 + 10 * DAY)
    assert t["stage"] == "bounced"

    ack = received(12, "support@acme.com", T0 + 120, "<a1@acme>", "<m1@zoho.in>", subject="[Request received]", auto_submitted=True)
    (t,), _ = build(s, [ack], T0 + 10 * DAY)
    assert t["stage"] == "sent" and t["received"] == 1 and t["replies"] == 0 and t["due"]


def test_internal_and_test_addresses_are_not_clients():
    s = [sent(1, "zeeshanbeg2004@gmail.com", T0, "<m1@zoho.in>"), sent(2, "support@vulgone.com", T0, "<m2@zoho.in>")]
    threads, _ = build(s, [], T0 + DAY)
    assert threads == []


def test_scopesync_mail_in_inbox_counts_as_outgoing():
    r = msg(5, "scopesync.inquiry@gmail.com", "security@client.io", "Security contact?", T0, "<s1@gmail>", folder="INBOX")
    r["cc"] = [{"name": "", "addr": "security@vulgone.com"}]
    (t,), _ = build([], [r], T0 + 10 * DAY)
    assert t["addr"] == "security@client.io" and t["stage"] == "sent" and t["sends"] == 1


def test_domain_engagement_flag():
    s = [sent(1, "hello@acme.com", T0, "<m1@zoho.in>"), sent(2, "sales@acme.com", T0 + DAY, "<m2@zoho.in>")]
    reply = received(10, "hello@acme.com", T0 + 2 * DAY, "<r1@acme>", "<m1@zoho.in>")
    threads, _ = build(s, [reply], T0 + 10 * DAY)
    by = {t["addr"]: t for t in threads}
    assert by["hello@acme.com"]["stage"] == "replied"
    assert by["sales@acme.com"]["engaged_at_domain"] is True


def test_researcher_mail_attaches_by_address_and_domain():
    s = [sent(1, "hello@acme.com", T0, "<m1@zoho.in>")]
    threads, _ = build(s, [], T0 + 10 * DAY)
    r_out = msg(50, "researcher@vulgone.com", "security@acme.com", "Vulnerability report - acme", T0 + 2 * DAY, "<r1@zoho.in>", folder="Sent", has_attachments=True)
    r_out2 = msg(51, "researcher@vulgone.com", "hello@acme.com", "Re: Vulnerability report - acme", T0 + 3 * DAY, "<r2@zoho.in>", folder="Sent")
    r_in = msg(60, "cto@acme.com", "researcher@vulgone.com", "Re: Vulnerability report - acme (PayPal invoice)", T0 + 4 * DAY, "<c1@acme>", folder="INBOX")
    early = msg(61, "cto@acme.com", "researcher@vulgone.com", "old mail", T0 - 5 * DAY, "<c0@acme>", folder="INBOX")
    th.attach_researcher(threads, [r_out, r_out2], [r_in, early], OWN)
    t = threads[0]
    assert [m["uid"] for m in t["researcher"]] == ["50", "51", "60"]
    assert t["report_sent_ts"] == T0 + 2 * DAY and t["report_count"] == 2
    assert t["stage"] == "sent"  # researcher mail never changes the follow-up stage
    queue, skipped = automation.compute_queue(threads, _settings(backlog_since="2025-01-01"), set(), set(), T0 + 10 * DAY)
    assert queue == [] and skipped["talking on researcher@"] == 1  # ...but it does stop the sequence


def test_unmatched_reply_is_collected():
    r = received(20, "someone@new.io", T0, "<x@new>", "<unknown@zoho.in>")
    threads, unmatched = build([], [r], T0 + DAY)
    assert threads == [] and [m["uid"] for m in unmatched] == ["20"]


def _settings(**over):
    s = {"paused": False, "dry_run": True, "wait_days": [7, 7], "daily_cap": 150, "min_gap_seconds": 120,
         "queue_order": "newest", "backlog_since": "2026-01-01",
         "window": {"days": [0, 1, 2, 3, 4], "start": "13:00", "end": "03:00", "tz": "Asia/Kolkata"}}
    s.update(over)
    return s


def test_queue_rules():
    now = T0 + 20 * DAY
    s = [
        sent(1, "due@a.com", T0, "<a1@zoho.in>"),                                   # step 2 due
        sent(2, "fresh@b.com", now - 2 * DAY, "<b1@zoho.in>"),                       # waiting
        sent(3, "two@c.com", T0, "<c1@zoho.in>"),
        sent(4, "two@c.com", T0 + 8 * DAY, "<c2@vulgone.com>", subject="Re: Regarding Your Website's Security", irt=["<c1@zoho.in>"]),  # step 3 due
        sent(5, "done@d.com", T0 - 30 * DAY, "<d1@zoho.in>"),
        sent(6, "done@d.com", T0 - 22 * DAY, "<d2@zoho.in>", subject="Re: Regarding Your Website's Security", irt=["<d1@zoho.in>"]),
        sent(7, "done@d.com", T0 - 15 * DAY, "<d3@zoho.in>", subject="Re: Regarding Your Website's Security", irt=["<d2@zoho.in>"]),  # finished
        sent(8, "blocked@e.com", T0, "<e1@zoho.in>"),                                # suppressed domain
        sent(9, "old@f.com", T0 - 400 * DAY, "<f1@zoho.in>"),                        # older than backlog start
    ]
    reply = received(10, "two@c.com", T0 + 9 * DAY, "<r@c>", "<c2@vulgone.com>")   # makes two@c.com replied
    threads, _ = build(s, [], now)
    queue, skipped = automation.compute_queue(threads, _settings(backlog_since="2025-01-01"), {"@e.com"}, set(), now)
    got = {(c["thread"]["addr"], c["step"]) for c in queue}
    assert got == {("due@a.com", 2), ("two@c.com", 3)}
    assert skipped["suppressed"] == 1 and skipped["sequence finished"] == 1 and skipped["waiting period not over"] == 1
    assert skipped["older than backlog start"] == 1
    # newest initial email first
    assert [c["thread"]["addr"] for c in queue] == ["two@c.com", "due@a.com"] or queue[0]["thread"]["first_ts"] >= queue[1]["thread"]["first_ts"]

    threads, _ = build(s, [reply], now)
    queue, skipped = automation.compute_queue(threads, _settings(backlog_since="2025-01-01"), {"@e.com"}, {("due@a.com|regarding your website's security", 2)}, now)
    assert queue == [] and skipped["replied or bounced"] == 1 and skipped["already sent by follower"] == 1


def test_window_and_gate():
    tz = ZoneInfo("Asia/Kolkata")
    w = _settings()["window"]
    assert automation.window_open(datetime(2026, 10, 7, 14, 0, tzinfo=tz), w)        # Wednesday afternoon
    assert automation.window_open(datetime(2026, 10, 8, 2, 30, tzinfo=tz), w)        # Thursday 02:30 = Wed window
    assert not automation.window_open(datetime(2026, 10, 8, 3, 30, tzinfo=tz), w)
    assert not automation.window_open(datetime(2026, 10, 7, 12, 59, tzinfo=tz), w)
    assert not automation.window_open(datetime(2026, 10, 10, 14, 0, tzinfo=tz), w)   # Saturday
    assert automation.window_open(datetime(2026, 10, 11, 1, 0, tzinfo=tz), w) is False  # Sunday 01:00 (Sat window)
    assert automation.next_window_start(datetime(2026, 10, 10, 14, 0, tzinfo=tz), w).weekday() == 0

    now = datetime(2026, 10, 7, 14, 0, tzinfo=tz).timestamp()
    ok, reason, _ = automation.send_gate(_settings(), now, 0, now - 1000, 0, 0)
    assert ok
    assert automation.send_gate(_settings(paused=True), now, 0, 0, 0, 0)[1] == "paused"
    assert automation.send_gate(_settings(), now, 150, 0, 0, 0)[1] == "daily cap reached"
    ok, reason, retry = automation.send_gate(_settings(), now, 0, now - 30, 0, 0)
    assert not ok and reason == "rate limit" and retry == now - 30 + 120
    assert automation.send_gate(_settings(), now, 0, 0, 2, now - 60)[1] == "backing off after error"
    assert automation.send_gate(_settings(), datetime(2026, 10, 10, 14, 0, tzinfo=tz).timestamp(), 0, 0, 0, 0)[1] == "outside sending window"


def test_classify_reply():
    c = automation.classify_reply
    assert c("Re: x", "Hi, you can send the report directly here. Thanks!")["category"] == "go_ahead"
    assert c("Re: x", "We don't have a bug bounty program at this time, but feel free to share.")["category"] == "no_bounty"
    r = c("Re: x", "We run our program via HackerOne: https://hackerone.com/acme please submit there.")
    assert r["category"] == "program" and r["platform"] == "hackerone" and r["draft_template"] == "reply_hackerone_invite"
    assert c("Re: x", "Please see https://acme.com/security for our responsible disclosure policy.")["category"] == "program"
    assert c("Re: x", "Thanks, but we're not interested. Please do not contact us again.")["category"] == "stop"
    assert c("Re: x", "You are explicitly not authorized to perform any security testing against our systems.")["category"] == "stop"
    assert c("Re: x", "If it's critical we'd be happy to pay a bounty of up to $500.")["category"] == "other"
    assert c("Re: x", "Could you clarify which website you are referring to?")["category"] == "question"
    assert c("Re: x", "We have received your request and our team will get back to you shortly.")["category"] == "ack"
    assert c("Re: x", "Please forward this to security@acme.com, they handle these.", "hello@other.io")["category"] == "forwarded"
    r = c("Re: x", "You can send security reports directly here, or to security@fileshot.io.", "brendan@fileshot.io")
    assert r["category"] == "go_ahead"
    assert c("Re: x", "Your request has been escalated to a human agent. They will get back to you shortly.")["category"] == "forwarded"
    r = c("Re: x", "We are a small startup with no budget for bounties, sorry.")
    assert r["category"] == "no_bounty" and r["draft_template"] == "reply_small_startup"


def test_classify_researcher_reply():
    c = automation.classify_researcher_reply
    assert c("Please do not waste your time and do not send your report as I will not be able to pay.")["category"] == "stop"
    assert c("We do not authorize further penetration or security testing.")["category"] == "stop"
    assert c("Dear Vulgone please send your email to pay by paypal 80 EUR.")["category"] == "reply"
    assert c("Hey there, Thanks for the report. This has been fixed.")["category"] == "reply"


def test_render_and_company():
    assert sender.render("Hi {company}, {missing}", {"company": "Acme"}) == "Hi Acme, {missing}"
    assert sender.render("Hi [Company/Team Name],", {"company": "Acme"}) == "Hi Acme,"
    t = {"subject": "Observation on - stageworks - Quick Clarification | Vulgone", "domain": "stageworks.io"}
    assert sender.company_from(t) == "stageworks"
    t = {"subject": "Regarding Your Website's Security", "domain": "koalendar.com"}
    assert sender.company_from(t) == "Koalendar"
    t = {"subject": "Vulnerability Report Confirmation - gmail | Vulgone", "domain": "gmail.com"}
    assert sender.company_from(t) == "there"
    assert sender.persona_from_text("Hi,\nI'm Sahil from Vulgone.\nWhile reviewing...") == "Sahil"
    assert sender.persona_from_text("Hi, Zeeshan here from Vulgone. We help") == "Zeeshan"


def test_choose_sender_follows_initial_email():
    t = {"initial_from": {"addr": "ramsha.mirza@vulgone.com", "name": "Ramsha Mirza"}}
    assert sender.choose_sender(t, {"sender_mode": "initial"}) == ("ramsha.mirza@vulgone.com", "Ramsha Mirza")
    assert sender.choose_sender(t, {"sender_mode": "fixed", "fixed_sender": "security@vulgone.com", "fixed_sender_name": "Vulgone Security"}) \
        == ("security@vulgone.com", "Vulgone Security")
    t = {"initial_from": {"addr": "someone@elsewhere.com", "name": "X"}}
    assert sender.choose_sender(t, {"sender_mode": "initial"})[0] == "security@vulgone.com"
