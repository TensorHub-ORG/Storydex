"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { verifyBuildInfo } = require("../scripts/verify-runtime-identity.cjs");

test("packaging accepts the verified runtime and rejects stale or incompatible binaries", () => {
  const expected = { runtime: "storydex-coomi-rs", version: "test", protocolVersion: 1,
    gitSha: "a".repeat(40), sourceFingerprint: "b".repeat(40) };
  assert.doesNotThrow(() => verifyBuildInfo({ ...expected }, expected));
  for (const key of Object.keys(expected)) {
    assert.throws(() => verifyBuildInfo({ ...expected, [key]: "stale" }, expected), /does not match/);
    const missing = { ...expected };
    delete missing[key];
    assert.throws(() => verifyBuildInfo(missing, expected), /does not match/);
  }
});
