"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  buildInventory,
  normalizePath,
  routeFromExpression,
  targetFor
} = require("../generate_rust_backend_interface_inventory.cjs");

test("inventory discovers the current public routes and frontend consumers", () => {
  const inventory = buildInventory();
  assert.equal(inventory.schemaVersion, 2);
  assert.ok(inventory.counts.pythonRoutes >= 100);
  assert.ok(inventory.counts.frontendConsumers >= 80);
  assert.ok(inventory.counts.rustRoutes >= 25);
  assert.equal(inventory.counts.frontendConsumersWithoutPythonRoute, 0);
  assert.ok(inventory.pythonRoutes.some((route) => route.path === "/agent/chat/stream" && route.method === "POST"));
  assert.ok(inventory.frontendConsumers.some((item) => item.path === "/workspace/git/summary"));
  assert.ok(inventory.rustRoutes.some((item) => item.path === "/workspace/git/summary" && item.method === "GET"));
  assert.ok(inventory.counts.frontendConsumersPendingInRust > 0);
});

test("inventory discovers template routes and normalizes parameterized paths", () => {
  const inventory = buildInventory();
  assert.equal(routeFromExpression("`/agent/runs/${encodeURIComponent(traceId)}/diff`"), "/agent/runs/{param}/diff");
  assert.equal(normalizePath("/presets/{name:path}/document"), "/presets/{param}/document");
  assert.ok(inventory.frontendConsumers.some((item) => item.path === "/auth/check-username/{param}"));
  assert.ok(inventory.frontendConsumers.some((item) => item.path === "/agent/runs/{param}/commit"));
  assert.ok(inventory.frontendConsumers.some((item) => item.path === "/presets/{param}/document"));
});

test("inventory keeps migration status and exclusion evidence explicit", () => {
  const inventory = buildInventory();
  const implemented = inventory.contracts.find((item) => item.method === "GET" && item.normalizedPath === "/workspace/git/summary");
  const implementedAuth = inventory.contracts.find((item) => item.method === "POST" && item.normalizedPath === "/auth/login");
  const excluded = inventory.contracts.find((item) => item.method === "GET" && item.normalizedPath === "/sys/workspace-state");
  assert.equal(implemented?.status, "implemented");
  assert.equal(implementedAuth?.status, "implemented");
  assert.equal(excluded?.status, "excluded");
  assert.match(excluded?.exclusionEvidence || "", /No Vue API consumer/);
  assert.equal(targetFor("/story/wiki/graph"), "coomi-services::storydex_project + wiki projection");
  assert.equal(targetFor("/agent/chat/stream"), "storydex-agentd");
  assert.equal(targetFor("/workspace/git/commit"), "coomi-services::storydex_project");
});
