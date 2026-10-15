// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// The JS side of the shared router vectors (8.1 test_module11_router.mjs):
// router.mjs agrees with module11/release/router_spec.py on 1,000 IDs, candidate and shadow
// shares, ramps, session IDs, the control-item reader, and the constants themselves.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import * as C from "../module11/edge/lambda/router_consts.mjs";
import {
  ROLLOUT_STATES, bucket, conversationId, itemToState, pick, route, sessionId, shadowBucket, shadowEnqueue, stateReader,
} from "../module11/edge/lambda/router.mjs";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const vectors = JSON.parse(read("./fixtures/module11/router_vectors.json"));
const IDS = vectors.ids;
const RID = Object.keys(vectors.rollouts)[0];
const share = (fn) => IDS.filter(fn).length / IDS.length;
const state = (name, canaryPct, shadowPct = 0, rolloutId = RID) => ({ state: name, rolloutId, canaryPct, shadowPct });

test("Python and JS agree on 1,000 IDs: buckets, shadow buckets, route and shadow decisions", () => {
  assert.equal(IDS.length, 1000);
  for (const [rid, data] of Object.entries(vectors.rollouts)) {
    IDS.forEach((id, i) => {
      assert.equal(bucket(rid, id), data.bucket[i]);
      assert.equal(shadowBucket(rid, id), data.shadowBucket[i]);
    });
    for (const c of data.cases) {
      const s = state(c.state, c.canaryPct, c.shadowPct, rid);
      const routes = IDS.map((id) => (route(s, id) === "candidate" ? "1" : "0")).join("");
      const shadows = IDS.map((id) => (shadowEnqueue(s, id) ? "1" : "0")).join("");
      assert.equal(createHash("sha256").update(routes).digest("hex"), c.routeSha256, `${c.state} ${c.canaryPct}`);
      assert.equal(createHash("sha256").update(shadows).digest("hex"), c.shadowSha256, `${c.state} ${c.shadowPct}`);
      assert.equal([...routes].filter((x) => x === "1").length, c.candidate);
      assert.equal([...shadows].filter((x) => x === "1").length, c.shadowed);
    }
  }
});

test("candidate share at the demo's first step (50) and prod's (5) in every candidate state", () => {
  for (const name of ["WARMING", "OBSERVING", "ADVANCING"]) {
    assert.ok(Math.abs(share((id) => route(state(name, 50), id) === "candidate") - 0.50) <= 0.04, name);
    assert.ok(Math.abs(share((id) => route(state(name, 5), id) === "candidate") - 0.05) <= 0.02, name);
  }
});

test("IDLE, SHADOWING, ROLLING_BACK, ROLLED_BACK and PROMOTED route stable", () => {
  for (const name of ["IDLE", "SHADOWING", "ROLLING_BACK", "ROLLED_BACK", "PROMOTED"]) {
    assert.equal(share((id) => route(state(name, 100), id) === "candidate"), 0, name);
  }
  assert.equal(route(null, "conv-12345"), "stable");
  assert.equal(route({ state: "WARMING", canaryPct: 100 }, "conv-12345"), "stable"); // no rolloutId
});

test("a ramp from 25 to 50 keeps the original buckets", () => {
  const at25 = IDS.filter((id) => route(state("ADVANCING", 25), id) === "candidate");
  const at50 = new Set(IDS.filter((id) => route(state("ADVANCING", 50), id) === "candidate"));
  assert.ok(at25.length > 0 && at25.every((id) => at50.has(id)));
});

test("shadow enqueue rates, and independence from the routing bucket", () => {
  const rate = (pct) => share((id) => shadowEnqueue(state("OBSERVING", 50, pct), id));
  assert.ok(Math.abs(rate(5) - 0.05) <= 0.02);
  assert.ok(Math.abs(rate(10) - 0.10) <= 0.03);
  assert.equal(rate(100), 1);
  assert.equal(rate(0), 0);
  const s = state("OBSERVING", 50, 10);
  const byArm = (arm) => {
    const ids = IDS.filter((id) => route(s, id) === arm);
    return ids.filter((id) => shadowEnqueue(s, id)).length / ids.length;
  };
  assert.ok(Math.abs(byArm("candidate") - 0.10) <= 0.04 && Math.abs(byArm("stable") - 0.10) <= 0.04);
  assert.ok(IDS.some((id) => bucket(RID, id) !== shadowBucket(RID, id)));
  assert.equal(shadowEnqueue(state("IDLE", 0, 100), IDS[0]), false);
  assert.equal(shadowEnqueue(state("SHADOWING", 0, 100), IDS[0]), true);
});

test("session IDs are at least 33 characters and unique per request", () => {
  const seen = new Set();
  for (let i = 0; i < 200; i += 1) {
    const id = sessionId("prod", "candidate", "14");
    assert.ok(id.length >= C.SESSION_ID_MIN && id.length <= C.SESSION_ID_MAX);
    assert.match(id, /^m11-prod-candidate-v14-[0-9a-f-]{36}$/);
    seen.add(id);
  }
  assert.equal(seen.size, 200);
  assert.equal(sessionId("d", "s", 1, "x".repeat(22)).length, 33);   // "m11-d-s-v1-" + 22
  assert.throws(() => sessionId("d", "s", 1, "x"), /outside 33-256/);
});

test("conversation_id defaults to the request ID and is validated", () => {
  assert.equal(conversationId({}, "req-1"), "req-1");
  assert.equal(conversationId({ conversation_id: null }, "req-1"), "req-1");
  assert.equal(conversationId({ conversation_id: "conv_ABC-123" }, "req-1"), "conv_ABC-123");
  for (const bad of ["short", "x".repeat(65), "bad id!!", 12345678]) {
    assert.throws(() => conversationId({ conversation_id: bad }, "req-1"), (error) => error.status === 400);
  }
});

