// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Vercel build for the Module 10 browser client. The Vercel project's root
// directory is the repository root, its build command is `node module11/vercel/build.mjs`, and
// its output directory is module11/vercel/dist.
//  1. Copies module10/ui/* into dist/ (Module 10's files are never edited).
//  2. Applies the copy-time patches in patches.json; each anchor must occur exactly once.
//  3. Writes the full dist/config.json with the six fields Module 10's ui.py writes.
// Inputs (Vercel env): M11_API_URL, M11_CLIENT_ID, M11_AUTH_URL, plus VERCEL_BRANCH_URL for a
// preview or M11_PROD_UI_ORIGIN for production (VERCEL_ENV=production). None are secrets.
import { copyFileSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(HERE, "..", "..");

export class BuildError extends Error {}

const required = (env, name) => {
  const value = (env[name] || "").trim();
  if (!value) throw new BuildError(`${name} is required`);
  return value;
};

const httpsUrl = (value, name) => {
  let url;
  try { url = new URL(value); } catch { throw new BuildError(`${name} must be an https URL`); }
  if (url.protocol !== "https:") throw new BuildError(`${name} must be an https URL`);
  return value.replace(/\/+$/, "");
};

/** The six config.json fields (apiUrl, clientId, authUrl, redirectUri, example, logzioUrl). */
export function configFrom(env, example) {
  const production = env.VERCEL_ENV === "production";
  const origin = production
    ? httpsUrl(required(env, "M11_PROD_UI_ORIGIN"), "M11_PROD_UI_ORIGIN")
    : httpsUrl(`https://${required(env, "VERCEL_BRANCH_URL").replace(/^https?:\/\//, "")}`, "VERCEL_BRANCH_URL");
  return {
    apiUrl: httpsUrl(required(env, "M11_API_URL"), "M11_API_URL"),
    clientId: required(env, "M11_CLIENT_ID"),
    authUrl: httpsUrl(required(env, "M11_AUTH_URL"), "M11_AUTH_URL"),
    redirectUri: `${origin}/`,
    example,
    logzioUrl: null,
  };
}

/** Replace each anchor, asserting it occurs exactly once; throws BuildError otherwise. */
export function applyPatches(files, patches) {
  const out = { ...files };
  for (const patch of patches) {
    const text = out[patch.file];
    if (typeof text !== "string") throw new BuildError(`patch target ${patch.file} is missing`);
    const count = text.split(patch.anchor).length - 1;
    if (count !== 1) throw new BuildError(`patch anchor for ${patch.file} occurs ${count} times (expected exactly 1)`);
    out[patch.file] = text.replace(patch.anchor, () => patch.replacement);
  }
  return out;
}

export function build({ root = REPO_ROOT, out = join(root, "module11", "vercel", "dist"), env = process.env } = {}) {
  const uiDir = join(root, "module10", "ui");
  const patches = JSON.parse(readFileSync(join(root, "module11", "vercel", "patches.json"), "utf8")).patches;
  const example = readFileSync(join(root, "module11", "vercel", "example.txt"), "utf8").trim();
  const config = configFrom(env, example);
  const names = readdirSync(uiDir).filter((name) => statSync(join(uiDir, name)).isFile()).sort();
  const texts = Object.fromEntries(names.filter((n) => /\.(mjs|js|html|css)$/.test(n))
    .map((n) => [n, readFileSync(join(uiDir, n), "utf8")]));
  const patched = applyPatches(texts, patches);
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  for (const name of names) {
    if (name in patched) writeFileSync(join(out, name), patched[name]);
    else copyFileSync(join(uiDir, name), join(out, name));
  }
  writeFileSync(join(out, "config.json"), `${JSON.stringify(config, null, 2)}\n`);
  return { out, files: [...names, "config.json"], config };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const index = process.argv.indexOf("--out");
  try {
    const result = build(index > 0 ? { out: resolve(process.argv[index + 1]) } : {});
    console.log(`m11 vercel build: ${result.files.length} files -> ${result.out}`);
  } catch (error) {
    console.error(`m11 vercel build failed: ${error.message}`);
    process.exit(1);
  }
}
