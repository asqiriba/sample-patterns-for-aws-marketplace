// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// The m11 /invoke adapter (8.1 test_module11_adapter.mjs). The real handler runs with
// its AWS SDK transport replaced, and with ./cors.mjs and ./m10adapter.mjs resolved to Module 10's
// unchanged files, exactly as edge.txt packages them.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { registerHooks } from "node:module";

const M10 = new URL("../module10/deployment/lambda/", import.meta.url);
const EDGE = new URL("../module11/edge/lambda/", import.meta.url);
const sdk = `
class Client { constructor(config) { this.config = config; } send(command, options) { return globalThis.__send(this.constructor.service, command, options); } }
class Command { constructor(input) { this.input = input; } }
export class DynamoDBClient extends Client { static service = "ddb"; }
export class S3Client extends Client { static service = "s3"; }
export class SQSClient extends Client { static service = "sqs"; }
export class BedrockAgentCoreClient extends Client { static service = "core"; }
export class GetItemCommand extends Command {}
export class PutItemCommand extends Command {}
export class UpdateItemCommand extends Command {}
export class DeleteItemCommand extends Command {}
export class GetObjectCommand extends Command {}
export class SendMessageCommand extends Command {}
export class InvokeAgentRuntimeCommand extends Command {}
export class StopRuntimeSessionCommand extends Command {}
`;
registerHooks({
  resolve(specifier, context, next) {
    if (specifier.startsWith("@aws-sdk/")) return { url: "mock:aws-sdk", shortCircuit: true };
    if (context.parentURL?.startsWith(EDGE.href) && ["./cors.mjs", "./m10adapter.mjs"].includes(specifier)) {
      const name = specifier === "./m10adapter.mjs" ? "adapter.mjs" : specifier.slice(2);
      return { url: new URL(name, M10).href, shortCircuit: true };
    }
    return next(specifier, context);
  },
  load(url, context, next) {
    if (url === "mock:aws-sdk") return { format: "module", source: sdk, shortCircuit: true };
    return next(url, context);
  },
});

class Stream {
  constructor() { this.meta = null; this.chunks = []; this.ended = false; }
  write(chunk) { this.chunks.push(Buffer.from(chunk).toString()); return true; }
  end() { this.ended = true; }
  get text() { return this.chunks.join(""); }
}
globalThis.awslambda = {
  streamifyResponse: (fn) => fn,
  HttpResponseStream: { from: (stream, meta) => { stream.meta = meta; return stream; } },
};
Object.assign(process.env, {
  TABLE_NAME: "requests", CONTROL_TABLE: "control", OUTCOMES_TABLE: "outcomes", ARTIFACT_BUCKET: "artifacts",
  RUNTIME_ARN: "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/m11_prod_companion-abc",
  SESSION_STAGE: "prod", TEAM_CONCURRENCY: "14", SHADOW_QUEUE_URL: "https://sqs.example/q",
});
const adapter = await import(new URL("adapter.mjs", EDGE).href);
const { handler, admit, BUSY, traceparentFrom, buildEnvelope, outcomeItem, shadowMessage, validate } = adapter;
const { bucket } = await import(new URL("router.mjs", EDGE).href);

// ------------------------------------------------------------------ fake AWS
const ENDPOINTS = {
  stable: { version: "7", manifestVersion: "2026-10-19T10:00:00Z/1111111", behaviorDigest: `sha256:${"1".repeat(64)}`, release: "harness-v7" },
  candidate: { version: "8", manifestVersion: "2026-10-20T14:02:11Z/abc1234", behaviorDigest: `sha256:${"8".repeat(64)}`, release: "harness-v8" },
};
const ROLLOUT = "r-0123456789abcdef";
const S = (v) => ({ S: v });
const N = (v) => ({ N: String(v) });
const entryItem = (e) => ({ M: Object.fromEntries(Object.entries(e).map(([k, v]) => [k, S(v)])) });
const controlItem = (state, canaryPct, shadowPct = 0, endpoints = ENDPOINTS) => ({
  pk: S("ROLLOUT"), sk: S("CURRENT"), state: S(state), rolloutId: S(ROLLOUT), canaryPct: N(canaryPct), shadowPct: N(shadowPct),
  endpoints: { M: Object.fromEntries(Object.entries(endpoints).map(([k, e]) => [k, entryItem(e)])) },
});

