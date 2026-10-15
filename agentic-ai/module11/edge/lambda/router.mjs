// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Session-hash router. The reference implementation and the one RolloutState enum
// live in module11/release/router_spec.py; the constants below come from the generated
// router_consts.mjs (check C12), and tests/fixtures/module11/router_vectors.json holds the
// shared vectors both implementations must reproduce bit for bit.
import { createHash, randomUUID } from "node:crypto";
import {
  BUCKETS, BUCKET_SEPARATOR, CANDIDATE_STATES, CONVERSATION_ID, ROLLOUT_STATES, SESSION_ID_MAX, SESSION_ID_MIN,
  SHADOW_SEPARATOR, SHADOW_STATES, STATE_CACHE_MS,
} from "./router_consts.mjs";

export { ROLLOUT_STATES };

const u64mod = (text) => Number(createHash("sha256").update(text, "utf8").digest().readBigUInt64BE(0) % BigInt(BUCKETS));

/** uint64_be(sha256(rolloutId + ":" + conversationId)[0:8]) mod 100 */
export const bucket = (rolloutId, conversationId) => u64mod(rolloutId + BUCKET_SEPARATOR + conversationId);
/** A separate hash input, so shadow sampling is independent of the routing arm. */
export const shadowBucket = (rolloutId, conversationId) => u64mod(rolloutId + SHADOW_SEPARATOR + conversationId);

/** "candidate" iff the rollout is in a candidate state and the bucket is below canaryPct. */
export function route(state, conversationId) {
  if (state && CANDIDATE_STATES.includes(state.state) && state.rolloutId
      && bucket(state.rolloutId, conversationId) < Number(state.canaryPct || 0)) return "candidate";
  return "stable";
}

export function shadowEnqueue(state, conversationId) {
  return Boolean(state && SHADOW_STATES.includes(state.state) && state.rolloutId
    && shadowBucket(state.rolloutId, conversationId) < Number(state.shadowPct || 0));
}

/** The optional body field conversation_id (routing only), defaulting to the request ID. */
export function conversationId(body, requestId) {
  const value = body?.conversation_id;
  if (value === undefined || value === null) return requestId;
  if (typeof value !== "string" || !CONVERSATION_ID.test(value)) {
    throw Object.assign(new Error("conversation_id must match ^[A-Za-z0-9_-]{8,64}$"), { status: 400 });
  }
  return value;
}

/** Fresh per request: m11-<stagekey>-<qualifier>-v<version>-<uuid4>. */
export function sessionId(stagekey, qualifier, version, unique = randomUUID()) {
  const value = `m11-${stagekey}-${qualifier}-v${Number.parseInt(version, 10)}-${unique}`;
  if (value.length < SESSION_ID_MIN || value.length > SESSION_ID_MAX) {
    throw new Error(`runtime session ID length ${value.length} outside ${SESSION_ID_MIN}-${SESSION_ID_MAX}`);
  }
  return value;
}

/** DynamoDB attribute values -> plain JS (S, N, BOOL, NULL, M, L, SS, NS). */
export function unmarshal(value) {
  if (value === undefined || value === null) return undefined;
  if ("S" in value) return value.S;
  if ("N" in value) return Number(value.N);
  if ("BOOL" in value) return value.BOOL;
  if ("NULL" in value) return null;
  if ("M" in value) return Object.fromEntries(Object.entries(value.M).map(([k, v]) => [k, unmarshal(v)]));
  if ("L" in value) return value.L.map(unmarshal);
  if ("SS" in value) return [...value.SS];
  if ("NS" in value) return value.NS.map(Number);
  return undefined;
}

export const itemToState = (item) => (item ? unmarshal({ M: item }) : null);

/**
 * The arm and the endpoint entry it serves. Rollout stages (prod) carry `stable` and `candidate`
 * entries; every other stage has one entry, named after its endpoint. Returns null when the
 * state names no endpoint to serve.
 */
export function pick(state, conversationId) {
  const endpoints = state?.endpoints || {};
  if (endpoints.stable) {
    const arm = route(state, conversationId);
    const qualifier = arm === "candidate" && endpoints.candidate ? "candidate" : "stable";
    return { arm: qualifier, qualifier, entry: endpoints[qualifier] };
  }
  const names = Object.keys(endpoints);
  if (names.length !== 1) return null;
  return { arm: names[0], qualifier: names[0], entry: endpoints[names[0]] };
}

/**
 * Control-item reader with a 5 s cache per Lambda instance. On a read failure it keeps the last
 * good value; with none it returns null and logs router_state_unavailable (the caller then
 * routes stable, or answers 503 when no endpoint entry exists).
 */
export function stateReader(read, { now = () => Date.now(), log = (line) => console.log(line) } = {}) {
  let cached = null;
  let readAt = -Infinity;
  return async function current() {
    if (cached && now() - readAt < STATE_CACHE_MS) return cached;
    try {
      cached = itemToState(await read());
      readAt = now();
    } catch (error) {
      log(JSON.stringify({ event: "router_state_unavailable", error_type: error?.name || "Error", cached: Boolean(cached) }));
    }
    return cached;
  };
}
