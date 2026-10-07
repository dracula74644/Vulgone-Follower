# Vulgone Follower

Follow-up automation and client pipeline for the `security@vulgone.com` Zoho outreach mailbox.

The initial inquiry is sent by a separate platform through this mailbox. Vulgone Follower
watches the Sent folder, and when a client stays silent it sends the **1st follow-up after
7 days** and the **2nd (final) follow-up 7 days later**, as threaded `Re:` replies using the
team's own templates. Then the client is dropped. Every reply is classified, the sequence
stops, and a draft answer is prepared for a human to send.

## Run

Put the credentials in `.env` (`MAIL_USER`, `MAIL_PASS`, `IMAP_HOST`, `IMAP_PORT`,
`SMTP_HOST`, `SMTP_PORT`, `SENDER_NAME`), then:

```
./startup.sh            # build and start in Docker, then open http://127.0.0.1:5055
./startup.sh stop       # stop (the automation stops with the container)
./startup.sh restart    # rebuild and restart (after code changes)
./startup.sh logs       # follow the logs
./startup.sh status     # container state and health
./startup.sh tests      # unit tests for the rules, gates and classifier (no mailbox needed)
./startup.sh local      # run without Docker, in a Python virtualenv
```

`HOST_PORT=8080 ./startup.sh` publishes on another port. The container reads `.env` at
runtime, runs as a non-root user, is published on localhost only, and keeps the mail cache
and its SQLite state (settings, sends log, pipeline) in the `follower-data` volume.

## It starts in DRY RUN

Everything runs — the queue is computed, "would send" lines appear in Activity, replies are
classified — but no email leaves until you open **Settings → Switch to LIVE sending** and
type `LIVE`. Use **Send a test email to me** first to see exactly what a follow-up looks like.
The **▶ / ⏸** button in the header pauses and resumes the automation at any time.

## The rules

A client gets the next follow-up when all of these hold:

1. We emailed them first (not a thread they started), they are not one of our own or test
   addresses, and neither the address nor the domain is on the do-not-contact list.
2. Nothing real came back: no reply and no bounce. Auto-acknowledgements and out-of-office
   messages do not count.
3. Nobody else at the same company replied to us after our email to this client.
4. The sequence is not finished: 1 email sent → 1st follow-up, 2 → 2nd follow-up, 3 → done.
   Follow-ups sent by hand count; two sends within 24 h count as one. A new email with the
   same subject more than 60 days after a thread went quiet starts a new sequence.
5. The last email is at least 7 days old (per-step setting).
6. Right before sending, the mailbox is re-checked: a reply or a manual send that just
   arrived cancels the send.

Sending gates, in order: not paused → inside the window (Mon–Fri, 13:00–03:00 IST by
default) → under the daily cap (150) → at least **2 minutes since the last email from the
mailbox** (the platform's sends count too) → no recent SMTP error (3 errors in a row pause
the automation). One send per 30-second tick. Newest clients first.

## Replies and the client pipeline

The pipeline mirrors the real process: inquiry → client answers → we send a sample from
`researcher@` → the conversation continues there. Each reply is classified and the client
gets one of these stages:

| Stage | Set when | What to do |
|---|---|---|
| Answer them | question, "no bounty", or anything unclear | reply (a draft is offered where a script exists) |
| Send a sample | they said "report it to this email" | send the sample from researcher@ |
| Sample sent | the first researcher@ email to them is seen | wait |
| In conversation (researcher@) | they wrote back on researcher@ | answer from that mailbox when it's your turn |
| Waiting on them | helpdesk "we received it" | nothing |
| Submit via their program | they point to HackerOne/Bugcrowd/own page | submit there |
| Confirm do-not-contact | "not interested / stop / not authorized" | confirm the block |
| Closed / Stopped | set by you | — |

Drafts are editable and only go out when you click **Send reply**. The Replies tab also
lists clients whose last message on researcher@ is unanswered. A client row keeps only the
stage, their program page link, notes and the do-not-contact flag; sample-sent date/count and
the last researcher@ message are filled automatically.

## Tabs

Queue (what goes out next) · Replies (waiting for you, do-not-contact confirmations,
unmatched mail) · Pipeline · Sequence (the three no-reply lists + CSV export) · Activity
(audit log) · Settings · Mail (plain folder browser).

## Notes

- The mailbox is only ever read; the only writes are the emails sent through SMTP.
- Our own sends carry an `X-Vulgone-Follower` header and a `vf-…@vulgone.com` Message-ID, so
  they are recognisable in Sent and in the conversation view.
- `data/follower.db` (SQLite) holds settings, the sends log, classified replies, the pipeline
  and the do-not-contact list; `cache/` holds mail headers. Both live in the Docker volume.
