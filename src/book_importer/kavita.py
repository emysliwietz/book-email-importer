"""Kavita: rescan only the folder of the new book, optionally set a cover (needed for PDFs)."""
import base64
import os
import time

import requests

from . import config


def library_path(local_path: str) -> str:
    """Translate a path inside this container to the same path as Kavita sees it."""
    rel = os.path.relpath(local_path, config.LIBRARY_DIR)
    return os.path.join(config.KAVITA_LIBRARY_DIR, rel)


def scan_folder(local_dir: str) -> bool:
    if not config.KAVITA_API_KEY:
        print("ℹ️ KAVITA_API_KEY not set - skipping Kavita scan")
        return False
    folder = library_path(local_dir)
    try:
        r = requests.post(f"{config.KAVITA_URL}/api/Library/scan-folder",
                          json={"apiKey": config.KAVITA_API_KEY, "folderPath": folder}, timeout=30)
        ok = r.status_code == 200
        print(f"{'🔄' if ok else '⚠️'} Kavita scan-folder {folder}: HTTP {r.status_code} {'' if ok else r.text[:200]}")
        return ok
    except requests.RequestException as e:
        print(f"⚠️ Kavita not reachable: {e}")
        return False


def _token() -> str | None:
    r = requests.post(f"{config.KAVITA_URL}/api/Plugin/authenticate",
                      params={"apiKey": config.KAVITA_API_KEY, "pluginName": "book-email-importer"}, timeout=30)
    return r.json().get("token") if r.status_code == 200 else None


def set_cover(title: str, file_name: str, cover_jpg: str, wait_s: int = 240) -> bool:
    """After the scan, find the new series by its file name and upload our cover (locked)."""
    if not (config.KAVITA_API_KEY and cover_jpg and os.path.exists(cover_jpg)):
        return False
    try:
        tok = _token()
        if not tok:
            print("⚠️ Kavita plugin login failed - cover not set")
            return False
        h = {"Authorization": f"Bearer {tok}"}
        deadline = time.time() + wait_s
        series_id = None
        while time.time() < deadline and series_id is None:
            r = requests.get(f"{config.KAVITA_URL}/api/Search/search", headers=h,
                             params={"queryString": title[:60], "includeChapterAndFiles": "true"}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                for f in d.get("files", []) or []:
                    if os.path.basename(f.get("filePath", "")) == file_name:
                        series_id = f.get("seriesId")
                        break
                if series_id is None:
                    for s in d.get("series", []) or []:
                        if (s.get("name") or "").lower() == title.lower():
                            series_id = s.get("seriesId")
                            break
            if series_id is None:
                time.sleep(15)
        if series_id is None:
            print("⚠️ new series not found in Kavita (yet) - cover not set")
            return False
        with open(cover_jpg, "rb") as f:
            b64 = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
        r = requests.post(f"{config.KAVITA_URL}/api/Upload/series", headers=h,
                          json={"id": series_id, "url": b64, "lockCover": True}, timeout=60)
        print(f"{'🖼️' if r.status_code == 200 else '⚠️'} Kavita cover for series {series_id}: HTTP {r.status_code}")
        return r.status_code == 200
    except requests.RequestException as e:
        print(f"⚠️ Kavita cover upload failed: {e}")
        return False