let tables;
let calls;
let control;
let resultFor;
// The adapter caches the control item for 5 s per Lambda instance; a new control item takes
// effect after the cache window, so the tests move the clock past it.
const realNow = Date.now.bind(Date);
let clockOffset = 0;
Date.now = () => realNow() + clockOffset;
const setControl = (item) => { control = item; clockOffset += 6000; };
const conditionFailed = () => Object.assign(new Error("conditional"), { name: "ConditionalCheckFailedException" });
const keyOf = (key) => `${key.pk.S}|${key.sk.S}`;
function putItem({ TableName, Item, ConditionExpression, ExpressionAttributeValues }) {
  const table = tables[TableName] ||= new Map();
  const existing = table.get(keyOf(Item));
  if (ConditionExpression === "attribute_not_exists(pk)" && existing) throw conditionFailed();
  if (ConditionExpression === "attribute_not_exists(pk) OR expiresAt < :now" && existing
      && !(Number(existing.expiresAt.N) < Number(ExpressionAttributeValues[":now"].N))) throw conditionFailed();
  table.set(keyOf(Item), structuredClone(Item));
  return {};
}
globalThis.__send = async (service, command, options) => {
  const name = command.constructor.name;
  calls.push({ service, name, input: command.input, options });
  const input = command.input;
  if (name === "GetItemCommand") {
    if (input.TableName === "control") return { Item: control };
    return { Item: tables[input.TableName]?.get(keyOf(input.Key)) };
  }
  if (name === "PutItemCommand") return putItem(input);
  if (name === "UpdateItemCommand") {
    const item = tables[input.TableName].get(keyOf(input.Key));
    item.status = input.ExpressionAttributeValues[":s"];
    return {};
  }
  if (name === "DeleteItemCommand") {
    const table = tables[input.TableName];
    if (table.get(keyOf(input.Key))?.owner.S !== input.ExpressionAttributeValues[":owner"].S) throw conditionFailed();
    table.delete(keyOf(input.Key));
    return {};
  }
  if (name === "InvokeAgentRuntimeCommand") {
    const envelope = JSON.parse(Buffer.from(input.payload).toString());
    const result = resultFor(envelope);
    tables.__results.set(`results/${envelope.identity.team}/${envelope.identity.principal}/${envelope.request.request_id}.json`, result);
    return { response: (async function* stream() {
      yield Buffer.from(`event: accepted\ndata: {}\n\n`);
      yield Buffer.from(`event: result\ndata: ${JSON.stringify(result)}\n\n`);
    })() };
  }
  if (name === "GetObjectCommand") {
    const body = tables.__results.get(input.Key);
    if (!body) throw Object.assign(new Error("missing"), { name: "NoSuchKey" });
    return { Body: { transformToString: async () => JSON.stringify(body) } };
  }
  if (name === "SendMessageCommand" || name === "StopRuntimeSessionCommand") return {};
  throw new Error(`unexpected ${service} ${name}`);
};
const defaultResult = (envelope) => ({
  status: "completed", request_id: envelope.request.request_id, trace_id: "a".repeat(32),
  events: [{ agent: "DevOps Companion", tool: "get_deployment_history", status: "completed" },
    { agent: "DevOps Companion", tool: "query_metrics", status: "failed" },
    { agent: "Recovery Planner", tool: "get_blast_radius", status: "completed" }],
  usage: { input_tokens: 300, output_tokens: 90, total_tokens: 390 },
  m11: { ...envelope.context, runtimeDigest: `sha256:${"d".repeat(64)}`, toolsOffered: ["get_deployment_history", "query_metrics"],
    cachedInputTokens: 128, guardrail: { action: "NONE" } },
});
beforeEach(() => {
  tables = { __results: new Map() };
  calls = [];
  setControl(controlItem("WARMING", 50));
  resultFor = defaultResult;
  delete process.env._X_AMZN_TRACE_ID;
});

