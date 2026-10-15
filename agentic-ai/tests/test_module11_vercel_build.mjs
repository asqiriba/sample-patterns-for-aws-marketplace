// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// The Vercel build files (8.1 test_module11_vercel_build.mjs): config.json, the
// anchored patches over Module 10's UI, the ignored-build step, and vercel.json's disabled branches.
// Vercel matches `git.deploymentEnabled` keys with minimatch; Node 24's path.matchesGlob uses the
// same minimatch semantics, so no extra dependency is installed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, matchesGlob } from "node:path";
import { fileURLToPath } from "node:url";
import { BuildError, REPO_ROOT, applyPatches, build, configFrom } from "../module11/vercel/build.mjs";
import { decide } from "../module11/vercel/ignore.mjs";

const VERCEL = join(REPO_ROOT, "module11", "vercel");
const UI = join(REPO_ROOT, "module10", "ui");
const patches = JSON.parse(readFileSync(join(VERCEL, "patches.json"), "utf8")).patches;
const ENV = {
  M11_API_URL: "https://abc123.execute-api.us-east-1.amazonaws.com/mr-42/",
  M11_CLIENT_ID: "client-id-1",
  M11_AUTH_URL: "https://m11-mr-42.auth.us-east-1.amazoncognito.com",
  VERCEL_BRANCH_URL: "devops-companion-git-friday-brevity-example-team.vercel.app",
};
const tempOut = () => mkdtempSync(join(tmpdir(), "m11-vercel-"));

test("config.json has all six fields, with redirectUri from VERCEL_BRANCH_URL", () => {
  const out = tempOut();
  try {
    const result = build({ out, env: ENV });
    const config = JSON.parse(readFileSync(join(out, "config.json"), "utf8"));
    assert.deepEqual(Object.keys(config).sort(), ["apiUrl", "authUrl", "clientId", "example", "logzioUrl", "redirectUri"]);
    assert.equal(config.redirectUri, `https://${ENV.VERCEL_BRANCH_URL}/`);
    assert.equal(config.apiUrl, "https://abc123.execute-api.us-east-1.amazonaws.com/mr-42");
    assert.equal(config.clientId, "client-id-1");
    assert.equal(config.authUrl, ENV.M11_AUTH_URL);
    assert.equal(config.logzioUrl, null);
    assert.equal(config.example, readFileSync(join(VERCEL, "example.txt"), "utf8").trim());
    assert.deepEqual(result.config, config);
  } finally { rmSync(out, { recursive: true, force: true }); }
  const production = configFrom({ ...ENV, VERCEL_ENV: "production", M11_PROD_UI_ORIGIN: "https://companion.example.com/" }, "x");
  assert.equal(production.redirectUri, "https://companion.example.com/");
  assert.equal(configFrom({ ...ENV, VERCEL_BRANCH_URL: "https://b.vercel.app" }, "x").redirectUri, "https://b.vercel.app/");
});

test("missing or non-https inputs fail the build", () => {
  for (const [name, value] of [["M11_API_URL", ""], ["M11_CLIENT_ID", " "], ["M11_AUTH_URL", "http://insecure.example"],
    ["VERCEL_BRANCH_URL", ""]]) {
    assert.throws(() => configFrom({ ...ENV, [name]: value }, "x"), BuildError, name);
  }
  assert.throws(() => configFrom({ ...ENV, VERCEL_ENV: "production" }, "x"), /M11_PROD_UI_ORIGIN is required/);
  const out = tempOut();
  try {
    const env = { PATH: process.env.PATH, M11_CLIENT_ID: "c" };
    const run = spawnSync(process.execPath, [join(VERCEL, "build.mjs"), "--out", out], { env, encoding: "utf8" });
    assert.equal(run.status, 1);
    assert.match(run.stderr, /^m11 vercel build failed: (VERCEL_BRANCH_URL|M11_API_URL) is required$/m);
    assert.deepEqual(readdirSync(out), []);   // nothing written before the inputs validate
  } finally { rmSync(out, { recursive: true, force: true }); }
});

test("each patch anchor occurs exactly once in Module 10's UI, and the build copies every file", () => {
  for (const patch of patches) {
    const text = readFileSync(join(UI, patch.file), "utf8");
    assert.equal(text.split(patch.anchor).length - 1, 1, `${patch.file}: ${patch.anchor}`);
  }
  const out = tempOut();
  try {
    build({ out, env: ENV });
    const names = readdirSync(UI).sort();
    assert.deepEqual(readdirSync(out).sort(), [...names, "config.json"].sort());
    for (const name of names) {
      const original = readFileSync(join(UI, name));
      const built = readFileSync(join(out, name));
      const patch = patches.find((p) => p.file === name);
      if (!patch) { assert.ok(original.equals(built), name); continue; }
      const text = built.toString();
      assert.ok(text.includes(patch.replacement) && text.split(patch.anchor).length - 1 <= (patch.replacement.includes(patch.anchor) ? 1 : 0));
      assert.equal(text, original.toString().replace(patch.anchor, patch.replacement));
    }
  } finally { rmSync(out, { recursive: true, force: true }); }
});

