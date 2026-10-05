"""Import local files without email:  python -m book_importer.cli book1.epub book2.pdf"""
import json
import os
import sys

from . import config, pipeline


def main() -> None:
    files = [os.path.abspath(f) for f in sys.argv[1:]]
    if not files:
        sys.exit("usage: python -m book_importer.cli FILE [FILE...]")
    os.makedirs(config.WORK_DIR, exist_ok=True)
    for r in pipeline.process(files):
        print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