const event = (body, { team = "platform", principal = "user-1", headers = {} } = {}) => ({
  requestContext: { authorizer: { principalId: principal, team }, requestId: "gw-1" }, headers,
  body: typeof body === "string" ? body : JSON.stringify(body),
});
async function call(body, options) {
  const stream = new Stream();
  await handler(event(body, options), stream, { getRemainingTimeInMillis: () => 225_000 });
  return { stream, status: stream.meta?.statusCode, json: stream.meta?.headers["Content-Type"] === "application/json" ? JSON.parse(stream.text) : null };
}
const sent = (name) => calls.filter((c) => c.name === name);
const invoked = () => JSON.parse(Buffer.from(sent("InvokeAgentRuntimeCommand")[0].input.payload).toString());
const outcome = () => sent("PutItemCommand").find((c) => c.input.TableName === "outcomes")?.input.Item;
// Conversation IDs that land in each arm at canaryPct 50.
const CONV = { stable: null, candidate: null };
for (let i = 0; !CONV.stable || !CONV.candidate; i += 1) {
  const id = `conv-${String(i).padStart(6, "0")}`;
  CONV[bucket(ROLLOUT, id) < 50 ? "candidate" : "stable"] ||= id;
}

// ------------------------------------------------------------------ validation
test("body validation goes through Module 10's validateRequest", async () => {
  for (const [body, options, status] of [
    ["{not json", {}, 400],
    [{ change_summary: "x", request_id: "r1", shadow: true }, {}, 400],        // a client cannot ask for shadow
    [{ change_summary: "x", variant: "candidate" }, {}, 400],
    [{ change_summary: "   " }, {}, 400],
    [{ change_summary: "x" }, { headers: { "X-Demo-Team": "payments" } }, 400], // header spoofing
    [{ change_summary: "x" }, { team: "bad team" }, 403],
    [{ change_summary: "x".repeat(4001) }, {}, 400],
    [{ change_summary: "x", conversation_id: "short" }, {}, 400],
    [{ change_summary: "x", conversation_id: "c".repeat(40), pad: "y".repeat(16400) }, {}, 413],
  ]) {
    const { status: got, json } = await call(body, options);
    assert.equal(got, status, JSON.stringify(body).slice(0, 80));
    assert.ok(json.error);
  }
  assert.equal(sent("InvokeAgentRuntimeCommand").length, 0);
  assert.throws(() => validate(event({ change_summary: "x", conversation_id: "c".repeat(16), pad: "y".repeat(16400) })),
    (error) => error.status === 413);
});

test("conversation_id is routing-only: stripped before validation, defaulting to the request ID", async () => {
  const v = validate(event({ change_summary: "  checkout 502s ", request_id: "req-1", conversation_id: "conv_ABC-123" }));
  assert.deepEqual(v.envelope.request, { change_summary: "checkout 502s", request_id: "req-1" });
  assert.equal(v.conversation, "conv_ABC-123");
  assert.equal(validate(event({ change_summary: "x", request_id: "req-2" })).conversation, "req-2");
  await call({ change_summary: "checkout 502s", request_id: "req-3", conversation_id: CONV.candidate });
  const envelope = invoked();
  assert.equal(envelope.context.conversationId, CONV.candidate);
  assert.ok(!("conversation_id" in envelope.request));
});

