"""IMAP access, header cache and message parsing for the Zoho mailbox (shared by the web app,
the sender and the scheduler). Every folder is opened read-only: nothing here changes the mailbox.
"""
import email
import html as html_lib
import imaplib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(BASE, "cache"))


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env(os.path.join(BASE, ".env"))

USER = os.environ["MAIL_USER"].lower()
PASS = os.environ["MAIL_PASS"]
HOST = os.environ.get("IMAP_HOST", "imappro.zoho.in")
PORT = int(os.environ.get("IMAP_PORT", "993"))
SMTP_HOST = os.environ.get("SMTP_HOST", "smtppro.zoho.in")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SENDER_NAME = os.environ.get("SENDER_NAME", "")
OWN_DOMAIN = USER.split("@")[-1]
# Optional second mailbox: the one the team sends vulnerability reports from.
RESEARCHER_USER = (os.environ.get("RESEARCHER_USER") or "").lower()
RESEARCHER_PASS = os.environ.get("RESEARCHER_PASS") or ""

HEADER_FIELDS = (
    "FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES CONTENT-TYPE "
    "LIST-UNSUBSCRIBE AUTO-SUBMITTED PRECEDENCE"
)
AUTOMATED_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounces?|notifications?|alerts?"
    r"|\w*dmarc\w*|reports?)\b",
    re.I,
)
# Folders where a reply from a client could land.
REPLY_FOLDER_ROLES = {"inbox", "", "spam", "trash"}


# ---------------------------------------------------------------- IMAP connection

def q(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mailbox:
    """One shared IMAP connection per account, guarded by a lock, reconnected on failure."""

    def __init__(self, user=None, password=None, host=None, port=None, ns=""):
        self.user, self.password = user or USER, password or PASS
        self.host, self.port = host or HOST, port or PORT
        self.ns = ns  # cache namespace ("" for the main mailbox)
        self.lock = threading.RLock()
        self.conn = None
        self.selected = None
        self._folders = {"at": 0, "value": None}

    def _connect(self):
        conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=60)
        conn.login(self.user, self.password)
        self.conn, self.selected = conn, None

    def get(self, folder=None):
        if self.conn is None:
            self._connect()
        else:
            try:
                self.conn.noop()
            except Exception:
                self._connect()
        if folder is not None and folder != self.selected:
            typ, data = self.conn.select(q(folder), readonly=True)
            if typ != "OK":
                raise ValueError(f"Cannot open folder {folder!r}: {data}")
            self.selected = folder
        return self.conn


mb = Mailbox()
researcher = Mailbox(RESEARCHER_USER, RESEARCHER_PASS, ns="researcher") if RESEARCHER_USER and RESEARCHER_PASS else None


def box_for(account):
    """The Mailbox for an account name used in API calls ('' / 'main' or 'researcher')."""
    if account == "researcher":
        if researcher is None:
            raise LookupError("researcher mailbox is not configured")
        return researcher
    return mb


def list_folders(conn):
    typ, data = conn.list()
    folders = []
    for raw in data or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        m = re.match(r'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)', line)
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        flags = m.group("flags").lower()
        if "\\noselect" in flags:
            continue
        role = "inbox" if name.upper() == "INBOX" else ""
        for r in ("sent", "drafts", "junk", "trash"):
            if "\\" + r in flags:
                role = "spam" if r == "junk" else r
        if not role and name.lower() == "templates":
            role = "templates"
        folders.append({"name": name, "role": role})
    return folders


def folder_names(conn, box=None):
    """{'sent': name, 'reply': [names where client mail lands], 'all': [...]} cached for 10 minutes."""
    box = box or mb
    cache = box._folders
    if time.time() - cache["at"] > 600 or cache["value"] is None:
        folders = list_folders(conn)
        cache["value"] = {
            "sent": next((f["name"] for f in folders if f["role"] == "sent"), "Sent"),
            "reply": [f["name"] for f in folders if f["role"] in REPLY_FOLDER_ROLES],
            "all": folders,
        }
        cache["at"] = time.time()
    return cache["value"]


def uid_search(conn, criteria, literal=None):
    if literal is not None:
        conn.literal = literal
        typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *criteria)
    else:
        typ, data = conn.uid("SEARCH", *criteria)
    if typ != "OK":
        raise ValueError(f"Search failed: {data}")
    return [u.decode() for u in (data[0] or b"").split()]


# ---------------------------------------------------------------- header parsing

def dec(value):
    if not value:
        return ""
    try:
        text = str(make_header(decode_header(value)))
    except Exception:
        text = str(value)
    return re.sub(r"\s+", " ", text).strip()


def addrs(value):
    out = []
    for name, addr in getaddresses([dec(value)] if value else []):
        if addr:
            out.append({"name": name, "addr": addr.lower()})
    return out


def msg_ids(value):
    return [x.lower() for x in re.findall(r"<[^>]+>", value or "")]


def parse_internaldate(value):
    try:
        return datetime.strptime(value, "%d-%b-%Y %H:%M:%S %z")
    except Exception:
        return None


