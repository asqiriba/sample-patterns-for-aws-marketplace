# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_check_bedrock_models.py
===================================
Offline tests for scripts/check_bedrock_models.py.

Bedrock responses come from botocore.stub.Stubber, and the scan runs over
files written to tmp_path, so no AWS credentials or network are needed.

Run:
    pytest tests/test_check_bedrock_models.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_bedrock_models.py"
_spec = importlib.util.spec_from_file_location("check_bedrock_models", _SCRIPT)
cbm = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cbm  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(cbm)

ACTIVE_ID = "amazon.titan-embed-text-v2:0"
PROFILE_ID = "us.anthropic.claude-sonnet-4-6"
LEGACY_ID = "anthropic.claude-old-model-v1:0"
MISSING_ID = "anthropic.claude-does-not-exist-v1:0"


def _client():
    return boto3.client(  # nosec B106 - dummy credentials for an offline, stubbed client
        "bedrock",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def _fm_response(model_id, status, legacy=None, eol=None):
    lifecycle = {"status": status}
    if legacy:
        lifecycle["legacyTime"] = legacy
    if eol:
        lifecycle["endOfLifeTime"] = eol
    return {
        "modelDetails": {
            "modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{model_id}",
            "modelId": model_id,
            "modelLifecycle": lifecycle,
        }
    }


def _profile(profile_id, status="ACTIVE"):
    return {
        "inferenceProfileName": profile_id,
        "inferenceProfileArn": f"arn:aws:bedrock:us-east-1:123456789012:inference-profile/{profile_id}",
        "inferenceProfileId": profile_id,
        "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/example"}],
        "status": status,
        "type": "SYSTEM_DEFINED",
    }


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

class TestScan:
    def test_finds_base_and_prefixed_ids(self, tmp_path):
        _write(tmp_path, "models.py", f'A = "{PROFILE_ID}"\nB = "{ACTIVE_ID}"\n')

        found = cbm.scan([tmp_path])

        assert found == {PROFILE_ID: {"models.py"}, ACTIVE_ID: {"models.py"}}

    def test_ignores_non_model_tokens_and_urls(self, tmp_path):
        _write(
            tmp_path, "notes.md",
            "meta.get('x'); see https://docs.aws.amazon.com/bedrock and team@amazon.com\n"
            "openai.providers is not a model; neither is amazon.nova\n",
        )

        assert cbm.scan([tmp_path]) == {}

    def test_skips_history_changelog_marker_env_and_absence_asserts(self, tmp_path):
        _write(tmp_path, "docs/history/OLD.md", f"{LEGACY_ID}\n")
        _write(tmp_path, "CHANGELOG.md", f"replaced {LEGACY_ID}\n")
        _write(tmp_path, "node_modules/pkg/index.js", f"'{LEGACY_ID}'\n")
        _write(tmp_path, ".env", f"MODEL={LEGACY_ID}\n")
        _write(tmp_path, "doc.md", f"{LEGACY_ID}  <!-- check-bedrock-models: ignore -->\n")
        _write(tmp_path, "tests/test_x.py", f'assert "{LEGACY_ID}" not in text\n')
        _write(tmp_path, ".env.example", f"MODEL={ACTIVE_ID}\n")

        assert cbm.scan([tmp_path]) == {ACTIVE_ID: {".env.example"}}


# ---------------------------------------------------------------------------
# Lifecycle checks and exit codes
# ---------------------------------------------------------------------------

