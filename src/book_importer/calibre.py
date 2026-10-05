"""Thin wrappers around calibre's command line tools (ebook-meta, ebook-convert) and poppler."""
import json
import os
import re
import subprocess
import tempfile
import zipfile
from html import unescape

from . import config

TIMEOUT = 600


def _run(args: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def read_meta(path: str) -> dict:
    """Embedded metadata as calibre sees it (title, authors, isbn, ...)."""
    r = _run(["ebook-meta", path], timeout=120)
    meta = {}
    for line in r.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if v and k in ("title", "author(s)", "publisher", "tags", "languages", "published", "identifiers",
                           "series", "comments"):
                meta[k] = v[:600]
    return meta


def extract_cover(path: str, out_jpg: str) -> bool:
    r = _run(["ebook-meta", path, f"--get-cover={out_jpg}"], timeout=120)
    return r.returncode == 0 and os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 2000


def convert(src: str, dst: str) -> bool:
    r = _run(["ebook-convert", src, dst])
    if r.returncode != 0:
        print(f"⚠️ ebook-convert failed for {os.path.basename(src)}: {r.stderr[-400:]}")
    return r.returncode == 0 and os.path.exists(dst)


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", unescape(s)).strip()


def text_excerpt(path: str, limit: int) -> tuple[str, int | None]:
    """Beginning of the book's text (title page, copyright page) + page count for PDFs."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        pages = None
        info = _run(["pdfinfo", path], timeout=60).stdout
        m = re.search(r"Pages:\s+(\d+)", info)
        if m:
            pages = int(m.group(1))
        txt = _run(["pdftotext", "-l", "12", "-layout", path, "-"], timeout=120).stdout
        return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", txt)).strip()[:limit], pages
    if ext == ".epub":
        try:
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
                # spine order is best-effort: sorted names keep title/copyright pages near the front for most books
                opf = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
                if opf:
                    o = z.read(opf).decode("utf-8", "replace")
                    ids = dict(re.findall(r'<item[^>]*id="([^"]+)"[^>]*href="([^"]+)"', o))
                    ids.update({k: v for v, k in re.findall(r'<item[^>]*href="([^"]+)"[^>]*id="([^"]+)"', o)})
                    base = os.path.dirname(opf)
                    spine = [os.path.normpath(os.path.join(base, ids[i])).replace("\\", "/")
                             for i in re.findall(r'<itemref[^>]*idref="([^"]+)"', o) if i in ids]
                    names = [n for n in spine if n in z.namelist()] or names
                out = ""
                for n in names:
                    out += " " + _strip_html(z.read(n).decode("utf-8", "replace"))
                    if len(out) > limit:
                        break
                return out.strip()[:limit], None
        except Exception as e:
            print(f"⚠️ could not read EPUB text: {e}")
            return "", None
    # anything else: let calibre produce plain text
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "x.txt")
        if convert(path, out):
            with open(out, encoding="utf-8", errors="replace") as f:
                return re.sub(r"\s+", " ", f.read(limit * 2)).strip()[:limit], None
    return "", None


def write_meta(path: str, m: dict, cover: str | None) -> bool:
    """Write title/authors/tags/... (and the cover where the format supports it) into the file itself."""
    args = ["ebook-meta", path, "--title", m["full_title"], "--authors", " & ".join(m["authors"]) or "Unknown"]
    if m.get("tags"):
        args += ["--tags", ",".join(m["tags"])]
    for flag, key in (("--publisher", "publisher"), ("--isbn", "isbn"), ("--language", "language"),
                      ("--date", "published"), ("--comments", "description"), ("--series", "series")):
        if m.get(key):
            args += [flag, str(m[key])]
    if m.get("series") and m.get("series_index") not in (None, ""):
        args += ["--index", str(m["series_index"])]
    if cover and not path.lower().endswith(".pdf"):
        args += ["--cover", cover]
    r = _run(args, timeout=300)
    if r.returncode != 0:
        print(f"⚠️ ebook-meta could not write metadata: {r.stderr[-300:]}")
    return r.returncode == 0


def write_opf(path: str, opf_out: str) -> None:
    _run(["ebook-meta", path, f"--to-opf={opf_out}"], timeout=120)


def image_size(path: str) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return 0, 0


def to_jpeg(src: str, dst: str) -> bool:
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.convert("RGB").save(dst, "JPEG", quality=90)
        return True
    except Exception as e:
        print(f"⚠️ cover conversion failed: {e}")
        return False


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
