"""From attachment files to finished, tagged books in the library (one Gemini query for all of them)."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
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


# ---------- Google AI answer cache (a retry of the same mail must not ask again) ----------
def _cache_path(items: list[dict]) -> str:
    key = hashlib.sha256("\n".join(sha256(it["work_copy"]) for it in items).encode()).hexdigest()
    return os.path.join(config.CACHE_DIR, key + ".json")


def _cache_prune() -> None:
    cutoff = time.time() - config.CACHE_DAYS * 86400
    for f in os.listdir(config.CACHE_DIR):
        p = os.path.join(config.CACHE_DIR, f)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def ask_cached(items: list[dict], known: list[str], use_cache: bool) -> list[dict]:
    """The single Gemini request of a mail. Every answer is stored right away (before anything is written to the
    library, which is where imports usually fail); only retries read it back."""
    path = _cache_path(items)
    if use_cache:
        try:
            with open(path) as f:
                answers = json.load(f)
            print("🗃️ Google AI answer taken from cache - no new request")
            return answers
        except (OSError, ValueError):
            pass
    answers = gemini.ask([it["ask"] for it in items], known)
    try:
        os.makedirs(config.CACHE_DIR, exist_ok=True)
        _cache_prune()
        with open(path + ".tmp", "w") as f:
            json.dump(answers, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except OSError as e:
        print(f"⚠️ Could not cache the Google AI answer: {e}")
    return answers


# ---------- grouping helpers ----------
CONVERT_PREFERENCE = [".azw3", ".mobi", ".azw", ".fb2", ".lit", ".pdb", ".rtf"]   # best source for an EPUB first


def edition_suffix(label: str, year: str, n: int) -> str:
    label = re.sub(r"\s+", " ", FORBIDDEN.sub("", (label or "").replace(":", " -"))).strip()[:40]
    year = (year or "")[:4] if re.match(r"^\d{4}", year or "") else ""
    parts = [p for p in (label, year) if p]
    return f" ({', '.join(parts)})" if parts else f" (Version {n})"


def _best(files: list[dict]) -> dict:
    """Among several copies of the same edition+format keep the largest (usually the most complete) one."""
    return max(files, key=lambda f: os.path.getsize(f["work_copy"]))


# ---------- main entry ----------
def process(files: list[str], placed: list[str] | None = None, placed_before=(), use_cache: bool = False) -> list[dict]:
    """files: saved attachments. Returns one result dict per WORK (for logging and the reply email).
    placed: every file written to the library is appended here (relative path) as soon as it is there, so a failed
    attempt still knows what it already did. placed_before: those paths from earlier attempts of the same mail -
    they are reported as new, not as "already in the library". use_cache: reuse a stored Google AI answer (retries)."""
    placed = [] if placed is None else placed
    placed_before = set(placed_before)
    results = []
    with tempfile.TemporaryDirectory(dir=config.WORK_DIR) as work:
        books = unpack(files, work)
        if not books:
            return results

        # 1. read every attachment (no conversion yet - that is decided per edition later)
        items = []
        for i, src in enumerate(books):
            ext = os.path.splitext(src)[1].lower()
            bdir = os.path.join(work, f"b{i}")
            os.makedirs(bdir)
            work_copy = os.path.join(bdir, "book" + ext)       # never touch the attachment itself
            shutil.copyfile(src, work_copy)
            excerpt, pages = calibre.text_excerpt(work_copy, config.EXCERPT_CHARS)
            items.append({"index": i, "name": os.path.basename(src), "ext": ext, "work_copy": work_copy, "dir": bdir,
                          "ask": {"index": i, "file_name": os.path.basename(src), "format": ext.lstrip("."),
                                  "size_mb": round(os.path.getsize(work_copy) / 1e6, 1), "pages": pages,
                                  "embedded_metadata": calibre.read_meta(work_copy), "text_excerpt": excerpt}})

        # 2. ONE Gemini request for all attachments of this email (also says which files are the same work/edition)
        known = categories()
        answers = {a.get("index"): a for a in ask_cached(items, known, use_cache)}

        groups: dict[int, list[dict]] = {}
        for it in items:
            a = answers.get(it["index"])
            if not a:
                results.append({"ok": False, "files": [(it["name"], "keine Antwort von Google AI")]})
                continue
            if a.get("not_a_book"):
                results.append({"ok": False, "files": [(it["name"], "kein Buch (laut Google AI) - ignoriert")]})
                continue
            it["a"] = a
            g = a.get("work_group")
            groups.setdefault(g if isinstance(g, int) else 10_000 + it["index"], []).append(it)

        # 3. one folder + one consistent set of metadata per work
        for gi, members in enumerate(groups.values()):
            results.append(_import_work(gi, members, known, placed, placed_before))
    return results


def _import_work(gi: int, members: list[dict], known: list[str], placed: list[str], placed_before: set[str]) -> dict:
    # the most confident answer (EPUB/larger file as tie-breaker) defines the shared metadata of the work
    lead = max(members, key=lambda m: (m["a"].get("confidence") or 0, m["ext"] == ".epub",
                                       os.path.getsize(m["work_copy"])))["a"]
    title = (lead.get("title") or "").strip() or os.path.splitext(members[0]["name"])[0]
    authors = [x.strip() for x in lead.get("authors") or [] if x and x.strip()][:4]
    subtitle = (lead.get("subtitle") or "").strip()
    common = {"title": title, "authors": authors, "full_title": title + (f": {subtitle}" if subtitle else ""),
              "series": lead.get("series"), "series_index": lead.get("series_index"),
              "tags": [t.strip() for t in lead.get("tags") or [] if t.strip()][:8],
              "description": lead.get("description"), "language": lead.get("language")}
    category = safe_category(lead.get("category"), known)
    base = base_name(title, authors)
    target_dir = os.path.join(config.LIBRARY_DIR, *category.split("/"), base)
    rel_dir = os.path.relpath(target_dir, config.LIBRARY_DIR) + os.sep
    # a folder that only an earlier attempt of this same mail created doesn't count as "already there"
    existed_before = os.path.isdir(target_dir) and not any(p.startswith(rel_dir) for p in placed_before)

    # editions inside this work (Gemini gives identical labels to files of the same edition)
    editions: dict[str, list[dict]] = {}
    for m in members:
        editions.setdefault(re.sub(r"\s+", " ", (m["a"].get("edition") or "").strip().lower()), []).append(m)
    multi = len(editions) > 1

    file_rows, new_files, covers_used, pdf_only_cover = [], [], [], None
    for n, (ekey, ed_members) in enumerate(editions.items(), start=1):
        ref = max(ed_members, key=lambda m: m["a"].get("confidence") or 0)["a"]
        meta = dict(common, edition=ref.get("edition"), published=ref.get("published"), publisher=ref.get("publisher"),
                    isbn=re.sub(r"[^\dX]", "", (ref.get("isbn") or "").upper()))
        suffix = edition_suffix(ref.get("edition"), ref.get("published"), n) if multi else ""
        name = base + suffix

        # one file per format; extra copies of the same format+edition in this email are skipped
        by_ext: dict[str, list[dict]] = {}
        for m in ed_members:
            by_ext.setdefault(m["ext"], []).append(m)
        outputs = []                                   # (path to write, original attachment name)
        for ext, lst in by_ext.items():
            keep = _best(lst)
            outputs.append((keep["work_copy"], keep["name"]))
            for other in lst:
                if other is not keep:
                    file_rows.append((other["name"], "doppelt in dieser E-Mail (gleiche Ausgabe und Format) - übersprungen"))
        # an EPUB is only generated when no real EPUB of this edition was attached
        if ".epub" not in by_ext and config.CONVERT_TO_EPUB:
            for ext in CONVERT_PREFERENCE:
                if ext in by_ext:
                    src = _best(by_ext[ext])
                    epub = os.path.join(src["dir"], "converted.epub")
                    if calibre.convert(src["work_copy"], epub):
                        outputs.insert(0, (epub, f"{src['name']} → EPUB umgewandelt"))
                        if not config.KEEP_ORIGINAL:
                            outputs = [o for o in outputs if o[0] != src["work_copy"]]
                    break

        primary = next((p for p, _ in outputs if p.endswith(".epub")), outputs[0][0])
        cover, cover_src = covers.best_cover(primary, meta, os.path.dirname(primary))
        covers_used.append(cover_src)
        os.makedirs(target_dir, mode=config.DIR_MODE, exist_ok=True)
        for path, label in outputs:
            calibre.write_meta(path, meta, cover)
            dst, status = _place(path, target_dir, name + os.path.splitext(path)[1].lower())
            rel = os.path.relpath(dst, config.LIBRARY_DIR)
            if status == "new":
                placed.append(rel)
            if status == "duplicate" and rel not in placed_before:
                file_rows.append((label, f"schon in der Bibliothek: {os.path.basename(dst)} - übersprungen"))
            else:
                file_rows.append((label, f"neu: {os.path.basename(dst)}"))
                new_files.append(rel)
        if config.WRITE_SIDECARS and n == 1:
            if cover and not os.path.exists(os.path.join(target_dir, "cover.jpg")):
                shutil.copyfile(cover, os.path.join(target_dir, "cover.jpg"))
            if not os.path.exists(os.path.join(target_dir, "metadata.opf")):
                calibre.write_opf(primary, os.path.join(target_dir, "metadata.opf"))
        if cover and primary.endswith(".pdf") and pdf_only_cover is None:
            keep = os.path.join(config.WORK_DIR, f"cover_{os.getpid()}_{gi}.jpg")
            shutil.copyfile(cover, keep)
            pdf_only_cover = (keep, name + ".pdf")

    for root, _, fs in os.walk(target_dir):
        for x in fs:
            try:
                os.chmod(os.path.join(root, x), config.FILE_MODE)
            except OSError:
                pass

    scanned = kavita.scan_folder(target_dir) if new_files else False
    has_epub = any(f.endswith(".epub") for f in new_files)
    if pdf_only_cover:
        if scanned and not has_epub:
            # PDFs can't carry a cover - give it to Kavita directly once the scan has picked the book up
            c, fn = pdf_only_cover
            threading.Thread(target=lambda: (kavita.set_cover(title, fn, c), os.remove(c)), daemon=True).start()
        else:
            os.remove(pdf_only_cover[0])
    return {"ok": True, "title": common["full_title"], "authors": authors, "base": base, "category": category,
            "new_category": category not in known and not existed_before, "folder_existed": existed_before,
            "editions": [e or "-" for e in editions] if multi else [], "files": file_rows, "new_files": new_files,
            "isbn": ", ".join(sorted({re.sub(r"[^\dX]", "", (m["a"].get("isbn") or "")) for m in members} - {""})),
            "publisher": lead.get("publisher"), "published": lead.get("published"), "language": lead.get("language"),
            "tags": common["tags"], "confidence": lead.get("confidence"), "cover": ", ".join(sorted(set(covers_used))),
            "kavita": scanned}