class TestMain:
    def test_all_active_exits_zero(self, tmp_path, capsys):
        _write(tmp_path, "models.py", f'A = "{PROFILE_ID}"\nB = "{ACTIVE_ID}"\n')
        client = _client()
        with Stubber(client) as stub:
            stub.add_response(
                "get_foundation_model",
                _fm_response(ACTIVE_ID, "ACTIVE"),
                {"modelIdentifier": ACTIVE_ID},
            )
            stub.add_response(
                "get_foundation_model",
                _fm_response("anthropic.claude-sonnet-4-6", "ACTIVE"),
                {"modelIdentifier": "anthropic.claude-sonnet-4-6"},
            )
            # Two pages, to exercise pagination.
            stub.add_response(
                "list_inference_profiles",
                {"inferenceProfileSummaries": [_profile("us.other.model-v1")], "nextToken": "t1"},
                {"typeEquals": "SYSTEM_DEFINED"},
            )
            stub.add_response(
                "list_inference_profiles",
                {"inferenceProfileSummaries": [_profile(PROFILE_ID)]},
                {"typeEquals": "SYSTEM_DEFINED", "nextToken": "t1"},
            )

            code = cbm.main([str(tmp_path)], client=client)
            stub.assert_no_pending_responses()

        out = capsys.readouterr().out
        assert code == 0
        assert "OK: every model ID is ACTIVE." in out
        assert PROFILE_ID in out and "inference-profile" in out
        assert ACTIVE_ID in out and "foundation-model" in out

    def test_legacy_exits_one(self, tmp_path, capsys):
        _write(tmp_path, "models.py", f'M = "{LEGACY_ID}"\n')
        legacy = datetime(2026, 4, 14, tzinfo=timezone.utc)
        eol = datetime.now(timezone.utc) + timedelta(days=90)
        client = _client()
        with Stubber(client) as stub:
            stub.add_response(
                "get_foundation_model",
                _fm_response(LEGACY_ID, "LEGACY", legacy=legacy, eol=eol),
                {"modelIdentifier": LEGACY_ID},
            )
            code = cbm.main([str(tmp_path)], client=client)

        out = capsys.readouterr().out
        assert code == 1
        assert "LEGACY" in out
        assert "2026-04-14" in out
        assert eol.date().isoformat() in out

    def test_past_end_of_life_is_eol(self, tmp_path, capsys):
        _write(tmp_path, "models.py", f'M = "{LEGACY_ID}"\n')
        client = _client()
        with Stubber(client) as stub:
            stub.add_response(
                "get_foundation_model",
                _fm_response(LEGACY_ID, "LEGACY", eol=datetime(2020, 1, 1, tzinfo=timezone.utc)),
                {"modelIdentifier": LEGACY_ID},
            )
            code = cbm.main([str(tmp_path)], client=client)

        assert code == 1
        assert " EOL " in capsys.readouterr().out

    def test_missing_model_exits_one(self, tmp_path, capsys):
        _write(tmp_path, "models.py", f'M = "{MISSING_ID}"\n')
        client = _client()
        with Stubber(client) as stub:
            stub.add_client_error(
                "get_foundation_model",
                service_error_code="ResourceNotFoundException",
                http_status_code=404,
                expected_params={"modelIdentifier": MISSING_ID},
            )
            code = cbm.main([str(tmp_path)], client=client)

        assert code == 1
        assert "UNKNOWN" in capsys.readouterr().out

    def test_missing_inference_profile_exits_one(self, tmp_path, capsys):
        _write(tmp_path, "models.py", f'M = "{PROFILE_ID}"\n')
        client = _client()
        with Stubber(client) as stub:
            stub.add_response(
                "get_foundation_model",
                _fm_response("anthropic.claude-sonnet-4-6", "ACTIVE"),
                {"modelIdentifier": "anthropic.claude-sonnet-4-6"},
            )
            stub.add_response(
                "list_inference_profiles",
                {"inferenceProfileSummaries": []},
                {"typeEquals": "SYSTEM_DEFINED"},
            )
            code = cbm.main([str(tmp_path)], client=client)

        assert code == 1
        assert "no inference profile" in capsys.readouterr().out

    def test_access_denied_exits_two(self, tmp_path, capsys):
        _write(tmp_path, "models.py", f'M = "{ACTIVE_ID}"\n')
        client = _client()
        with Stubber(client) as stub:
            stub.add_client_error(
                "get_foundation_model",
                service_error_code="AccessDeniedException",
                http_status_code=403,
            )
            code = cbm.main([str(tmp_path)], client=client)

        assert code == 2
        assert "Bedrock lookup failed" in capsys.readouterr().err

    def test_no_ids_exits_zero_without_aws(self, tmp_path, capsys):
        _write(tmp_path, "empty.py", "x = 1\n")

        assert cbm.main([str(tmp_path)], client=object()) == 0
        assert "No Bedrock model IDs found." in capsys.readouterr().out

    def test_region_default_chain(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert cbm._default_region() == "us-east-1"
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        assert cbm._default_region() == "eu-west-1"
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert cbm._default_region() == "us-west-2"

    def test_missing_path_is_usage_error(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            cbm.main([str(tmp_path / "nope")], client=object())
        assert exc.value.code == 2
