"""Cover images without any AI query: embedded cover first, then Open Library / Google Books by ISBN or title."""
import os
import re
import urllib.parse

import requests

from . import calibre

MIN_HEIGHT = 500   # anything smaller looks blurry in Kavita / on e-readers
UA = {"User-Agent": "book-email-importer/1.0 (+https://github.com/emysliwietz/book-email-importer)"}


def _download(url: str, dst: str) -> bool:
    try:
        r = requests.get(url, headers=UA, timeout=30)
        if r.status_code != 200 or len(r.content) < 3000 or not r.headers.get("content-type", "").startswith("image"):
            return False
        tmp = dst + ".dl"
        with open(tmp, "wb") as f:
            f.write(r.content)
        ok = calibre.to_jpeg(tmp, dst)
        os.remove(tmp)
        return ok
    except requests.RequestException:
        return False


def _google_books_url(isbn: str, title: str, author: str) -> str | None:
    q = f"isbn:{isbn}" if isbn else f'intitle:"{title}"' + (f' inauthor:"{author}"' if author else "")
    try:
        r = requests.get("https://www.googleapis.com/books/v1/volumes",
                         params={"q": q, "maxResults": 3, "printType": "books"}, headers=UA, timeout=20)
        for item in r.json().get("items", []) or []:
            links = item.get("volumeInfo", {}).get("imageLinks", {})
            url = links.get("extraLarge") or links.get("large") or links.get("medium") or links.get("thumbnail")
            if url:
                url = url.replace("http://", "https://").replace("&edge=curl", "")
                return re.sub(r"zoom=\d", "zoom=0", url) if "zoom=" in url else url
    except (requests.RequestException, ValueError):
        pass
    return None


def best_cover(book_path: str, meta: dict, workdir: str) -> tuple[str | None, str]:
    """Returns (path to cover.jpg or None, source description)."""
    embedded = os.path.join(workdir, "embedded.jpg")
    has_embedded = (not book_path.lower().endswith(".pdf")) and calibre.extract_cover(book_path, embedded)
    if has_embedded and calibre.image_size(embedded)[1] >= MIN_HEIGHT:
        return embedded, "eingebettetes Cover"

    isbn = re.sub(r"\D", "", meta.get("isbn") or "")
    author = (meta.get("authors") or [""])[0]
    candidates = []
    if isbn:
        candidates.append(("Open Library", f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false"))
    gb = _google_books_url(isbn, meta.get("title", ""), author)
    if gb:
        candidates.append(("Google Books", gb))
    if not isbn and meta.get("title"):
        q = urllib.parse.quote(meta["title"])
        try:
            r = requests.get(f"https://openlibrary.org/search.json?title={q}&author={urllib.parse.quote(author)}&limit=1&fields=cover_i",
                             headers=UA, timeout=20)
            docs = r.json().get("docs", [])
            if docs and docs[0].get("cover_i"):
                candidates.append(("Open Library", f"https://covers.openlibrary.org/b/id/{docs[0]['cover_i']}-L.jpg"))
        except (requests.RequestException, ValueError):
            pass

    best, best_h, src = None, 0, ""
    for name, url in candidates:
        dst = os.path.join(workdir, f"cover_{len(name)}_{best_h}.jpg")
        if _download(url, dst):
            h = calibre.image_size(dst)[1]
            if h > best_h:
                best, best_h, src = dst, h, name
            if h >= MIN_HEIGHT:
                break
    if best and (not has_embedded or best_h > calibre.image_size(embedded)[1]):
        return best, src
    if has_embedded:
        return embedded, "eingebettetes Cover"
    return None, "kein Cover gefunden"
