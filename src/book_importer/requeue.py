"""Put a mail (again) into the retry queue, e.g. one whose import failed before retries existed.

    python -m book_importer.requeue            list the newest mails in the inbox with their UID
    python -m book_importer.requeue UID [UID]  queue these mails; the running importer picks them up within
                                               IDLE_TIMEOUT seconds
"""
import email
import email.policy
import sys
import time

from . import config, retry
from .main import connect


def main() -> None:
    server = connect()
    try:
        if len(sys.argv) < 2:
            uids = sorted(server.search(["ALL"]))[-20:]
            for uid, d in sorted(server.fetch(uids, ["BODY.PEEK[HEADER]"]).items()):
                h = email.message_from_bytes(d[b"BODY[HEADER]"], policy=email.policy.default)
                print(f"{uid:>7}  {h.get('date', '')[:25]:25}  {str(h.get('from', ''))[:30]:30}  {h.get('subject', '')}")
            return
        for arg in sys.argv[1:]:
            uid = int(arg)
            raw = server.fetch([uid], ["BODY.PEEK[]"]).get(uid, {}).get(b"BODY[]")
            if not raw:
                print(f"✗ UID {uid}: not found")
                continue
            # notified=True: the sender already got the error mail; the next mail they get is the final result
            entry = retry.add(raw, uid, attempt=0, next_at=time.time(), placed=[], notified=True, error=None)
            print(f"✓ UID {uid} queued as {entry['id']} - imported within {config.IDLE_TIMEOUT}s")
    finally:
        server.logout()


if __name__ == "__main__":
    main()
