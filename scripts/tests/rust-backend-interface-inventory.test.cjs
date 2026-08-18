"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { buildInventory, normalizePath, targetFor } = require("../generate_rust_backend_interface_inventory.cjs");

test("inventory discovers the current public routes and frontend consumers", () => {
  const inventory = buildInventory();
  assert.equal(inventory.schemaVersion, 1);
  assert.ok(inventory.counts.pythonRoutes >= 100);
  assert.ok(inventory.counts.frontendConsumers >= 25);
  assert.equal(inventory.counts.frontendConsumersWithoutRoute, 0);
  assert.ok(inventory.routes.some((route) => route.path === "/agent/chat/stream" && route.method === "POST"));
  assert.ok(inventory.frontendConsumers.some((item) => item.path === "/workspace/git/summary"));
});

test("inventory normalizes parameterized paths and keeps target ownership explicit", () => {
  assert.equal(normalizePath("/presets/{name:path}/document"), "/presets/{param}/document");
  assert.equal(targetFor("/story/wiki/graph"), "coomi-services::storydex_project + wiki projection");
  assert.equal(targetFor("/agent/chat/stream"), "storydex-agentd");
  assert.equal(targetFor("/workspace/git/commit"), "coomi-services::storydex_project");
});
