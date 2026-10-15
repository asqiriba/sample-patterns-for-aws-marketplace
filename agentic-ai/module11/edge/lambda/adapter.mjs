// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// m11 /invoke adapter: validate, admit, record, route and invoke, stream and close
// out, shadow and release. It reuses Module 10's validateRequest by importing Module 10's
// adapter.mjs, packaged unchanged as m10adapter.mjs beside this file (edge.txt), so the
// header-spoofing, size and key rules stay Module 10's. Differences from Module 10:
//  - an N-slot team semaphore (TEAM_CONCURRENCY) instead of one lease per team;
//  - the session-hash router (router.mjs) picks the endpoint qualifier per conversation;
//  - release identity is stamped from the control item's endpoints entry;
//  - traceparent is forwarded only when X-Ray sampled the request (flags 01);
//  - an outcome item per request, shadow enqueue, and StopRuntimeSession when done.
import { createHash, randomUUID } from "node:crypto";
import { once } from "node:events";
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand, StopRuntimeSessionCommand } from "@aws-sdk/client-bedrock-agentcore";
import { DynamoDBClient, GetItemCommand, PutItemCommand, UpdateItemCommand, DeleteItemCommand } from "@aws-sdk/client-dynamodb";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";
import { SQSClient, SendMessageCommand } from "@aws-sdk/client-sqs";
import { corsHeaders } from "./cors.mjs";
import { validateRequest } from "./m10adapter.mjs";
import { conversationId, pick, sessionId, shadowEnqueue, stateReader } from "./router.mjs";

const database = new DynamoDBClient({});
const core = new BedrockAgentCoreClient({ maxAttempts: 1 });
const s3 = new S3Client({});
const sqs = new SQSClient({});

const LEASE_SECONDS = 300;
const OUTCOME_TTL_SECONDS = 30 * 86400;
const MAX_BODY = 16384;
export const BUSY = { error: "Team already has an active investigation", source: "team-concurrency" };

const env = (name) => process.env[name];
const readControl = stateReader(async () => (await database.send(new GetItemCommand({
  TableName: env("CONTROL_TABLE"), Key: { pk: { S: "ROLLOUT" }, sk: { S: "CURRENT" } },
}))).Item);

/** Step 1: Module 10's validateRequest on the body without conversation_id, plus the raw size. */
export function validate(event) {
  let stripped = event;
  let conversation;
  if (typeof event.body === "string" && !event.isBase64Encoded) {
    try {
      const parsed = JSON.parse(event.body);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && "conversation_id" in parsed) {
        const { conversation_id: value, ...rest } = parsed;
        conversation = value;
        stripped = { ...event, body: JSON.stringify(rest) };
      }
    } catch { /* validateRequest answers invalid JSON */ }
  }
  const envelope = validateRequest(stripped);
  if (Buffer.byteLength(event.body) > MAX_BODY) throw Object.assign(new Error("Invalid request body"), { status: 413 });
  return { envelope, conversation: conversationId({ conversation_id: conversation }, envelope.request.request_id) };
}

/** Envelope traceparent only when X-Ray sampled this request (Sampled=1 -> flags 01). */
export function traceparentFrom(traceHeader) {
  const xray = Object.fromEntries((traceHeader || "").split(";").map((p) => p.split("=")));
  if (xray.Sampled !== "1" || !/^1-[a-f0-9]{8}-[a-f0-9]{24}$/.test(xray.Root || "") || !/^[a-f0-9]{16}$/.test(xray.Parent || "")) {
    return undefined;
  }
  return `00-${xray.Root.slice(2).replace("-", "")}-${xray.Parent}-01`;
}

