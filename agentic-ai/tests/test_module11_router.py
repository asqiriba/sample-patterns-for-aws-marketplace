# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Session router reference implementation and the shared vectors.
The JS side (router.mjs) is checked against the same vectors file."""
import json
import re
import uuid
from pathlib import Path

import pytest

from module11.release import router_spec as R

ROOT = Path(__file__).resolve().parents[1]
IDS = R.vector_ids()
RID = R.VECTOR_ROLLOUTS[0]


def _share(state, canary, shadow=0, rid=RID):
    item = {"rolloutId": rid, "state": state, "canaryPct": canary, "shadowPct": shadow}
    return (sum(R.route(item, c) == "candidate" for c in IDS) / len(IDS),
            sum(R.shadow_enqueue(item, c) for c in IDS) / len(IDS))


@pytest.mark.parametrize("state", R.CANDIDATE_STATES)
def test_candidate_share_at_demo_and_prod_first_steps(state):
    assert abs(_share(state, R.PROFILES["demo"]["steps"][0])[0] - 0.50) <= 0.04
    assert abs(_share(state, R.PROFILES["prod"]["steps"][0])[0] - 0.05) <= 0.02


@pytest.mark.parametrize("state", ["IDLE", "SHADOWING", "ROLLING_BACK", "ROLLED_BACK", "PROMOTED"])
def test_non_candidate_states_route_stable(state):
    assert _share(state, 50)[0] == 0.0


def test_ramp_keeps_the_original_buckets():
    low = {c for c in IDS if R.route({"rolloutId": RID, "state": "OBSERVING", "canaryPct": 25}, c) == "candidate"}
    high = {c for c in IDS if R.route({"rolloutId": RID, "state": "OBSERVING", "canaryPct": 50}, c) == "candidate"}
    assert low < high
    assert all(25 <= R.bucket(RID, c) < 50 for c in high - low)


def test_shadow_rates_and_independence():
    assert abs(_share("SHADOWING", 0, 5)[1] - 0.05) <= 0.02
    assert abs(_share("OBSERVING", 50, 10)[1] - 0.10) <= 0.03
    assert _share("OBSERVING", 50, 100)[1] == 1.0
    assert _share("OBSERVING", 50, 0)[1] == 0.0
    assert _share("ROLLING_BACK", 0, 100)[1] == 0.0
    item = {"rolloutId": RID, "state": "OBSERVING", "canaryPct": 50, "shadowPct": 10}
    cand = [c for c in IDS if R.route(item, c) == "candidate"]
    stab = [c for c in IDS if R.route(item, c) == "stable"]
    rate = lambda ids: sum(R.shadow_enqueue(item, c) for c in ids) / len(ids)  # noqa: E731
    assert abs(rate(cand) - rate(stab)) <= 0.05          # both arms sampled at the same rate
    assert any(R.bucket(RID, c) != R.shadow_bucket(RID, c) for c in IDS)


def test_bucket_definition():
    import hashlib
    c = IDS[7]
    digest = hashlib.sha256(f"{RID}:{c}".encode()).digest()[:8]
    assert R.bucket(RID, c) == int.from_bytes(digest, "big") % 100
    digest = hashlib.sha256(f"{RID}:shadow:{c}".encode()).digest()[:8]
    assert R.shadow_bucket(RID, c) == int.from_bytes(digest, "big") % 100


def test_session_ids_and_conversation_ids():
    sid = R.session_id("prod", "candidate", 14, "1b4e" + "0" * 28 + "-x")
    assert len(R.session_id("prod", "candidate", 14)) == 59
    seen = {R.session_id("prod", "stable", 3) for _ in range(50)}
    assert len(seen) == 50 and all(len(s) >= 33 for s in seen)
    assert sid.startswith("m11-prod-candidate-v14-")
    assert R.conversation_id({}, "req-12345678") == "req-12345678"
    assert R.conversation_id({"conversation_id": "conv_ABC-123"}, "r") == "conv_ABC-123"
    for bad in ("short", "x" * 65, "has space!", 12345678):
        with pytest.raises(ValueError):
            R.conversation_id({"conversation_id": bad}, "r")


def test_opening_states():
    assert R.opening("demo") == {"state": "WARMING", "canaryPct": 50, "shadowPct": 0}
    assert R.opening("prod") == {"state": "WARMING", "canaryPct": 5, "shadowPct": 5}
    assert R.opening("model") == {"state": "SHADOWING", "canaryPct": 0, "shadowPct": 10}
    assert R.opening("model", shadow_pct=100)["shadowPct"] == 100
    assert R.ROLLOUT_ID.match(R.new_rollout_id())


def test_committed_vectors_and_js_constants_are_current():
    assert (ROOT / R.VECTORS_PATH).read_text(encoding="utf-8") == R.render_vectors()
    assert R.js_constants_current(ROOT)
    vectors = json.loads((ROOT / R.VECTORS_PATH).read_text(encoding="utf-8"))
    assert len(vectors["ids"]) == 1000 and all(R.CONVERSATION_ID.match(c) for c in vectors["ids"])
    js = (ROOT / R.CONSTS_PATH).read_text(encoding="utf-8")
    for state in R.STATES:
        assert f'"{state}"' in js
    assert re.search(r"export const STATE_CACHE_MS = 5000;", js)


def test_stale_js_constants_fail_c12(tmp_path):
    (tmp_path / "module11/edge/lambda").mkdir(parents=True)
    assert not R.js_constants_current(tmp_path)
    R.write_generated(tmp_path)
    assert R.js_constants_current(tmp_path)
    path = tmp_path / R.CONSTS_PATH
    path.write_text(path.read_text().replace("5000", "6000"))
    assert not R.js_constants_current(tmp_path)
    assert R.js_constants_current(tmp_path / "no-edge-here")
