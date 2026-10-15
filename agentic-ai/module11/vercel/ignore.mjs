// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Vercel "Ignored Build Step". Vercel skips the build on exit 0 and
// builds on exit 1. Skip while M11_API_URL is unset (the MR is not wired yet) and when the
// commit author is the bot (M11_BOT_LOGIN, a non-secret project variable), so a bot-authored
// commit never builds even if a git deployment starts despite vercel.json's patterns.
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

export function decide(env) {
  if (!(env.M11_API_URL || "").trim()) return { exit: 0, reason: "M11_API_URL is not set for this branch" };
  const author = (env.VERCEL_GIT_COMMIT_AUTHOR_LOGIN || "").trim().toLowerCase();
  const bot = (env.M11_BOT_LOGIN || "").trim().toLowerCase();
  if (bot && author === bot) return { exit: 0, reason: "commit authored by the bot" };
  return { exit: 1, reason: "build" };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = decide(process.env);
  console.log(`m11 ignore step: ${result.exit === 0 ? "skip" : "build"} (${result.reason})`);
  process.exit(result.exit);
}