/** Step 2: N-slot team semaphore. Conditional PutItem on TEAM#<t>#SLOT#<k>, from a random offset. */
export async function admit(send, table, team, owner, slots, now, offset = Math.floor(Math.random() * slots)) {
  for (let i = 0; i < slots; i += 1) {
    const k = (offset + i) % slots;
    const key = { pk: { S: `TEAM#${team}#SLOT#${k}` }, sk: { S: "ACTIVE" } };
    try {
      await send(new PutItemCommand({
        TableName: table, Item: { ...key, owner: { S: owner }, expiresAt: { N: String(now + LEASE_SECONDS) } },
        ConditionExpression: "attribute_not_exists(pk) OR expiresAt < :now",
        ExpressionAttributeValues: { ":now": { N: String(now) } },
      }));
      return key;
    } catch (error) {
      if (error.name !== "ConditionalCheckFailedException") throw error;
    }
  }
  return null;
}

/** Step 4: the runtime envelope. Identity comes from the routed qualifier's endpoints entry. */
export function buildEnvelope(validated, picked, state, traceparent) {
  const { envelope, conversation } = validated;
  const context = {
    conversationId: conversation, variant: picked.arm,
    manifestVersion: picked.entry.manifestVersion, behaviorDigest: picked.entry.behaviorDigest,
    release: picked.entry.release,
  };
  if (state?.rolloutId && state.state !== "IDLE") context.rolloutId = state.rolloutId;
  if (traceparent) context.traceparent = traceparent;
  return { identity: envelope.identity, request: envelope.request, context };
}

const S = (value) => ({ S: String(value) });
const N = (value) => ({ N: String(Number(value) || 0) });
const L = (values) => ({ L: values.map(S) });

/** Step 5: the outcome item the controller's signals read. */
export function outcomeItem({ envelope, picked, state, runtimeSession, result, httpStatus, startedMs, firstEventMs, nowMs }) {
  const day = new Date(nowMs).toISOString().slice(0, 10).replaceAll("-", "");
  const rollout = state?.rolloutId && state.state !== "IDLE" ? state.rolloutId : null;
  const events = Array.isArray(result?.events) ? result.events : [];
  const usage = result?.usage || {};
  const m11 = result?.m11 || {};
  return {
    pk: S(rollout ? `ROLLOUT#${rollout}` : `NONE#${day}`), sk: S(`${nowMs}#${envelope.request.request_id}`),
    arm: S(picked.arm), qualifier: S(picked.qualifier), runtimeVersion: S(picked.entry.version),
    runtimeSessionId: S(runtimeSession), conversationId: S(envelope.context.conversationId),
    manifestVersion: S(envelope.context.manifestVersion), behaviorDigest: S(envelope.context.behaviorDigest),
    release: S(envelope.context.release), runtimeDigest: S(m11.runtimeDigest || "unknown"),
    team: S(envelope.identity.team), status: S(result?.status || "failed"), httpStatus: N(httpStatus),
    latencyMs: N(nowMs - startedMs), firstEventMs: N(firstEventMs ?? nowMs - startedMs),
    toolsOffered: L(Array.isArray(m11.toolsOffered) ? m11.toolsOffered : []),
    toolsExecuted: L(events.filter((e) => e.status === "completed").map((e) => e.tool)),
    toolFailures: L(events.filter((e) => e.status === "failed").map((e) => e.tool)),
    inputTokens: N(usage.input_tokens), outputTokens: N(usage.output_tokens),
    cachedInputTokens: N(m11.cachedInputTokens), toolCalls: N(events.length),
    planner: { BOOL: events.some((e) => e.agent === "Recovery Planner") }, shadow: { BOOL: false },
    guardrailAction: S(m11.guardrail?.action || "NONE"),
    expiresAt: N(Math.floor(nowMs / 1000) + OUTCOME_TTL_SECONDS),
  };
}

/** Step 6: the shadow work item, with the candidate's identity from endpoints.candidate. */
export function shadowMessage(envelope, state) {
  const candidate = state.endpoints.candidate;
  return {
    conversationId: envelope.context.conversationId, change_summary: envelope.request.change_summary,
    team: envelope.identity.team, principal: envelope.identity.principal,
    originalRequestId: envelope.request.request_id, rolloutId: state.rolloutId,
    candidate: { manifestVersion: candidate.manifestVersion, behaviorDigest: candidate.behaviorDigest, release: candidate.release },
  };
}