test("a missing or repeated anchor fails the build", () => {
  const patch = { file: "app.mjs", anchor: "ANCHOR", replacement: "X" };
  assert.deepEqual(applyPatches({ "app.mjs": "a ANCHOR b" }, [patch]), { "app.mjs": "a X b" });
  assert.throws(() => applyPatches({ "app.mjs": "a b" }, [patch]), /occurs 0 times/);
  assert.throws(() => applyPatches({ "app.mjs": "ANCHOR ANCHOR" }, [patch]), /occurs 2 times/);
  assert.throws(() => applyPatches({}, [patch]), /patch target app.mjs is missing/);
  assert.deepEqual(applyPatches({ "app.mjs": "ANCHOR" }, [{ ...patch, replacement: "$& $1" }]), { "app.mjs": "$& $1" });
});

test("ignore.mjs: skip unwired branches and bot-authored commits, build otherwise", () => {
  const wired = { M11_API_URL: ENV.M11_API_URL, M11_BOT_LOGIN: "project_12345678_bot_example" };
  assert.equal(decide({}).exit, 0);
  assert.equal(decide({ M11_API_URL: "  " }).exit, 0);
  assert.equal(decide({ ...wired, VERCEL_GIT_COMMIT_AUTHOR_LOGIN: "presenter" }).exit, 1);
  assert.equal(decide({ ...wired, VERCEL_GIT_COMMIT_AUTHOR_LOGIN: "project_12345678_bot_example" }).exit, 0);
  assert.equal(decide({ ...wired, VERCEL_GIT_COMMIT_AUTHOR_LOGIN: "Project_12345678_BOT_example" }).exit, 0);
  assert.equal(decide({ M11_API_URL: ENV.M11_API_URL, VERCEL_GIT_COMMIT_AUTHOR_LOGIN: "" }).exit, 1);
  const script = join(VERCEL, "ignore.mjs");
  const exit = (env) => spawnSync(process.execPath, [script], { env: { PATH: process.env.PATH, ...env }, encoding: "utf8" }).status;
  assert.equal(exit({}), 0);
  assert.equal(exit({ ...wired, VERCEL_GIT_COMMIT_AUTHOR_LOGIN: "presenter" }), 1);
  assert.equal(exit({ ...wired, VERCEL_GIT_COMMIT_AUTHOR_LOGIN: wired.M11_BOT_LOGIN }), 0);
});

test("vercel.json disables git deployments for every bot branch and keeps humans' branches", () => {
  const config = JSON.parse(readFileSync(join(VERCEL, "vercel.json"), "utf8"));
  const patterns = config.git.deploymentEnabled;
  const enabled = (branch) => !Object.entries(patterns).some(([glob, on]) => on === false && matchesGlob(branch, glob));
  for (const branch of ["demo/payments-v9", "demo/revert-v9", "corpus/policy/pol-1", "recommendation/abc", "scenario/0123456789ab", "model/gpt-6-luna"]) {
    assert.equal(enabled(branch), false, branch);
  }
  for (const branch of ["friday-brevity", "main", "friday-brevity-dryrun-1"]) assert.equal(enabled(branch), true, branch);
  assert.ok(Object.values(patterns).every((v) => v === false));
  const csp = config.headers[0].headers.find((h) => h.key === "Content-Security-Policy").value;
  assert.match(csp, /frame-ancestors 'none'/);
  assert.match(csp, /connect-src 'self' https:\/\/\*\.execute-api\.us-east-1\.amazonaws\.com/);
});

test("Module 10's UI is never edited by the build", () => {
  const before = Object.fromEntries(readdirSync(UI).map((n) => [n, readFileSync(join(UI, n)).toString("base64")]));
  const out = tempOut();
  try { build({ out, env: ENV }); } finally { rmSync(out, { recursive: true, force: true }); }
  const after = Object.fromEntries(readdirSync(UI).map((n) => [n, readFileSync(join(UI, n)).toString("base64")]));
  assert.deepEqual(after, before);
  assert.equal(fileURLToPath(new URL("../module11/vercel/", import.meta.url)), `${VERCEL}/`);
});
