// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// The generated router constants agree with tests/fixtures/module11/router_vectors.json
// test_module11_router.mjs tests router.mjs itself.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as C from "../module11/edge/lambda/router_consts.mjs";

const vectors = JSON.parse(readFileSync(fileURLToPath(new URL("./fixtures/module11/router_vectors.json", import.meta.url)), "utf8"));

const u64mod = (text) => Number(createHash("sha256").update(text).digest().readBigUInt64BE(0) % BigInt(C.BUCKETS));
const bucket = (rid, conv) => u64mod(rid + C.BUCKET_SEPARATOR + conv);
const shadowBucket = (rid, conv) => u64mod(rid + C.SHADOW_SEPARATOR + conv);

test("buckets from the generated constants match the Python vectors", () => {
  for (const [rid, data] of Object.entries(vectors.rollouts)) {
    vectors.ids.forEach((conv, i) => {
      assert.equal(bucket(rid, conv), data.bucket[i]);
      assert.equal(shadowBucket(rid, conv), data.shadowBucket[i]);
    });
  }
});

test("route and shadow cases match bit for bit", () => {
  for (const [rid, data] of Object.entries(vectors.rollouts)) {
    for (const c of data.cases) {
      const routes = vectors.ids.map((conv) =>
        C.CANDIDATE_STATES.includes(c.state) && bucket(rid, conv) < c.canaryPct ? "1" : "0").join("");
      const shadows = vectors.ids.map((conv) =>
        C.SHADOW_STATES.includes(c.state) && shadowBucket(rid, conv) < c.shadowPct ? "1" : "0").join("");
      assert.equal(createHash("sha256").update(routes).digest("hex"), c.routeSha256, `${c.state} ${c.canaryPct}`);
      assert.equal(createHash("sha256").update(shadows).digest("hex"), c.shadowSha256, `${c.state} ${c.shadowPct}`);
    }
  }
});

test("enum and identifier constants", () => {
  assert.deepEqual([...C.ROLLOUT_STATES], ["IDLE", "SHADOWING", "WARMING", "OBSERVING", "ADVANCING",
    "ROLLING_BACK", "ROLLED_BACK", "PROMOTED"]);
  assert.ok(Object.isFrozen(C.CANDIDATE_STATES));
  assert.equal(C.STATE_CACHE_MS, 5000);
  assert.ok(C.CONVERSATION_ID.test("conv_ABC-123"));
  assert.ok(!C.CONVERSATION_ID.test("short"));
  assert.match(C.SPEC_SHA256, /^[0-9a-f]{64}$/);
});