function jsonReply(stream, statusCode, body) {
  const response = awslambda.HttpResponseStream.from(stream, {
    statusCode, headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...corsHeaders() },
  });
  response.write(JSON.stringify(body));
  response.end();
}

const log = (fields) => console.log(JSON.stringify(fields));

async function run(event, rawStream, context) {
  let validated;
  try { validated = validate(event); } catch (error) { jsonReply(rawStream, error.status || 400, { error: error.message }); return; }
  const { team, principal } = validated.envelope.identity;
  const requestId = validated.envelope.request.request_id;
  const table = env("TABLE_NAME");
  const key = { pk: { S: `TEAM#${team}#USER#${principal}` }, sk: { S: `REQUEST#${requestId}` } };
  const hash = createHash("sha256").update(validated.envelope.request.change_summary).digest("hex");
  const owner = randomUUID();
  const startedMs = Date.now();
  const now = Math.floor(startedMs / 1000);
  const remainingMs = context?.getRemainingTimeInMillis?.() ?? 225_000;
  const invokeTimeoutMs = Math.max(10_000, remainingMs - 30_000);
  const send = (command) => database.send(command);
  let slot = null;
  let admitted = false;
  let stream;
  let picked;
  let state;
  let envelope;
  let runtimeSession;
  let firstEventMs;
  try {
    const previous = (await send(new GetItemCommand({ TableName: table, Key: key, ConsistentRead: true }))).Item;
    if (previous) {
      if (previous.requestHash.S !== hash) { jsonReply(rawStream, 409, { error: "Request ID already used for a different request" }); return; }
      jsonReply(rawStream, 202, { request_id: requestId, status: previous.status.S, result_url: `requests/${requestId}` }); return;
    }
    slot = await admit(send, table, team, owner, Math.max(1, Number.parseInt(env("TEAM_CONCURRENCY") || "1", 10)), now);
    if (!slot) { jsonReply(rawStream, 429, BUSY); return; }
    try {
      await send(new PutItemCommand({ TableName: table,
        Item: { ...key, requestHash: { S: hash }, sessionId: { S: owner }, status: { S: "RUNNING" },
          expiresAt: { N: String(now + 86400) }, deadline: { N: String(now + Math.ceil(invokeTimeoutMs / 1000) + 30) } },
        ConditionExpression: "attribute_not_exists(pk)" }));
      admitted = true;
    } catch (error) {
      if (error.name !== "ConditionalCheckFailedException") throw error;
      jsonReply(rawStream, 409, { error: "Request already admitted", request_id: requestId }); return;
    }
    state = await readControl();
    picked = pick(state, validated.conversation);
    if (!picked?.entry?.version) {
      log({ event: "router_state_unavailable", request_id: requestId });
      await send(new UpdateItemCommand({ TableName: table, Key: key, UpdateExpression: "SET #s = :s",
        ExpressionAttributeNames: { "#s": "status" }, ExpressionAttributeValues: { ":s": { S: "FAILED" } } })).catch(() => {});
      admitted = false;
      jsonReply(rawStream, 503, { error: "Release routing is unavailable", request_id: requestId }); return;
    }
    envelope = buildEnvelope(validated, picked, state, traceparentFrom(process.env._X_AMZN_TRACE_ID));
    runtimeSession = sessionId(env("SESSION_STAGE"), picked.qualifier, picked.entry.version);
    const response = await core.send(new InvokeAgentRuntimeCommand({
      agentRuntimeArn: env("RUNTIME_ARN"), qualifier: picked.qualifier, runtimeSessionId: runtimeSession,
      contentType: "application/json", accept: "text/event-stream", payload: Buffer.from(JSON.stringify(envelope)),
    }), { abortSignal: AbortSignal.timeout(invokeTimeoutMs) });
    stream = awslambda.HttpResponseStream.from(rawStream, {
      statusCode: 200, headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store",
        "X-Request-Id": requestId, ...corsHeaders() },
    });
    for await (const chunk of response.response) {
      firstEventMs ??= Date.now() - startedMs;
      if (!stream.write(chunk)) await once(stream, "drain");
    }
    // The persisted result, not HTTP 200, decides completion (Module 10's rule).
    const stored = await s3.send(new GetObjectCommand({ Bucket: env("ARTIFACT_BUCKET"), Key: `results/${team}/${principal}/${requestId}.json` }));
    const result = JSON.parse(await stored.Body.transformToString());
    await send(new UpdateItemCommand({ TableName: table, Key: key, UpdateExpression: "SET #s = :s",
      ExpressionAttributeNames: { "#s": "status" },
      ExpressionAttributeValues: { ":s": { S: result.status === "completed" ? "COMPLETED" : "FAILED" } } }));
    admitted = false;
    await send(new PutItemCommand({ TableName: env("OUTCOMES_TABLE"), Item: outcomeItem({
      envelope, picked, state, runtimeSession, result, httpStatus: 200, startedMs, firstEventMs, nowMs: Date.now() }) }))
      .catch((error) => log({ event: "outcome_write_failed", request_id: requestId, error_type: error.name }));
    log({ event: "invocation_complete", request_id: requestId, gateway_request_id: event.requestContext?.requestId,
      team, arm: picked.arm, status: result.status, trace_id: result.trace_id });
    stream.end();
  } catch (error) {
    // A failed InvokeAgentRuntime answers 502 "Invocation failed" without the error class, and the outcome item
    // counts it in the arm's error rate. Callers cannot prove the request never reached the container, so
    // they never re-send it (the transport rule, evaluation/runner.py transport_reinvokes).
    if (admitted) {
      await send(new UpdateItemCommand({ TableName: table, Key: key, UpdateExpression: "SET #s = :s",
        ExpressionAttributeNames: { "#s": "status" }, ExpressionAttributeValues: { ":s": { S: "FAILED" } } })).catch(() => {});
    }
    if (envelope) {
      await send(new PutItemCommand({ TableName: env("OUTCOMES_TABLE"), Item: outcomeItem({
        envelope, picked, state, runtimeSession, result: { status: "failed" }, httpStatus: 502, startedMs, firstEventMs, nowMs: Date.now() }) }))
        .catch(() => {});
    }
    log({ event: "invocation_failed", request_id: requestId, error_type: error.name });
    if (stream) {
      stream.write(`event: error\ndata: ${JSON.stringify({ status: "failed", request_id: requestId, error: "Invocation failed" })}\n\n`);
      stream.end();
    } else jsonReply(rawStream, 502, { error: "Invocation failed", request_id: requestId });
  } finally {
    if (envelope && env("SHADOW_QUEUE_URL") && state?.endpoints?.candidate && shadowEnqueue(state, envelope.context.conversationId)) {
      await sqs.send(new SendMessageCommand({ QueueUrl: env("SHADOW_QUEUE_URL"), MessageBody: JSON.stringify(shadowMessage(envelope, state)) }))
        .catch((error) => log({ event: "shadow_enqueue_failed", request_id: requestId, error_type: error.name }));
    }
    if (runtimeSession) {
      await core.send(new StopRuntimeSessionCommand({ runtimeSessionId: runtimeSession, agentRuntimeArn: env("RUNTIME_ARN"), qualifier: picked.qualifier }))
        .catch((error) => log({ level: "WARN", event: "stop_session_failed", request_id: requestId, error_type: error.name }));
    }
    if (slot) await send(new DeleteItemCommand({ TableName: table, Key: slot, ConditionExpression: "#owner = :owner",
      ExpressionAttributeNames: { "#owner": "owner" }, ExpressionAttributeValues: { ":owner": { S: owner } } })).catch(() => {});
  }
}

export const handler = globalThis.awslambda ? awslambda.streamifyResponse(run) : run;
