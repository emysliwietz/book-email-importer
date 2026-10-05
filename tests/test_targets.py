import os
import subprocess
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _targets(env: dict) -> list:
    code = "from book_importer import config; print(config.TARGET_ADDRESSES)"
    out = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": SRC, **env},
                         capture_output=True, text=True, check=True).stdout
    return eval(out)


def test_target_defaults_to_account():
    assert _targets({"EMAIL_ACCOUNT": "Books@Sermak.xyz", "TARGET_ADDRESS": ""}) == ["books@sermak.xyz"]


def test_multiple_targets_and_wildcard():
    assert _targets({"EMAIL_ACCOUNT": "a@x.de", "TARGET_ADDRESS": "b@x.de, Alias@Y.de"}) == ["b@x.de", "alias@y.de"]
    assert _targets({"EMAIL_ACCOUNT": "a@x.de", "TARGET_ADDRESS": "*"}) == ["*"]