// ------------------------------------------------------------------ admission
test("N-slot team semaphore: 14 slots admit 14 and reject the 15th", async () => {
  tables.requests = new Map();
  const send = async (command) => putItem(command.input);
  const keys = [];
  for (let i = 0; i < 14; i += 1) keys.push(await admit(send, "requests", "platform", `owner-${i}`, 14, 1000, i % 3));
  assert.equal(new Set(keys.map((k) => k.pk.S)).size, 14);
  assert.ok(keys.every((k) => /^TEAM#platform#SLOT#(\d|1[0-3])$/.test(k.pk.S) && k.sk.S === "ACTIVE"));
  assert.equal(await admit(send, "requests", "platform", "owner-14", 14, 1000), null);
  assert.ok(await admit(send, "requests", "payments", "other-team", 14, 1000));          // per team
  assert.ok(await admit(send, "requests", "platform", "after-lease", 14, 1000 + 301));    // 300 s lease expired
});

test("a full team answers 429 with Module 10's team-concurrency body and invokes nothing", async () => {
  const now = Math.floor(Date.now() / 1000);
  tables.requests = new Map();
  for (let k = 0; k < 14; k += 1) {
    putItem({ TableName: "requests", Item: { pk: S(`TEAM#platform#SLOT#${k}`), sk: S("ACTIVE"), owner: S(`o${k}`), expiresAt: N(now + 300) } });
  }
  const { status, json } = await call({ change_summary: "checkout 502s", request_id: "busy-1" });
  assert.equal(status, 429);
  assert.deepEqual(json, BUSY);
  assert.equal(json.source, "team-concurrency");
  assert.equal(sent("InvokeAgentRuntimeCommand").length, 0);
});

// ------------------------------------------------------------------ routing, identity, outcome
test("each arm is recorded, and the envelope's identity is the routed qualifier's endpoints entry", async () => {
  for (const arm of ["stable", "candidate"]) {
    calls = [];
    const { stream, status } = await call({ change_summary: "checkout 502s", request_id: `req-${arm}`, conversation_id: CONV[arm] });
    assert.equal(status, 200);
    assert.match(stream.text, /event: result/);
    const invoke = sent("InvokeAgentRuntimeCommand")[0].input;
    assert.equal(invoke.qualifier, arm);
    assert.match(invoke.runtimeSessionId, new RegExp(`^m11-prod-${arm}-v${ENDPOINTS[arm].version}-[0-9a-f-]{36}$`));
    const envelope = invoked();
    assert.deepEqual(envelope.identity, { team: "platform", principal: "user-1" });
    assert.equal(envelope.context.variant, arm);
    assert.equal(envelope.context.rolloutId, ROLLOUT);
    assert.ok(!("shadow" in envelope.context));                                       // only the shadow worker sets it
    for (const key of ["manifestVersion", "behaviorDigest", "release"]) assert.equal(envelope.context[key], ENDPOINTS[arm][key]);
    const item = outcome();
    assert.equal(item.arm.S, arm);
    assert.equal(item.qualifier.S, arm);
    assert.equal(item.runtimeVersion.S, ENDPOINTS[arm].version);
    for (const key of ["manifestVersion", "behaviorDigest", "release"]) assert.equal(item[key].S, ENDPOINTS[arm][key]);
    assert.equal(item.runtimeDigest.S, `sha256:${"d".repeat(64)}`);
    assert.equal(item.runtimeSessionId.S, invoke.runtimeSessionId);
    assert.equal(tables.requests.get(`TEAM#platform#USER#user-1|REQUEST#req-${arm}`).status.S, "COMPLETED");
    assert.equal(sent("StopRuntimeSessionCommand")[0].input.runtimeSessionId, invoke.runtimeSessionId);
    assert.equal([...tables.requests.keys()].filter((k) => k.includes("#SLOT#")).length, 0);   // slot released
  }
});

test("the same conversation is session-sticky across requests", async () => {
  for (const id of ["a1", "a2", "a3"]) await call({ change_summary: "checkout 502s", request_id: id, conversation_id: CONV.candidate });
  assert.deepEqual(sent("InvokeAgentRuntimeCommand").map((c) => c.input.qualifier), ["candidate", "candidate", "candidate"]);
  const sessions = sent("InvokeAgentRuntimeCommand").map((c) => c.input.runtimeSessionId);
  assert.equal(new Set(sessions).size, 3);                                             // fresh session per request
});

test("outcome item shape", async () => {
  await call({ change_summary: "checkout 502s", request_id: "shape-1", conversation_id: CONV.stable });
  const item = outcome();
  assert.deepEqual(Object.keys(item).sort(), ["arm", "behaviorDigest", "cachedInputTokens", "conversationId", "expiresAt",
    "firstEventMs", "guardrailAction", "httpStatus", "inputTokens", "latencyMs", "manifestVersion", "outputTokens", "pk",
    "planner", "qualifier", "release", "runtimeDigest", "runtimeSessionId", "runtimeVersion", "shadow", "sk", "status",
    "team", "toolCalls", "toolFailures", "toolsExecuted", "toolsOffered"]);
  assert.equal(item.pk.S, `ROLLOUT#${ROLLOUT}`);
  assert.match(item.sk.S, /^\d{13}#shape-1$/);
  assert.deepEqual(item.toolsExecuted, { L: [S("get_deployment_history"), S("get_blast_radius")] });
  assert.deepEqual(item.toolFailures, { L: [S("query_metrics")] });
  assert.deepEqual(item.toolsOffered, { L: [S("get_deployment_history"), S("query_metrics")] });
  assert.deepEqual([item.inputTokens, item.outputTokens, item.cachedInputTokens, item.toolCalls], [N(300), N(90), N(128), N(3)]);
  assert.deepEqual([item.planner, item.shadow, item.status, item.httpStatus], [{ BOOL: true }, { BOOL: false }, S("completed"), N(200)]);
  assert.equal(item.guardrailAction.S, "NONE");
  assert.ok(Number(item.expiresAt.N) > Date.now() / 1000 + 29 * 86400);
  const idle = outcomeItem({ envelope: invoked(), picked: { arm: "dev", qualifier: "dev", entry: ENDPOINTS.stable },
    state: { state: "IDLE", rolloutId: ROLLOUT }, runtimeSession: "s", result: { status: "failed" }, httpStatus: 502,
    startedMs: 0, nowMs: Date.UTC(2026, 9, 20) });
  assert.equal(idle.pk.S, "NONE#20261020");
  assert.equal(idle.runtimeDigest.S, "unknown");
});

test("a failed result still writes an outcome item and marks the request FAILED", async () => {
  resultFor = (envelope) => ({ ...defaultResult(envelope), status: "failed" });
  await call({ change_summary: "checkout 502s", request_id: "fail-1" });
  assert.equal(outcome().status.S, "failed");
  assert.equal(tables.requests.get("TEAM#platform#USER#user-1|REQUEST#fail-1").status.S, "FAILED");
});

test("shadow enqueue carries the candidate entry", async () => {
  setControl(controlItem("OBSERVING", 50, 100));
  await call({ change_summary: "checkout 502s", request_id: "shadow-1", conversation_id: CONV.stable });
  const [message] = sent("SendMessageCommand");
  const body = JSON.parse(message.input.MessageBody);
  assert.deepEqual(body.candidate, { manifestVersion: ENDPOINTS.candidate.manifestVersion,
    behaviorDigest: ENDPOINTS.candidate.behaviorDigest, release: ENDPOINTS.candidate.release });
  assert.deepEqual({ ...body, candidate: undefined }, { conversationId: CONV.stable, change_summary: "checkout 502s",
    team: "platform", principal: "user-1", originalRequestId: "shadow-1", rolloutId: ROLLOUT, candidate: undefined });
  setControl(controlItem("OBSERVING", 50, 0));
  calls = [];
  await call({ change_summary: "checkout 502s", request_id: "shadow-2", conversation_id: CONV.stable });
  assert.equal(sent("SendMessageCommand").length, 0);
  assert.deepEqual(shadowMessage(invoked(), { rolloutId: ROLLOUT, endpoints: ENDPOINTS }).candidate.release, "harness-v8");
});

test("non-rollout stages serve their single endpoint; no routable entry answers 503", async () => {
  setControl({ pk: S("ROLLOUT"), sk: S("CURRENT"), state: S("IDLE"), endpoints: { M: { dev: entryItem(ENDPOINTS.stable) } } });
  await call({ change_summary: "checkout 502s", request_id: "dev-1" });
  assert.equal(sent("InvokeAgentRuntimeCommand")[0].input.qualifier, "dev");
  assert.equal(invoked().context.variant, "dev");
  assert.ok(!("rolloutId" in invoked().context));
  setControl({ pk: S("ROLLOUT"), sk: S("CURRENT"), state: S("IDLE"), endpoints: { M: {} } });
  calls = [];
  const { status, json } = await call({ change_summary: "checkout 502s", request_id: "none-1" });
  assert.equal(status, 503);
  assert.equal(json.error, "Release routing is unavailable");
  assert.equal(sent("InvokeAgentRuntimeCommand").length, 0);
});

test("a replayed request ID answers 202 with the stored status; a different body is 409", async () => {
  await call({ change_summary: "checkout 502s", request_id: "dup-1" });
  calls = [];
  const again = await call({ change_summary: "checkout 502s", request_id: "dup-1" });
  assert.equal(again.status, 202);
  assert.deepEqual(again.json, { request_id: "dup-1", status: "COMPLETED", result_url: "requests/dup-1" });
  assert.equal((await call({ change_summary: "something else", request_id: "dup-1" })).status, 409);
  assert.equal(sent("InvokeAgentRuntimeCommand").length, 0);
});

// ------------------------------------------------------------------ traceparent
test("traceparent is forwarded with flags 01 only when X-Ray sampled the request", async () => {
  const root = "1-5759e988-bd862e3fe1be46a994272793";
  assert.equal(traceparentFrom(`Root=${root};Parent=53995c3f42cd8ad8;Sampled=1`), "00-5759e988bd862e3fe1be46a994272793-53995c3f42cd8ad8-01");
  assert.equal(traceparentFrom(`Root=${root};Parent=53995c3f42cd8ad8;Sampled=0`), undefined);
  assert.equal(traceparentFrom(`Root=${root};Sampled=1`), undefined);
  assert.equal(traceparentFrom(undefined), undefined);
  process.env._X_AMZN_TRACE_ID = `Root=${root};Parent=53995c3f42cd8ad8;Sampled=1`;
  await call({ change_summary: "checkout 502s", request_id: "tp-1" });
  assert.equal(invoked().context.traceparent, "00-5759e988bd862e3fe1be46a994272793-53995c3f42cd8ad8-01");
  for (const header of [`Root=${root};Parent=53995c3f42cd8ad8;Sampled=0`, undefined]) {
    calls = [];
    if (header) process.env._X_AMZN_TRACE_ID = header; else delete process.env._X_AMZN_TRACE_ID;
    await call({ change_summary: "checkout 502s", request_id: `tp-${header ? 0 : "none"}` });
    assert.ok(!("traceparent" in invoked().context));
  }
  const built = buildEnvelope({ envelope: { identity: {}, request: {} }, conversation: "c-12345678" },
    { arm: "stable", entry: ENDPOINTS.stable }, { state: "IDLE" }, undefined);
  assert.deepEqual(Object.keys(built.context).sort(), ["behaviorDigest", "conversationId", "manifestVersion", "release", "variant"]);
});
