import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const configPanel = readFileSync(new URL("../src/components/CoomiConfigPanel.vue", import.meta.url), "utf8");
const agentApi = readFileSync(new URL("../src/api/agent.ts", import.meta.url), "utf8");
const agentTypes = readFileSync(new URL("../src/types/agent.ts", import.meta.url), "utf8");
const routesAgent = readFileSync(new URL("../../backend/api/routes_agent.py", import.meta.url), "utf8");
const presetSidebar = readFileSync(new URL("../src/components/PresetManagementSidebar.vue", import.meta.url), "utf8");

assert.match(agentTypes, /AgentCoomiModelListRequest/u, "Agent model-list request type should exist.");
assert.match(agentTypes, /AgentCoomiModelListResponse/u, "Agent model-list response type should exist.");
assert.match(agentApi, /fetchAgentCoomiModels/u, "Frontend API wrapper should expose fetchAgentCoomiModels.");
assert.match(agentApi, /\/agent\/coomi\/models/u, "Frontend API wrapper should call /agent/coomi/models.");
assert.match(routesAgent, /@router\.post\("\/agent\/coomi\/models"/u, "Backend should expose /agent/coomi/models.");

assert.match(configPanel, /fetchAgentCoomiModels/u, "LLM config panel should import and call fetchAgentCoomiModels.");
assert.match(configPanel, /获取模型/u, "LLM config panel should render a 获取模型 button.");
assert.match(configPanel, /<select\s+v-model="form\.model"/u, "Standard model field should be a dropdown.");
assert.match(configPanel, /<select\s+v-model="form\.fastModel"/u, "Fast model field should be a dropdown.");
assert.match(configPanel, /modelOptions/u, "LLM config panel should keep fetched model options.");

assert.match(
  presetSidebar,
  /@contextmenu\.prevent\.stop="openPresetContextMenu\(\$event, item\)"/u,
  "Preset rows should open a context menu on right click."
);
assert.match(presetSidebar, /preset-context-menu/u, "Preset sidebar should render a context menu.");
assert.match(presetSidebar, /handleDeletePreset/u, "Preset context menu should offer delete.");
assert.match(presetSidebar, /workspaceStore\.deletePath/u, "Preset delete should use workspaceStore.deletePath.");
assert.match(presetSidebar, /sidecarPathFor/u, "Deleting markdown presets should account for their sidecar JSON.");
assert.match(presetSidebar, /复制相对路径/u, "Preset context menu should include copying the relative path.");