def apply_flags(summary, flags):
    summary["flags"] = flags
    summary["seen"] = "\\Seen" in flags
    summary["answered"] = "\\Answered" in flags
    summary["flagged"] = "\\Flagged" in flags


def summarize(meta, header_bytes):
    meta = meta.decode(errors="replace")
    uid = re.search(r"UID (\d+)", meta)
    flags = re.search(r"FLAGS \(([^)]*)\)", meta)
    idate = re.search(r'INTERNALDATE "([^"]+)"', meta)
    size = re.search(r"RFC822\.SIZE (\d+)", meta)
    hdr = email.message_from_bytes(header_bytes or b"")

    received = parse_internaldate(idate.group(1)) if idate else None
    try:
        sent_date = parsedate_to_datetime(hdr.get("Date")) if hdr.get("Date") else None
        if sent_date and sent_date.tzinfo is None:
            sent_date = sent_date.replace(tzinfo=timezone.utc)
    except Exception:
        sent_date = None
    when = received or sent_date or datetime.fromtimestamp(0, timezone.utc)

    frm = addrs(hdr.get("From"))
    sender = frm[0] if frm else {"name": "", "addr": ""}
    ctype = (hdr.get("Content-Type") or "").lower()
    auto_submitted = (hdr.get("Auto-Submitted") or "no").lower() != "no"
    automated = bool(
        AUTOMATED_RE.match(sender["addr"].split("@")[0])
        or hdr.get("List-Unsubscribe")
        or auto_submitted
        or (hdr.get("Precedence") or "").lower() in ("bulk", "list", "junk")
    )
    s = {
        "uid": uid.group(1) if uid else None,
        "from": sender,
        "to": addrs(hdr.get("To")),
        "cc": addrs(hdr.get("Cc")),
        "subject": dec(hdr.get("Subject")) or "(no subject)",
        "date": (sent_date or when).isoformat(),
        "ts": when.timestamp(),
        "size": int(size.group(1)) if size else 0,
        "message_id": (msg_ids(hdr.get("Message-ID")) or [""])[0],
        "in_reply_to": msg_ids(hdr.get("In-Reply-To")),
        "references": msg_ids(hdr.get("References")),
        "has_attachments": ctype.startswith("multipart/mixed"),
        "automated": automated,
        "auto_submitted": auto_submitted,
    }
    apply_flags(s, flags.group(1).split() if flags else [])
    return s


def fetch_summaries(conn, uids, folder):
    out = []
    for i in range(0, len(uids), 300):
        chunk = ",".join(uids[i:i + 300])
        typ, data = conn.uid(
            "FETCH", chunk,
            f"(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])",
        )
        if typ != "OK":
            raise ValueError(f"Fetch failed: {data}")
        j = 0
        while j < len(data):
            item = data[j]
            if isinstance(item, tuple):
                meta, body = item[0], item[1]
                # Some servers put FLAGS etc. after the header literal.
                if j + 1 < len(data) and isinstance(data[j + 1], bytes):
                    meta += data[j + 1]
                    j += 1
                s = summarize(meta, body)
                if s["uid"]:
                    s["folder"] = folder
                    out.append(s)
            j += 1
    return out


def with_attachments(conn, uids):
    """Zoho can't SEARCH on Content-Type, so filter on the fetched header instead."""
    keep = set()
    for i in range(0, len(uids), 500):
        typ, data = conn.uid(
            "FETCH", ",".join(uids[i:i + 500]), "(UID BODY.PEEK[HEADER.FIELDS (CONTENT-TYPE)])"
        )
        for item in data or []:
            if isinstance(item, tuple) and b"multipart/mixed" in item[1].lower():
                m = re.search(rb"UID (\d+)", item[0])
                if m:
                    keep.add(m.group(1).decode())
    return [u for u in uids if u in keep]


def imap_date(d):
    return d.strftime("%d-%b-%Y")


def since_from_days(days):
    return None if days <= 0 else datetime.now() - timedelta(days=days)


