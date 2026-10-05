"""All configuration comes from environment variables (see README)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# --- mailbox ---
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.strato.de")
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
# Recipient address(es) that trigger an import: comma-separated; "*" = every mail in this mailbox.
TARGET_ADDRESSES = [a.lower() for a in (_list("TARGET_ADDRESS") or [EMAIL_ACCOUNT]) if a]
TARGET_ADDRESS = ", ".join(TARGET_ADDRESSES)   # for log output
# Comma-separated addresses ("me@x.de") and/or domains ("@x.de" or "x.de"). Empty = accept every sender.
ALLOWED_SENDERS = [s.lower() for s in _list("ALLOWED_SENDERS")]
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "300"))
STATE_FILE = os.getenv("STATE_FILE", "/data/last_seen_uid.txt")
WORK_DIR = os.getenv("WORK_DIR", "/data/work")
DELETE_AFTER_IMPORT = _bool("DELETE_AFTER_IMPORT", False)
# Failed imports (e.g. library not writable, network error) are tried again. Minutes to wait after each failed
# attempt; the last gap repeats until RETRY_MAX_DAYS after the first failure, then the sender gets a complaint
# mail and the mail is given up. Empty = no retries.
RETRY_SCHEDULE_MIN = [float(x) for x in _list("RETRY_SCHEDULE_MIN", "5,15,60,180,360,720")]
RETRY_MAX_DAYS = float(os.getenv("RETRY_MAX_DAYS", "7"))
RETRY_DIR = os.getenv("RETRY_DIR", "/data/retry")
# Google AI answers are kept here so a retry doesn't ask again (entries older than CACHE_DAYS are removed).
CACHE_DIR = os.getenv("CACHE_DIR", "/data/cache")
CACHE_DAYS = float(os.getenv("CACHE_DAYS", "7"))

# --- replies (optional; defaults reuse the mailbox credentials) ---
REPLY_ENABLED = _bool("REPLY_ENABLED", True)
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.strato.de")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SENDER_EMAIL = os.getenv("SMTP_SENDER_EMAIL") or EMAIL_ACCOUNT
SMTP_SENDER_PASSWORD = os.getenv("SMTP_SENDER_PASSWORD") or EMAIL_PASSWORD

# --- Google AI (Gemini) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_USE_SEARCH = _bool("GEMINI_USE_SEARCH", True)
EXCERPT_CHARS = int(os.getenv("EXCERPT_CHARS", "6000"))

# --- library ---
LIBRARY_DIR = os.getenv("LIBRARY_DIR", "/books/ebooks")
CATEGORY_EXCLUDE = _list("CATEGORY_EXCLUDE", "calibre_stash")
ALLOW_NEW_CATEGORIES = _bool("ALLOW_NEW_CATEGORIES", True)
CONVERT_TO_EPUB = _bool("CONVERT_TO_EPUB", True)       # MOBI/AZW3/FB2/... -> EPUB (Kavita can't read those)
KEEP_ORIGINAL = _bool("KEEP_ORIGINAL", True)           # keep the original file next to the converted EPUB
WRITE_SIDECARS = _bool("WRITE_SIDECARS", True)         # cover.jpg + metadata.opf next to the book (calibre style)
FILE_MODE = int(os.getenv("FILE_MODE", "664"), 8)
DIR_MODE = int(os.getenv("DIR_MODE", "775"), 8)

# --- Kavita ---
KAVITA_URL = os.getenv("KAVITA_URL", "http://kavita:5000").rstrip("/")
KAVITA_API_KEY = os.getenv("KAVITA_API_KEY", "")
# The same library folder as Kavita sees it (only differs if Kavita mounts the books elsewhere)
KAVITA_LIBRARY_DIR = os.getenv("KAVITA_LIBRARY_DIR", LIBRARY_DIR)

BOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".fb2", ".djvu", ".lit", ".pdb", ".rtf", ".cbz", ".cbr"}
KAVITA_NATIVE = {".epub", ".pdf", ".cbz", ".cbr"}
CONVERTIBLE = {".mobi", ".azw", ".azw3", ".fb2", ".lit", ".pdb", ".rtf"}


def check() -> list[str]:
    missing = [n for n, v in (("EMAIL_ACCOUNT", EMAIL_ACCOUNT), ("EMAIL_PASSWORD", EMAIL_PASSWORD),
                               ("GEMINI_API_KEY", GEMINI_API_KEY)) if not v]
    return missing
