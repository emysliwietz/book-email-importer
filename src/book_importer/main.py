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

from . import config, pipeline, retry

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


def reply_text(results: list[dict], error: str | None = None, note: str | None = None) -> str:
    lines = []
    if error:
        lines.append(f"Beim Import ist ein Fehler aufgetreten:\n{error}\n")
    if note:
        lines.append(note)
    for r in results:
        if not r.get("ok"):
            lines += [f"✗ {name}: {status}" for name, status in r["files"]]
            continue
        nothing_new = not r.get("new_files")
        head = "=" if nothing_new else "✓"
        block = [f"{head} {r['title']} – {', '.join(r['authors']) or 'unbekannt'}",
                 f"   Ordner: {r['category']}/{r['base']}"
                 + (" (Kategorie neu angelegt)" if r.get("new_category") else "")
                 + (" (Ordner gab es schon)" if r.get("folder_existed") else "")]
        if r.get("editions"):
            block.append(f"   Ausgaben: {', '.join(r['editions'])}")
        block += [f"   • {name}: {status}" for name, status in r["files"]]
        if not nothing_new:
            block += [f"   ISBN: {r.get('isbn') or '-'} | {r.get('publisher') or '-'} {r.get('published') or ''}"
                      f" | Sprache: {r.get('language') or '-'}",
                      f"   Tags: {', '.join(r.get('tags') or [])}",
                      f"   Cover: {r.get('cover')} | Kavita: {'neu eingelesen' if r.get('kavita') else 'nicht erreicht'}"]
        lines.append("\n".join(block))
    if not lines or (note and not error and not results):
        lines.append("In dieser E-Mail wurde kein E-Book und keine PDF gefunden.")
    return "\n\n".join(lines) + "\n\nDies ist eine automatisch generierte E-Mail. Beep. Boop.\n"


def reply(msg, results: list[dict], error: str | None = None, note: str | None = None) -> None:
    if not config.REPLY_ENABLED:
        return
    to = [a for _, a in getaddresses(msg.get_all("reply-to", []) or msg.get_all("from", []))]
    if not to:
        return
    out = EmailMessage()
    out["From"] = config.SMTP_SENDER_EMAIL
    out["To"] = ", ".join(to)
    out["Subject"] = "Re: " + (msg.get("subject") or "Buch-Import")
    if msg.get("message-id"):
        out["In-Reply-To"] = msg["message-id"]
        out["References"] = msg["message-id"]
    out["Message-ID"] = make_msgid()
    out.set_content(reply_text(results, error, note))
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
    if "*" not in config.TARGET_ADDRESSES and not any(t in rcpts for t in config.TARGET_ADDRESSES):
        return
    senders = [a for _, a in getaddresses(msg.get_all("from", []))]
    if not sender_allowed(senders):
        print(f"❌ UID {uid}: sender {senders} not allowed - ignored")
        return
    print(f"\n📨 UID {uid} from {senders}: {msg.get('subject')}")
    attempt(server, uid, raw, msg, None)


def import_mail(msg, placed: list[str], placed_before: list[str], use_cache: bool) -> tuple[list[dict], str | None]:
    results, error = [], None
    try:
        os.makedirs(config.WORK_DIR, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=config.WORK_DIR) as td:
            files = save_attachments(msg, td)
            print(f"📎 {len(files)} candidate attachment(s): {[os.path.basename(f) for f in files]}")
            results = pipeline.process(files, placed, placed_before, use_cache) if files else []
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    for r in results:
        print(("✅" if r.get("ok") else "⚠️"), r)
    return results, error


