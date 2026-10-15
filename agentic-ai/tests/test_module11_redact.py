# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Redaction for harvested sessions."""
import subprocess
import sys
from pathlib import Path

from module11.release import redact as R

ROOT = Path(__file__).resolve().parents[1]


def test_path_loaded_anonymize_pii_works_without_langchain():
    """A fresh interpreter with langchain blocked and module7 never imported as a package."""
    code = (
        "import sys\n"
        "for name in ('langchain', 'langchain_core', 'langgraph', 'module7'):\n"
        "    sys.modules[name] = None\n"
        "from module11.release import redact\n"
        "f = redact.load_anonymize('.')\n"
        "print(f('mail jane@example.com key ' + 'AKIA' + 'Z' * 16))\n"   # built at runtime: no key literal
        "assert 'module7' not in [m for m in sys.modules if sys.modules[m] is not None]\n"
    )
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this interpreter with fixed test code
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert "[REDACTED_EMAIL]" in out and "[REDACTED_AWS_ACCESS_KEY]" in out


def test_ids_arns_and_names_are_replaced():
    anonymize = R.load_anonymize(str(ROOT))
    session = {"turns": [{"input": "Jordan here: arn:aws:iam::123456789012:role/m11-prod-runtime failed for 123456789012"}],
               "note": "ping jordan@example.com", "count": 3}
    out = R.redact(session, anonymize, names=["Jordan"])
    text = out["turns"][0]["input"]
    assert "123456789012" not in text and "arn:aws:iam::111122223333:role/m11-prod-runtime" in text
    assert text.startswith("presenter here") and out["note"] == "ping [REDACTED_EMAIL]" and out["count"] == 3
    rendered = R.diff(session, out)
    assert "-" in rendered and "+" in rendered and "111122223333" in rendered


def test_incident_shape_drops_everything_else():
    shaped = R.incident_shape({"service": "checkout-service", "turns": [], "runtimeSessionId": "m11-x", "trace": {}})
    assert shaped == {"service": "checkout-service", "turns": []}
