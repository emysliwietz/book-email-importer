"""IMAP listener: new mails to TARGET_ADDRESS with ebook/PDF attachments are imported into the library."""
import email
import email.policy
import os
import signal
import smtplib
import sys
import tempfile
import time
import traceback
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid

from imapclient import IMAPClient

from . import config, pipeline

shutdown = False


def _stop(signum, frame):
    global shutdown
    shutdown = True
    print(f"🛑 Shutdown requested (signal {signum})")


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


# ---- state ----
def load_state() -> int:
    try:
        with open(config.STATE_FILE) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def save_state(uid: int) -> None:
    os.makedirs(os.path.dirname(config.STATE_FILE) or ".", exist_ok=True)
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(uid))
    os.replace(tmp, config.STATE_FILE)


# ---- mail helpers ----
def sender_allowed(addresses: list[str]) -> bool:
    if not config.ALLOWED_SENDERS:
        return True
    for a in (x.lower() for x in addresses):
        domain = a.rsplit("@", 1)[-1]
        for rule in config.ALLOWED_SENDERS:
            if "@" in rule.lstrip("@"):          # full address: me@example.de
                if a == rule:
                    return True
            elif domain == rule.lstrip("@"):     # domain: example.de or @example.de
                return True
    return False


def save_attachments(msg, outdir: str) -> list[str]:
    paths = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if not name:
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in config.BOOK_EXTENSIONS and ext != ".zip":
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        safe = pipeline.clean_name(os.path.basename(name), 180) or f"attachment{len(paths)}{ext}"
        p = os.path.join(outdir, safe)
        with open(p, "wb") as f:
            f.write(data)
        paths.append(p)
    return paths


def reply(msg, results: list[dict], error: str | None = None) -> None:
    if not config.REPLY_ENABLED:
        return
    to = [a for _, a in getaddresses(msg.get_all("reply-to", []) or msg.get_all("from", []))]
    if not to:
        return
    lines = []
    if error:
        lines.append(f"Beim Import ist ein Fehler aufgetreten:\n{error}\n")
    for r in results:
        if not r.get("ok"):
            lines.append(f"✗ {r['file']}: {r.get('error')}")
        elif r.get("duplicate"):
            lines.append(f"= {r['file']}: schon vorhanden als „{r['base']}“ in {r['category']}")
        else:
            lines.append(f"✓ {r['title']} – {', '.join(r['authors']) or 'unbekannt'}\n"
                         f"   Ordner: {r['category']}{' (neu angelegt)' if r.get('new_category') else ''}\n"
                         f"   Dateien: {', '.join(os.path.basename(f) for f in r['files'])}\n"
                         f"   ISBN: {r.get('isbn') or '-'} | {r.get('publisher') or '-'} {r.get('published') or ''}"
                         f" | Sprache: {r.get('language') or '-'}\n"
                         f"   Tags: {', '.join(r.get('tags') or [])}\n"
                         f"   Cover: {r.get('cover')} | Kavita: {'neu eingelesen' if r.get('kavita') else 'nicht erreicht'}")
    if not lines:
        lines.append("In dieser E-Mail wurde kein E-Book und keine PDF gefunden.")
    out = EmailMessage()
    out["From"] = config.SMTP_SENDER_EMAIL
    out["To"] = ", ".join(to)
    out["Subject"] = "Re: " + (msg.get("subject") or "Buch-Import")
    if msg.get("message-id"):
        out["In-Reply-To"] = msg["message-id"]
        out["References"] = msg["message-id"]
    out["Message-ID"] = make_msgid()
    out.set_content("\n\n".join(lines) + "\n\nDies ist eine automatisch generierte E-Mail. Beep. Boop.\n")
    try:
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=60) as s:
            s.starttls()
            s.login(config.SMTP_SENDER_EMAIL, config.SMTP_SENDER_PASSWORD)
            s.send_message(out)
        print(f"📤 Reply sent to {', '.join(to)}")
    except Exception as e:
        print(f"⚠️ Could not send reply: {e}")


# ---- processing ----
def handle(server: IMAPClient, uid: int, raw: bytes) -> None:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    rcpts = [a.lower() for _, a in getaddresses(msg.get_all("to", []) + msg.get_all("cc", []) +
                                                 msg.get_all("delivered-to", []) + msg.get_all("x-original-to", []))]
    if config.TARGET_ADDRESS and config.TARGET_ADDRESS not in rcpts:
        return
    senders = [a for _, a in getaddresses(msg.get_all("from", []))]
    if not sender_allowed(senders):
        print(f"❌ UID {uid}: sender {senders} not allowed - ignored")
        return
    print(f"\n📨 UID {uid} from {senders}: {msg.get('subject')}")
    os.makedirs(config.WORK_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config.WORK_DIR) as td:
        files = save_attachments(msg, td)
        print(f"📎 {len(files)} candidate attachment(s): {[os.path.basename(f) for f in files]}")
        results, error = [], None
        try:
            results = pipeline.process(files) if files else []
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        for r in results:
            print(("✅" if r.get("ok") else "⚠️"), r)
        reply(msg, results, error)
    if config.DELETE_AFTER_IMPORT and not error:
        server.delete_messages([uid])
        server.expunge()
    else:
        server.add_flags([uid], [b"\\Seen"])


def connect() -> IMAPClient:
    s = IMAPClient(config.IMAP_SERVER, ssl=True, timeout=120)
    s.login(config.EMAIL_ACCOUNT, config.EMAIL_PASSWORD)
    s.select_folder("INBOX")
    print(f"📬 Connected to {config.IMAP_SERVER} as {config.EMAIL_ACCOUNT}")
    return s


def run_once(server: IMAPClient, last_uid: int) -> int:
    if last_uid == 0:
        # First start: don't import the whole mailbox history, only what arrives from now on.
        uids = server.search(["ALL"])
        last_uid = max(uids) if uids else 0
        save_state(last_uid)
        print(f"🆕 First start - ignoring {len(uids)} existing message(s), watching from UID {last_uid}")
        return last_uid
    for uid in sorted(u for u in server.search(["UID", f"{last_uid + 1}:*"]) if u > last_uid):
        if shutdown:
            break
        raw = server.fetch([uid], ["BODY.PEEK[]"]).get(uid, {}).get(b"BODY[]")
        if raw:
            try:
                handle(server, uid, raw)
            except Exception:
                traceback.print_exc()
        last_uid = uid
        save_state(last_uid)
    return last_uid


def main() -> None:
    missing = config.check()
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)}")
    if not os.path.isdir(config.LIBRARY_DIR):
        sys.exit(f"LIBRARY_DIR {config.LIBRARY_DIR} does not exist - is the books volume mounted?")
    if not config.ALLOWED_SENDERS:
        print("⚠️ ALLOWED_SENDERS is empty - books from ANY sender will be imported")
    os.makedirs(config.WORK_DIR, exist_ok=True)
    print(f"📚 book-email-importer: {config.TARGET_ADDRESS} -> {config.LIBRARY_DIR} (model {config.GEMINI_MODEL})")
    last_uid = load_state()
    while not shutdown:
        try:
            server = connect()
            last_uid = run_once(server, last_uid)
            while not shutdown:
                server.idle()
                server.idle_check(timeout=config.IDLE_TIMEOUT)
                server.idle_done()
                last_uid = run_once(server, last_uid)
            server.logout()
        except Exception as e:
            print(f"⚠️ Connection problem: {e} - reconnecting in 30s")
            for _ in range(30):
                if shutdown:
                    break
                time.sleep(1)


if __name__ == "__main__":
    main()