def build_criteria(args):
    crit, literal = [], None

    def s(v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    if args.get("unread") == "1":
        crit.append("UNSEEN")
    if args.get("flagged") == "1":
        crit.append("FLAGGED")
    if args.get("since"):
        crit += ["SINCE", imap_date(datetime.fromisoformat(args["since"]))]
    if args.get("before"):
        # IMAP BEFORE is exclusive; make the chosen day inclusive.
        crit += ["BEFORE", imap_date(datetime.fromisoformat(args["before"]) + timedelta(days=1))]
    frm = (args.get("from") or "").strip()
    if frm and frm.isascii():
        crit += ["FROM", s(frm)]
    text = (args.get("q") or "").strip()
    if text:
        if text.isascii():
            crit += ["TEXT", s(text)]
        else:
            crit.append("TEXT")  # literal must be the final argument
            literal = text.encode("utf-8")
    return (crit or ["ALL"]), literal


# ---------------------------------------------------------------- header cache

_cache = {}


def cache_path(folder, ns=""):
    name = re.sub(r"[^\w.-]", "_", folder) + ".json"
    return os.path.join(CACHE_DIR, ns, name) if ns else os.path.join(CACHE_DIR, name)


def load_cache(folder, ns=""):
    key = f"{ns}:{folder}"
    if key not in _cache:
        try:
            with open(cache_path(folder, ns)) as fh:
                _cache[key] = json.load(fh)
        except (OSError, ValueError):
            _cache[key] = {"uidvalidity": None, "items": {}}
    return _cache[key]


def save_cache(folder, ns=""):
    path = cache_path(folder, ns)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(_cache[f"{ns}:{folder}"], fh)
    os.replace(tmp, path)


def sync_folder(conn, folder, box=None):
    """Bring the local header cache for `folder` up to date and return its summaries."""
    box = box or mb
    typ, data = conn.status(q(folder), "(UIDVALIDITY MESSAGES)")
    if typ != "OK":
        raise ValueError(f"Cannot open folder {folder!r}: {data}")
    text = data[0].decode(errors="replace") if data and data[0] else ""
    validity = (re.search(r"UIDVALIDITY (\d+)", text) or [None, None])[1]
    count = int((re.search(r"MESSAGES (\d+)", text) or [None, "0"])[1])

    cache = load_cache(folder, box.ns)
    if cache["uidvalidity"] != validity:
        cache = _cache[f"{box.ns}:{folder}"] = {"uidvalidity": validity, "items": {}}
    items = cache["items"]

    server = {}
    if count:
        box.get(folder)
        typ, data = conn.uid("FETCH", "1:*", "(UID FLAGS)")
        for item in data or []:
            if not isinstance(item, bytes):
                continue
            u = re.search(rb"UID (\d+)", item)
            f = re.search(rb"FLAGS \(([^)]*)\)", item)
            if u:
                server[u.group(1).decode()] = f.group(1).decode().split() if f else []

    changed = False
    missing = [u for u in server if u not in items]
    if missing:
        for s in fetch_summaries(conn, missing, folder):
            if box.ns:
                s["account"] = box.ns
            items[s["uid"]] = s
        changed = True
    for u in [u for u in items if u not in server]:
        del items[u]
        changed = True
    for u, flags in server.items():
        s = items.get(u)
        if s and s["flags"] != flags:
            apply_flags(s, flags)
            changed = True
    if changed:
        save_cache(folder, box.ns)
    return list(items.values())


def collect(conn, folders, since, box=None):
    cutoff = since.timestamp() if since else 0
    out = []
    for f in folders:
        try:
            out += [m for m in sync_folder(conn, f, box) if m["ts"] >= cutoff]
        except ValueError:
            continue
    return out


def newest_sent_ts(conn, sent_folder, box=None):
    """When the newest message in Sent arrived (any sender sharing the mailbox)."""
    (box or mb).get(sent_folder)
    typ, data = conn.fetch("*", "(INTERNALDATE)")
    for item in data or []:
        if isinstance(item, bytes):
            m = re.search(rb'INTERNALDATE "([^"]+)"', item)
            if m:
                d = parse_internaldate(m.group(1).decode())
                if d:
                    return d.timestamp()
    return 0


# ---------------------------------------------------------------- full messages

def fetch_raw(folder, uid, account=""):
    """(fetch metadata, raw bytes) of one message. Raises LookupError if it does not exist."""
    if not str(uid).isdigit():
        raise LookupError(f"bad uid {uid!r}")
    box = box_for(account)
    with box.lock:
        conn = box.get(folder)
        typ, data = conn.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
    for item in data or []:
        if isinstance(item, tuple):
            return item[0].decode(errors="replace"), item[1]
    raise LookupError(f"message {folder}#{uid} not found")


def parse_message(raw):
    return email.message_from_bytes(raw, policy=policy.compat32)


def leaf_parts(msg):
    return [p for p in msg.walk() if not p.is_multipart()]


def part_text(part):
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def html_to_text(html):
    t = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_lib.unescape(t)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def message_text(msg):
    """Plain text of a parsed message: the text/plain part, else the HTML part converted."""
    for p in leaf_parts(msg):
        if p.get_content_type() == "text/plain" and not p.get_filename():
            return part_text(p).replace("\r\n", "\n")
    for p in leaf_parts(msg):
        if p.get_content_type() == "text/html" and not p.get_filename():
            return html_to_text(part_text(p))
    return ""


QUOTE_SPLIT_RE = re.compile(
    r"\n\s*(On .{5,160}wrote:|From: .*|-----Original Message-----|_{10,}|##- Please (type|enter) your reply above this line -##)",
    re.I,
)


def strip_quoted(text):
    """The author's own words: drop quoted history and signature-style '>' lines."""
    text = text.replace("\r\n", "\n")
    text = QUOTE_SPLIT_RE.split(text, maxsplit=1)[0]
    text = re.sub(r"\n[ \t]*>.*", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_text(folder, uid, account=""):
    """Own-words text of one message (quoted history removed)."""
    _, raw = fetch_raw(folder, uid, account)
    return strip_quoted(message_text(parse_message(raw)))
