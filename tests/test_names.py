import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("ALLOWED_SENDERS", "me@gmx.de,example.org,@sermak.xyz")

from book_importer import config, pipeline  # noqa: E402
from book_importer.main import sender_allowed  # noqa: E402


def test_base_name():
    assert pipeline.base_name("Clean Code", ["Robert C. Martin"]) == "Clean Code - Robert C. Martin"
    assert pipeline.base_name("Gaza in Crisis", ["Noam Chomsky", "Ilan Pappe"]) == "Gaza in Crisis - Noam Chomsky & Ilan Pappe"
    assert pipeline.base_name("X", ["A", "B", "C", "D"]) == "X - A et al"


def test_clean_name_removes_forbidden_chars():
    assert pipeline.clean_name('Faust: Der Tragödie "Erster" Teil / Reclam?') == "Faust - Der Tragödie Erster Teil Reclam"


def test_safe_category():
    known = ["Self", "Informatics", "Informatics/Hacking"]
    assert pipeline.safe_category("informatics/hacking", known) == "Informatics/Hacking"
    assert pipeline.safe_category("../../etc", known) == "etc"
    assert pipeline.safe_category("", known) == "Unsorted"


def test_sender_allowed():
    config.ALLOWED_SENDERS = ["me@gmx.de", "example.org", "@sermak.xyz"]
    assert sender_allowed(["me@gmx.de"])
    assert not sender_allowed(["other@gmx.de"])
    assert sender_allowed(["anyone@example.org"])
    assert sender_allowed(["x@sermak.xyz"])
    assert not sender_allowed(["x@evil.example.org.attacker.com"])
