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
  assert.equal(inventory.counts.pythonRoutes, 99);
  assert.equal(inventory.counts.frontendConsumers, 96);
  assert.equal(inventory.counts.rustRoutes, 104);
  assert.equal(inventory.counts.frontendConsumersWithoutPythonRoute, 23);
  assert.equal(inventory.counts.frontendConsumersWithPythonRoute, 73);
  assert.equal(
    inventory.pythonRoutes.some((route) => route.path === "/agent/chat/stream" && route.method === "POST"),
    false,
    "the removed Python Agent stream route must not reappear in the inventory"
  );
  assert.ok(inventory.frontendConsumers.some((item) => item.path === "/workspace/git/summary"));
  assert.ok(inventory.rustRoutes.some((item) => item.path === "/workspace/git/summary" && item.method === "GET"));
  assert.ok(inventory.frontendConsumers.some((item) => item.path === "/agent/chat" && !item.pythonRoutePresent));
  assert.equal(inventory.counts.frontendConsumersImplementedInRust, inventory.counts.frontendConsumers);
  assert.equal(inventory.counts.frontendConsumersPendingInRust, 0);
  assert.equal(inventory.counts.contractsImplemented, inventory.counts.rustRoutes);
  assert.equal(inventory.counts.contractsPending, 0);
  assert.equal(inventory.counts.contractsExcluded, 22);
  assert.equal(inventory.counts.rustRoutesWithoutPythonRoute, 27);
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
  assert.equal(inventory.stableBoundary.legacyRuntime.includes("not launched"), true);
  assert.equal(targetFor("/story/wiki/graph"), "coomi-services::storydex_project + wiki projection");
  assert.equal(targetFor("/agent/chat/stream"), "storydex-agentd");
  assert.equal(targetFor("/workspace/git/commit"), "coomi-services::storydex_project");
});
