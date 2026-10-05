"""Google AI (Gemini): ONE request per email, covering every book attached to it.

The request carries everything the model needs (file name, embedded metadata, text of the first pages, the list
of existing library categories) and asks for strict JSON, so there are no follow-up or retry-for-format queries.
Google Search grounding is enabled in the same request to verify ISBN/publisher/year.
"""
import json
import time

import requests

from . import config

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

BOOK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "index": {"type": "INTEGER", "description": "index of the book in the input list"},
        "title": {"type": "STRING", "description": "main title only, no subtitle, correct capitalisation"},
        "subtitle": {"type": "STRING"},
        "authors": {"type": "ARRAY", "items": {"type": "STRING"},
                    "description": "full names as 'First Last', main authors only, max 4"},
        "series": {"type": "STRING"},
        "series_index": {"type": "NUMBER"},
        "publisher": {"type": "STRING"},
        "published": {"type": "STRING", "description": "YYYY or YYYY-MM-DD of this edition"},
        "language": {"type": "STRING", "description": "ISO 639-1 code of the book's text, e.g. en, de"},
        "isbn": {"type": "STRING", "description": "ISBN-13 of this edition if known, digits only"},
        "description": {"type": "STRING", "description": "2-4 sentence blurb in the book's language"},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"},
                 "description": "3-8 genre/subject tags in English, Title Case"},
        "category": {"type": "STRING", "description": "library folder path, see instructions"},
        "new_category": {"type": "BOOLEAN"},
        "confidence": {"type": "NUMBER", "description": "0..1 how sure you are about title+author"},
        "not_a_book": {"type": "BOOLEAN", "description": "true if the file is clearly not a book (invoice, form, ...)"},
    },
    "required": ["index", "title", "authors", "language", "tags", "category", "confidence"],
}
SCHEMA = {"type": "OBJECT", "properties": {"books": {"type": "ARRAY", "items": BOOK_SCHEMA}}, "required": ["books"]}

PROMPT = """You are the librarian of a private ebook library. For EACH book below, identify the exact work and \
edition and return clean, complete metadata. Use the embedded metadata, the file name and especially the text of \
the first pages (title page, copyright page with ISBN/publisher/year). Use Google Search to verify and complete \
facts (ISBN-13, publisher, year, series) when they are not certain from the text. Never invent an ISBN.

Rules:
- title: the real main title without subtitle; subtitle separately. Fix capitalisation, remove junk such as \
"(Z-Library)", "libgen", hashes, "Anna's Archive", edition noise.
- authors: real people's full names ("Robert C. Martin"), editors only if there is no author.
- category: choose the single best-fitting folder from the EXISTING CATEGORIES list (exact spelling, a sub path \
like "Informatics/Hacking" is allowed when listed). {new_rule}
- language: the language of the book text.
- not_a_book: true for things that are clearly no book (invoices, receipts, scanned forms).

EXISTING CATEGORIES:
{categories}

BOOKS:
{books}
"""


def ask(books: list[dict], categories: list[str]) -> list[dict]:
    """books: [{index, file_name, format, size_mb, pages, embedded_metadata, text_excerpt}] -> list of metadata dicts."""
    new_rule = ("If none fits reasonably, propose a short new top-level category in English and set new_category=true."
                if config.ALLOW_NEW_CATEGORIES else "You MUST pick one of the existing categories.")
    prompt = PROMPT.format(new_rule=new_rule, categories="\n".join(f"- {c}" for c in categories),
                           books=json.dumps(books, ensure_ascii=False, indent=1))
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0.2},
    }
    if config.GEMINI_USE_SEARCH:
        body["tools"] = [{"google_search": {}}]
    url = API.format(model=config.GEMINI_MODEL)
    headers = {"x-goog-api-key": config.GEMINI_API_KEY, "Content-Type": "application/json"}

    # One logical query. Only transient server errors (429/5xx) are retried, with backoff.
    for attempt in range(4):
        r = requests.post(url, headers=headers, json=body, timeout=300)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            wait = 20 * (attempt + 1)
            print(f"⏳ Gemini HTTP {r.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        break
    d = r.json()
    usage = d.get("usageMetadata", {})
    print(f"🤖 Gemini {d.get('modelVersion')}: {usage.get('totalTokenCount')} tokens for {len(books)} book(s)")
    text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]).strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)["books"]
