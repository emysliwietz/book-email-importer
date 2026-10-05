"""A failed import is queued and retried without a second Google AI request; files the failed attempt already
wrote are reported as new (not "already in the library")."""
import os
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from book_importer import calibre, config, covers, gemini, kavita, main, pipeline, retry  # noqa: E402


class FakeServer:
    def __init__(self):
        self.flagged = []

    def add_flags(self, uids, flags):
        self.flagged += uids


def make_mail() -> bytes:
    m = EmailMessage()
    m["From"] = "me@example.org"
    m["To"] = "books@example.org"
    m["Subject"] = "Zwei Bücher"
    m["Message-ID"] = "<x@example.org>"
    m.set_content("hi")
    m.add_attachment(b"first book", maintype="application", subtype="epub+zip", filename="a.epub")
    m.add_attachment(b"second book", maintype="application", subtype="epub+zip", filename="b.epub")
    return m.as_bytes()


def setup(tmp_path, monkeypatch):
    for name in ("LIBRARY_DIR", "WORK_DIR", "CACHE_DIR", "RETRY_DIR"):
        os.makedirs(tmp_path / name, exist_ok=True)
        monkeypatch.setattr(config, name, str(tmp_path / name))
    os.makedirs(tmp_path / "LIBRARY_DIR" / "horror")
    monkeypatch.setattr(config, "RETRY_SCHEDULE_MIN", [5, 15, 60])
    monkeypatch.setattr(config, "TARGET_ADDRESSES", ["books@example.org"])
    monkeypatch.setattr(config, "ALLOWED_SENDERS", ["example.org"])
    monkeypatch.setattr(calibre, "text_excerpt", lambda p, n: ("text", 1))
    monkeypatch.setattr(calibre, "read_meta", lambda p: {})
    monkeypatch.setattr(calibre, "write_meta", lambda *a: None)
    monkeypatch.setattr(calibre, "write_opf", lambda *a: None)
    monkeypatch.setattr(covers, "best_cover", lambda *a: (None, "keins"))
    scanned = []
    monkeypatch.setattr(kavita, "scan_folder", lambda d: scanned.append(d) or True)
    calls = []

    def ask(books, known):
        calls.append(books)
        return [{"index": 0, "work_group": 0, "title": "Alpha", "authors": ["Ann"], "category": "horror",
                 "confidence": 0.9, "tags": [], "language": "en"},
                {"index": 1, "work_group": 1, "title": "The Fisherman", "authors": ["John Langan"],
                 "category": "horror", "confidence": 0.9, "tags": [], "language": "en"}]
    monkeypatch.setattr(gemini, "ask", ask)
    replies = []
    monkeypatch.setattr(main, "reply", lambda msg, results, error=None, note=None: replies.append((results, error, note)))
    return calls, replies, scanned


def test_failed_import_is_retried_without_new_request(tmp_path, monkeypatch):
    calls, replies, scanned = setup(tmp_path, monkeypatch)
    real_makedirs = os.makedirs

    def broken_makedirs(p, *a, **k):
        if "The Fisherman" in str(p):
            raise PermissionError(13, "Permission denied", str(p))
        return real_makedirs(p, *a, **k)
    monkeypatch.setattr(pipeline.os, "makedirs", broken_makedirs)

    server, raw = FakeServer(), make_mail()
    main.handle(server, 42, raw)

    # first attempt: Alpha was written, The Fisherman failed -> queued, one "will retry" reply, mail not flagged
    assert len(calls) == 1
    [entry] = retry.entries()
    assert entry["attempt"] == 1 and entry["placed"] == ["horror/Alpha - Ann/Alpha - Ann.epub"]
    assert len(replies) == 1 and "PermissionError" in replies[0][1] and "in 5 Minuten" in replies[0][2]
    assert server.flagged == []

    # not due yet
    main.run_retries(server)
    assert retry.entries() and len(calls) == 1

    # second attempt still fails -> rescheduled with the next gap, no extra mail
    entry["next_at"] = 0
    retry.save(entry)
    main.run_retries(server)
    [entry] = retry.entries()
    assert entry["attempt"] == 2 and len(replies) == 1 and len(calls) == 1

    # permissions fixed -> retry succeeds from cache
    monkeypatch.setattr(pipeline.os, "makedirs", real_makedirs)
    entry["next_at"] = 0
    retry.save(entry)
    main.run_retries(server)
    assert len(calls) == 1, "a retry must not ask Google AI again"
    assert retry.entries() == [] and server.flagged == [42]
    results, error, note = replies[-1]
    assert error is None and "Versuch 3" in note
    alpha = next(r for r in results if r["title"] == "Alpha")
    assert alpha["files"] == [("a.epub", "neu: Alpha - Ann.epub")]
    assert alpha["new_files"] == ["horror/Alpha - Ann/Alpha - Ann.epub"]
    assert alpha["folder_existed"] is False
    fisher = next(r for r in results if r["title"] == "The Fisherman")
    assert fisher["files"] == [("b.epub", "neu: The Fisherman - John Langan.epub")]
    assert any(d.endswith("Alpha - Ann") for d in scanned)


def test_gives_up_after_schedule(tmp_path, monkeypatch):
    calls, replies, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(pipeline, "process", lambda *a: (_ for _ in ()).throw(OSError("NAS weg")))
    server = FakeServer()
    main.handle(server, 7, make_mail())
    # keeps retrying past the end of the schedule (last gap repeats) ...
    for _ in range(5):
        [entry] = retry.entries()
        entry["next_at"] = 0
        retry.save(entry)
        main.run_retries(server)
    [entry] = retry.entries()
    assert entry["attempt"] == 6 and len(replies) == 1 and server.flagged == []
    # ... until a week after the first failure, then complains once and gives up
    entry["next_at"], entry["created"] = 0, entry["created"] - 8 * 86400
    retry.save(entry)
    main.run_retries(server)
    assert retry.entries() == [] and server.flagged == [7]
    assert len(replies) == 2 and "Beschwerde: Seit 8 Tage" in replies[-1][2] and "7 Versuchen" in replies[-1][2]
    assert os.listdir(config.RETRY_DIR) == []


def test_fresh_mail_with_same_files_asks_again(tmp_path, monkeypatch):
    calls, _, _ = setup(tmp_path, monkeypatch)
    server = FakeServer()
    main.handle(server, 1, make_mail())
    main.handle(server, 2, make_mail())
    assert len(calls) == 2


def test_schedule_and_wording(monkeypatch):
    monkeypatch.setattr(config, "RETRY_SCHEDULE_MIN", [5, 15, 60, 180, 360, 720])
    monkeypatch.setattr(config, "RETRY_MAX_DAYS", 7)
    t0 = 1_000_000.0
    assert [retry.delay_after(n, t0, t0) for n in range(0, 9)] == [None, 5, 15, 60, 180, 360, 720, 720, 720]
    assert retry.delay_after(20, t0, t0 + 7 * 86400 - 600) == 10          # last attempt lands exactly at a week
    assert retry.delay_after(21, t0, t0 + 7 * 86400) is None
    assert retry.human(5) == "5 Minuten" and retry.human(60) == "1 Stunde" and retry.human(1340) == "22,3 Stunden"
