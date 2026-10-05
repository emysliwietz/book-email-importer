"""From attachment files to finished, tagged books in the library (one Gemini query for all of them)."""
import hashlib
import os
import re
import shutil
import tempfile
import threading
import zipfile

from . import calibre, config, covers, gemini, kavita

FORBIDDEN = re.compile(r'[\\/*?"<>|\x00-\x1f]')


# ---------- helpers ----------
def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_name(s: str, limit: int = 140) -> str:
    s = s.replace(":", " -").replace("–", "-")
    s = FORBIDDEN.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return s[:limit].rstrip(" .-")


def base_name(title: str, authors: list[str]) -> str:
    authors = [a for a in authors if a]
    who = " & ".join(authors) if len(authors) <= 3 else f"{authors[0]} et al"
    return clean_name(f"{title} - {who}" if who else title)


def categories() -> list[str]:
    """Existing category folders: every top-level folder plus sub-folders that themselves hold book folders."""
    out = []
    root = config.LIBRARY_DIR
    for top in sorted(os.listdir(root)):
        p = os.path.join(root, top)
        if top.startswith(".") or top in config.CATEGORY_EXCLUDE or not os.path.isdir(p):
            continue
        out.append(top)
        for sub in sorted(os.listdir(p)):
            sp = os.path.join(p, sub)
            if sub.startswith(".") or not os.path.isdir(sp) or len(sub) > 30:
                continue
            entries = os.listdir(sp)
            subdirs = [e for e in entries if os.path.isdir(os.path.join(sp, e))]
            books_here = [e for e in entries if os.path.splitext(e)[1].lower() in config.BOOK_EXTENSIONS]
            if len(subdirs) >= 2 and not books_here:
                out.append(f"{top}/{sub}")
    return out


def safe_category(cat: str, known: list[str]) -> str:
    parts = [clean_name(p, 60) for p in (cat or "").replace("\\", "/").split("/") if p.strip() and p.strip() != ".."]
    cat = "/".join(p for p in parts if p) or "Unsorted"
    if cat in known:
        return cat
    low = {k.lower(): k for k in known}
    if cat.lower() in low:
        return low[cat.lower()]
    return cat if config.ALLOW_NEW_CATEGORIES else "Unsorted"


def unpack(files: list[str], workdir: str) -> list[str]:
    """Keep book files; open ZIPs (that aren't comics) and take the book files inside."""
    out = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in config.BOOK_EXTENSIONS:
            out.append(f)
        elif ext == ".zip":
            try:
                with zipfile.ZipFile(f) as z:
                    for n in z.namelist():
                        if os.path.splitext(n)[1].lower() in config.BOOK_EXTENSIONS and not n.startswith("__MACOSX"):
                            dst = os.path.join(workdir, "unzipped", clean_name(os.path.basename(n)))
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            with z.open(n) as src, open(dst, "wb") as d:
                                shutil.copyfileobj(src, d)
                            out.append(dst)
            except zipfile.BadZipFile:
                print(f"⚠️ broken zip: {os.path.basename(f)}")
    return out


def _place(src: str, target_dir: str, name: str) -> tuple[str, str]:
    """Copy src to target_dir/name atomically. Returns (final path, status) - status 'new'|'duplicate'.
    Same title + author + format already in the library = duplicate (the existing file is never overwritten)."""
    dst = os.path.join(target_dir, name)
    status = "new"
    if os.path.exists(dst):
        return dst, "duplicate"
    part = dst + ".part"
    shutil.copyfile(src, part)
    os.chmod(part, config.FILE_MODE)
    os.replace(part, dst)
    return dst, status


