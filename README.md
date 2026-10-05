# book-email-importer

Mail an ebook (EPUB, PDF, MOBI, AZW3, FB2, …) to a dedicated address and it appears — cleanly named, tagged and
with a cover — in your [Kavita](https://www.kavitareader.com/) library. Sibling of
[latex-email-daemon](https://github.com/emysliwietz/latex-email-daemon).

## What happens to a mail

1. The IMAP listener (IDLE) sees a new mail to `TARGET_ADDRESS` from an allowed sender.
2. Book attachments are collected (ZIPs are unpacked). MOBI/AZW3/FB2/… are converted to EPUB with calibre when no EPUB was attached, because
   Kavita can't read them (the original is kept next to it).
3. **One Google AI (Gemini) request per mail — for all its books together.** It gets the file names, the
   embedded metadata, the text of the first pages and the list of existing library folders, and returns strict JSON:
   title, subtitle, authors, series, publisher, year, language, ISBN, description, tags and the best-fitting folder.
   Google Search grounding is enabled in the same request to verify ISBN/publisher/year. No other AI calls are made.
4. The metadata is written **into** the file with calibre (`ebook-meta`), the file is renamed to
   `Title - Author.ext` and stored as `LIBRARY_DIR/<category>/<Title - Author>/`, together with `cover.jpg` and
   `metadata.opf` (calibre style).
5. Covers come without AI: the embedded cover if it is large enough, otherwise Open Library / Google Books by ISBN.
6. Kavita rescans **only that folder** (`/api/Library/scan-folder`). PDFs can't carry a cover, so for them the
   cover is uploaded to the new Kavita series and locked.
7. The sender gets a short reply listing what was imported where (or why not).

**Several files in one mail:** different books are filed separately. Files that are the *same work* (other format
or another edition) are recognised in the same single AI request and get one folder and one consistent set of
metadata. A real EPUB always wins over a converted one (conversion only happens when no EPUB of that edition was
attached). Different editions are kept side by side with the edition in the name, e.g.
`Think Python - Allen Downey (2nd ed., 2015).pdf`. Two copies of the same edition and format in one mail: the larger
one is kept. Files already in the library (same title, author, edition and format) are never overwritten. The reply
says for every file whether it is new, already in the library or a second copy in the mail.

## Environment variables

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `EMAIL_ACCOUNT` | ✅ | | IMAP login, e.g. `books@example.org` |
| `EMAIL_PASSWORD` | ✅ | | IMAP password |
| `GEMINI_API_KEY` | ✅ | | Google AI Studio API key |
| `KAVITA_API_KEY` | recommended | | Kavita auth key of an **admin** user (Kavita → Settings → Account → Auth keys) |
| `ALLOWED_SENDERS` | recommended | *(everyone)* | Comma-separated addresses and/or domains, e.g. `me@gmx.de,example.org` |
| `IMAP_SERVER` | | `imap.strato.de` | |
| `TARGET_ADDRESS` | | `EMAIL_ACCOUNT` | Recipient address(es) that trigger an import, comma-separated (e.g. an alias that forwards into the mailbox); `*` = every mail in the mailbox |
| `IDLE_TIMEOUT` | | `300` | Seconds per IMAP IDLE round |
| `GEMINI_MODEL` | | `gemini-flash-latest` | Any Gemini model with structured output |
| `GEMINI_USE_SEARCH` | | `true` | Google Search grounding inside the single request |
| `KAVITA_URL` | | `http://kavita:5000` | Reachable from this container |
| `KAVITA_LIBRARY_DIR` | | `LIBRARY_DIR` | The library folder as *Kavita* sees it, if it differs |
| `LIBRARY_DIR` | | `/books/ebooks` | Where books are filed (category folders live here) |
| `CATEGORY_EXCLUDE` | | `calibre_stash` | Top-level folders the AI must not file into |
| `ALLOW_NEW_CATEGORIES` | | `true` | AI may create a new top-level folder if nothing fits |
| `CONVERT_TO_EPUB` | | `true` | Convert MOBI/AZW3/FB2/… to EPUB |
| `KEEP_ORIGINAL` | | `true` | Keep the original file next to the converted EPUB |
| `WRITE_SIDECARS` | | `true` | Write `cover.jpg` + `metadata.opf` next to the book |
| `REPLY_ENABLED` | | `true` | Send a result mail back to the sender |
| `SMTP_SERVER` / `SMTP_PORT` | | `smtp.strato.de` / `587` | For the reply |
| `SMTP_SENDER_EMAIL` / `SMTP_SENDER_PASSWORD` | | the IMAP account | For the reply |
| `DELETE_AFTER_IMPORT` | | `false` | Delete the mail after a successful import (otherwise it's marked read) |
| `EXCERPT_CHARS` | | `6000` | How much book text goes into the AI request |
| `FILE_MODE` / `DIR_MODE` | | `664` / `775` | Permissions of created files/folders |

On first start the listener ignores the mails already in the inbox and only handles new ones.

## Compose

```yaml
  book-importer:
    image: ghcr.io/emysliwietz/book-email-importer:latest
    container_name: book-importer
    restart: unless-stopped
    environment:
      - EMAIL_ACCOUNT=${BOOKS_EMAIL_ACCOUNT}
      - EMAIL_PASSWORD=${BOOKS_EMAIL_PASSWORD}
      - ALLOWED_SENDERS=${BOOKS_ALLOWED_SENDERS}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - KAVITA_API_KEY=${KAVITA_API_KEY}
      - KAVITA_URL=http://kavita:5000
      - KAVITA_LIBRARY_DIR=/books/ebooks
    volumes:
      - /mnt/media/books:/books          # same host folder Kavita uses
      - /root/docker/book-importer:/data # state + temp files
```

## Import local files (no mail)

```sh
docker run --rm --env-file .env -v /mnt/media/books:/books -v "$PWD":/in ghcr.io/emysliwietz/book-email-importer \
  python -m book_importer.cli /in/some-book.epub
```