def attempt(server: IMAPClient, uid: int | None, raw: bytes, msg, entry: dict | None) -> None:
    """One import attempt of a mail - the first one (entry None) or a retry from the queue."""
    placed_before = list(entry["placed"]) if entry else []
    placed = list(placed_before)                       # grows while files land in the library
    results, error = import_mail(msg, placed, placed_before, use_cache=entry is not None)
    failed = (entry["attempt"] if entry else 0) + (1 if error else 0)

    if error:
        delay = retry.delay_after(failed)
        if delay is not None:
            try:
                next_at = time.time() + delay * 60
                if entry:
                    entry.update(attempt=failed, next_at=next_at, placed=placed, last_error=error)
                    retry.save(entry)
                else:
                    entry = retry.add(raw, uid, failed, next_at, placed, notified=False, error=error)
                print(f"🔁 Retry {failed} of {len(config.RETRY_SCHEDULE_MIN)} in {retry.human(delay)} ({entry['id']})")
                if not entry["notified"]:
                    left = config.RETRY_SCHEDULE_MIN[failed - 1:]
                    more = (f", danach bei Bedarf noch {len(left) - 1} weitere Male (insgesamt etwa "
                            f"{retry.human(sum(left))} lang)") if len(left) > 1 else ""
                    reply(msg, [], error, note=(
                        f"Es wird automatisch noch einmal versucht: in {retry.human(delay)}{more}. "
                        "Eine weitere E-Mail folgt, sobald es geklappt hat oder aufgegeben wird."))
                    entry["notified"] = True
                    retry.save(entry)
                return
            except Exception:
                traceback.print_exc()                 # queue not usable - fall through to the final reply

    # final: success, or no retries left
    note = None
    if entry and not error:
        note = "Der erneute Versuch hat geklappt" + (f" (Versuch {failed + 1})." if failed else ".")
    elif error and failed > 1:
        note = (f"Auch nach {failed} Versuchen ging es nicht - es wird nicht weiter versucht. "
                "Bitte die E-Mail noch einmal schicken, wenn das Problem behoben ist.")
    if error and placed:
        note = (note + "\n" if note else "") + "Schon abgelegt wurden:\n" + "\n".join(f"   • {p}" for p in placed)
    reply(msg, results, error, note)
    if entry:
        retry.remove(entry)
    if uid is None:
        return
    try:
        if config.DELETE_AFTER_IMPORT and not error:
            server.delete_messages([uid])
            server.expunge()
        else:
            server.add_flags([uid], [b"\\Seen"])
    except Exception as e:
        print(f"⚠️ UID {uid}: could not flag/delete the mail: {e}")


def run_retries(server: IMAPClient) -> None:
    for entry in retry.due():
        if shutdown:
            break
        try:
            raw = retry.raw(entry)
        except OSError as e:
            print(f"⚠️ retry {entry['id']}: mail file missing ({e}) - dropped")
            retry.remove(entry)
            continue
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        print(f"\n🔁 Retry {entry['id']} (UID {entry.get('uid')}, after {entry['attempt']} failed): {msg.get('subject')}")
        try:
            attempt(server, entry.get("uid"), raw, msg, entry)
        except Exception:
            traceback.print_exc()


def idle_timeout() -> float:
    nxt = retry.next_due()
    return config.IDLE_TIMEOUT if nxt is None else max(1, min(config.IDLE_TIMEOUT, nxt - time.time()))


def connect() -> IMAPClient:
    s = IMAPClient(config.IMAP_SERVER, ssl=True, timeout=120)
    s.login(config.EMAIL_ACCOUNT, config.EMAIL_PASSWORD)
    s.select_folder("INBOX")
    print(f"📬 Connected to {config.IMAP_SERVER} as {config.EMAIL_ACCOUNT}")
    return s


def run_once(server: IMAPClient, last_uid: int) -> int:
    if not os.path.exists(config.STATE_FILE):
        # First start (no state file yet): don't import the mailbox history, only what arrives from now on.
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
    pending = retry.entries()
    if pending:
        print(f"🔁 {len(pending)} mail(s) waiting for a retry")
    last_uid = load_state()
    while not shutdown:
        try:
            server = connect()
            last_uid = run_once(server, last_uid)
            run_retries(server)
            while not shutdown:
                server.idle()
                server.idle_check(timeout=idle_timeout())
                server.idle_done()
                last_uid = run_once(server, last_uid)
                run_retries(server)
            server.logout()
        except Exception as e:
            print(f"⚠️ Connection problem: {e} - reconnecting in 30s")
            for _ in range(30):
                if shutdown:
                    break
                time.sleep(1)


if __name__ == "__main__":
    main()