# ---------- main entry ----------
def process(files: list[str]) -> list[dict]:
    """files: saved attachments. Returns one result dict per book (for logging and the reply email)."""
    results = []
    with tempfile.TemporaryDirectory(dir=config.WORK_DIR) as work:
        books = unpack(files, work)
        if not books:
            return results

        # 1. prepare every book locally: convert for Kavita if needed, read metadata + first pages
        prepared = []
        for i, src in enumerate(books):
            ext = os.path.splitext(src)[1].lower()
            bdir = os.path.join(work, f"b{i}")
            os.makedirs(bdir)
            # always work on our own copy - metadata is written into the file before it goes to the library
            work_copy = os.path.join(bdir, "book" + ext)
            shutil.copyfile(src, work_copy)
            primary, originals = work_copy, []
            src_name = os.path.basename(src)
            if ext in config.CONVERTIBLE and config.CONVERT_TO_EPUB:
                epub = os.path.join(bdir, "converted.epub")
                if calibre.convert(work_copy, epub):
                    primary = epub
                    if config.KEEP_ORIGINAL:
                        originals.append(work_copy)
            excerpt, pages = calibre.text_excerpt(primary, config.EXCERPT_CHARS)
            prepared.append({"src": src, "primary": primary, "originals": originals, "dir": bdir})
            prepared[-1]["ask"] = {
                "index": i, "file_name": src_name, "format": ext.lstrip("."),
                "size_mb": round(os.path.getsize(work_copy) / 1e6, 1), "pages": pages,
                "embedded_metadata": calibre.read_meta(primary), "text_excerpt": excerpt,
            }

        # 2. ONE Gemini request for all books of this email
        known = categories()
        answers = {a.get("index"): a for a in gemini.ask([p["ask"] for p in prepared], known)}

        # 3. apply metadata, cover, file into the library, rescan in Kavita
        for i, p in enumerate(prepared):
            a = answers.get(i)
            name = p["ask"]["file_name"]
            if not a:
                results.append({"file": name, "ok": False, "error": "keine Antwort von Google AI"})
                continue
            if a.get("not_a_book"):
                results.append({"file": name, "ok": False, "error": "kein Buch (laut Google AI) - ignoriert"})
                continue
            title = (a.get("title") or "").strip() or os.path.splitext(name)[0]
            authors = [x.strip() for x in a.get("authors") or [] if x and x.strip()][:4]
            meta = dict(a, title=title, authors=authors,
                        full_title=title + (f": {a['subtitle'].strip()}" if (a.get("subtitle") or "").strip() else ""),
                        isbn=re.sub(r"[^\dX]", "", (a.get("isbn") or "").upper()),
                        tags=[t.strip() for t in a.get("tags") or [] if t.strip()][:8])
            category = safe_category(a.get("category"), known)
            base = base_name(title, authors)
            target_dir = os.path.join(config.LIBRARY_DIR, *category.split("/"), base)

            cover, cover_src = covers.best_cover(p["primary"], meta, p["dir"])
            files_out, statuses = [], []
            for f in [p["primary"], *p["originals"]]:
                calibre.write_meta(f, meta, cover)
                os.makedirs(target_dir, mode=config.DIR_MODE, exist_ok=True)
                dst, st = _place(f, target_dir, base + os.path.splitext(f)[1].lower())
                files_out.append(dst)
                statuses.append(st)
            if config.WRITE_SIDECARS:
                if cover:
                    shutil.copyfile(cover, os.path.join(target_dir, "cover.jpg"))
                calibre.write_opf(files_out[0], os.path.join(target_dir, "metadata.opf"))
            for root, dirs, fs in os.walk(target_dir):
                for x in fs:
                    try:
                        os.chmod(os.path.join(root, x), config.FILE_MODE)
                    except OSError:
                        pass

            if all(s == "duplicate" for s in statuses):
                results.append({"file": name, "ok": True, "duplicate": True, "base": base, "category": category})
                continue
            scanned = kavita.scan_folder(target_dir)
            if scanned and cover and files_out[0].lower().endswith(".pdf"):
                # PDFs can't carry a cover - give it to Kavita directly once the scan has picked the book up
                keep = os.path.join(config.WORK_DIR, f"cover_{os.getpid()}_{i}.jpg")
                shutil.copyfile(cover, keep)
                threading.Thread(target=lambda t=title, fn=os.path.basename(files_out[0]), c=keep:
                                 (kavita.set_cover(t, fn, c), os.remove(c)), daemon=True).start()
            results.append({
                "file": name, "ok": True, "base": base, "category": category, "new_category": category not in known,
                "files": [os.path.relpath(x, config.LIBRARY_DIR) for x in files_out], "statuses": statuses,
                "authors": authors, "title": meta["full_title"], "isbn": meta["isbn"], "tags": meta["tags"],
                "language": a.get("language"), "published": a.get("published"), "publisher": a.get("publisher"),
                "confidence": a.get("confidence"), "cover": cover_src, "kavita": scanned,
            })
    return results