test("pick: rollout stages serve stable/candidate entries; other stages their single endpoint", () => {
  const entries = { stable: { version: "3", release: "harness-v7" }, candidate: { version: "4", release: "harness-v8" } };
  const s = { ...state("WARMING", 50), endpoints: entries };
  const arms = IDS.map((id) => pick(s, id));
  assert.ok(arms.every((p) => p.arm === p.qualifier && p.entry === entries[p.arm]));
  assert.deepEqual(new Set(arms.map((p) => p.arm)), new Set(["stable", "candidate"]));
  assert.ok(IDS.every((id) => pick({ ...s, state: "ROLLED_BACK" }, id).arm === "stable"));
  const noCandidate = { ...s, endpoints: { stable: entries.stable } };
  assert.ok(IDS.every((id) => pick(noCandidate, id).qualifier === "stable"));
  assert.deepEqual(pick({ state: "IDLE", endpoints: { dev: entries.stable } }, "c-12345678"),
    { arm: "dev", qualifier: "dev", entry: entries.stable });
  assert.equal(pick({ state: "IDLE", endpoints: {} }, "c-12345678"), null);
  assert.equal(pick(null, "c-12345678"), null);
});

test("control item: DynamoDB unmarshalling and the 5 s cache that keeps the last good value", async () => {
  const item = { state: { S: "WARMING" }, rolloutId: { S: RID }, canaryPct: { N: "50" }, latched: { BOOL: false },
    endpoints: { M: { stable: { M: { version: { S: "3" }, release: { S: "harness-v7" } } } } },
    steps: { L: [{ N: "50" }, { N: "100" }] }, note: { NULL: true }, tags: { SS: ["a"] } };
  assert.deepEqual(itemToState(item), { state: "WARMING", rolloutId: RID, canaryPct: 50, latched: false,
    endpoints: { stable: { version: "3", release: "harness-v7" } }, steps: [50, 100], note: null, tags: ["a"] });
  let now = 0;
  let reads = 0;
  let fail = false;
  const lines = [];
  const current = stateReader(async () => { reads += 1; if (fail) throw Object.assign(new Error("x"), { name: "Throttled" }); return item; },
    { now: () => now, log: (line) => lines.push(line) });
  assert.equal((await current()).state, "WARMING");
  now = C.STATE_CACHE_MS - 1;
  await current();
  assert.equal(reads, 1);
  now = C.STATE_CACHE_MS;
  fail = true;
  assert.equal((await current()).state, "WARMING"); // last good value
  assert.equal(reads, 2);
  assert.match(lines[0], /"event":"router_state_unavailable".*"cached":true/);
  const empty = stateReader(async () => { throw new Error("down"); }, { log: () => {} });
  assert.equal(await empty(), null);
});

// Python's json.dumps(value, sort_keys=True) for the flat constants object (", " and ": ").
const pyDumps = (value) => {
  if (Array.isArray(value)) return `[${value.map(pyDumps).join(", ")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}: ${pyDumps(value[k])}`).join(", ")}}`;
  }
  return JSON.stringify(value);
};

test("the JS constants match router_spec.py", () => {
  const consts = {
    ROLLOUT_STATES: [...C.ROLLOUT_STATES], CANDIDATE_STATES: [...C.CANDIDATE_STATES], SHADOW_STATES: [...C.SHADOW_STATES],
    BUCKETS: C.BUCKETS, BUCKET_SEPARATOR: C.BUCKET_SEPARATOR, SHADOW_SEPARATOR: C.SHADOW_SEPARATOR,
    STATE_CACHE_MS: C.STATE_CACHE_MS, CONVERSATION_ID_PATTERN: C.CONVERSATION_ID.source,
    SESSION_ID_MIN: C.SESSION_ID_MIN, SESSION_ID_MAX: C.SESSION_ID_MAX,
  };
  assert.equal(createHash("sha256").update(pyDumps(consts)).digest("hex"), C.SPEC_SHA256);
  const spec = read("../module11/release/router_spec.py");
  const value = (name) => spec.match(new RegExp(`^${name} = (.+)$`, "m"))[1].trim();
  const tuple = (name) => [...value(name).matchAll(/"([A-Z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual([...ROLLOUT_STATES], [...spec.matchAll(/^ {4}([A-Z_]+) = "\1"$/gm)].map((m) => m[1]));
  assert.deepEqual([...C.CANDIDATE_STATES], tuple("CANDIDATE_STATES"));
  assert.deepEqual([...C.SHADOW_STATES], tuple("SHADOW_STATES"));
  assert.equal(C.BUCKETS, Number(value("BUCKETS")));
  assert.equal(C.BUCKET_SEPARATOR, JSON.parse(value("BUCKET_SEPARATOR")));
  assert.equal(C.SHADOW_SEPARATOR, JSON.parse(value("SHADOW_SEPARATOR")));
  assert.equal(C.STATE_CACHE_MS, Number(value("STATE_CACHE_SECONDS")) * 1000);
  assert.equal(`r"${C.CONVERSATION_ID.source}"`, value("CONVERSATION_ID").match(/^re\.compile\((.+)\)$/)[1]);
  assert.deepEqual([C.SESSION_ID_MIN, C.SESSION_ID_MAX],
    value("SESSION_ID_MIN, SESSION_ID_MAX").split("#")[0].split(",").map((x) => Number(x.trim())));
});
