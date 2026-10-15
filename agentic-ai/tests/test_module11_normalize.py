# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/release/normalize.py and hashing.py."""
import pytest

from module11.release.hashing import digest, file_hash, files_hash
from module11.release.normalize import NormalizationError, canonical_json, normalize


def test_text_variants_hash_equal():
    base = file_hash("a.md", b"Call the tool.\nThen answer.\n")
    for variant in (b"Call the tool.\r\nThen answer.\r\n", b"Call the tool.   \nThen answer.\t\n",
                    b"\xef\xbb\xbfCall the tool.\nThen answer.\n", b"Call the tool.\nThen answer.",
                    b"Call the tool.\nThen answer.\n\n\n", b"Call the tool.\rThen answer.\r"):
        assert file_hash("a.md", variant) == base


def test_word_change_changes_hash():
    assert file_hash("a.md", b"Call the tool.\n") != file_hash("a.md", b"Call a tool.\n")


def test_json_key_order_and_formatting_do_not_matter():
    assert file_hash("t.json", b'{"b": 1, "a": [1, 2]}') == file_hash("t.json", b'{\n  "a": [1,2],\n  "b": 1\n}\n')
    assert file_hash("t.json", b'{"a": 1}') != file_hash("t.json", b'{"a": 2}')


def test_yaml_comments_do_not_matter_and_match_json():
    a = file_hash("o.yaml", b"# comment\nmaxTurns: 6  # inline\nrequired: [a, b]\n")
    b = file_hash("o.yaml", b"required:\n  - a\n  - b\nmaxTurns: 6\n")
    assert a == b
    assert normalize("o.yaml", b"x: 1\n") == canonical_json({"x": 1})


def test_invalid_inputs_raise_naming_the_path_not_contents():
    with pytest.raises(NormalizationError) as exc:
        normalize("module11/harness/agent/models.yaml", b"key: [unclosed\nsecret: hunter2\n")
    assert exc.value.path == "module11/harness/agent/models.yaml"
    assert "hunter2" not in str(exc.value)
    with pytest.raises(NormalizationError):
        normalize("x.json", b"{not json")
    with pytest.raises(NormalizationError):
        normalize("x.md", b"\xff\xfe bad utf8")


def test_jsonl_lines_are_canonical():
    assert normalize("s.jsonl", b'{"b":1,"a":2}\r\n\n{"c":3}') == b'{"a":2,"b":1}\n{"c":3}\n'


def test_raw_files_are_bytes():
    assert normalize("image.png", b"\x89PNG\r\n") == b"\x89PNG\r\n"


def test_multi_file_and_digest_are_order_independent():
    assert files_hash({"b": "sha256:2", "a": "sha256:1"}) == files_hash({"a": "sha256:1", "b": "sha256:2"})
    assert digest({"x": "1", "y": "2"}) == digest({"y": "2", "x": "1"})
