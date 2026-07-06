import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const editor = readFileSync(new URL("../src/components/PresetEditor.vue", import.meta.url), "utf8");
const presetStore = readFileSync(new URL("../src/stores/preset.ts", import.meta.url), "utf8");

function sliceBetween(source, start, end) {
  const startIndex = source.indexOf(start);
  assert.notEqual(startIndex, -1, `Missing start marker: ${start}`);
  const endIndex = source.indexOf(end, startIndex + start.length);
  assert.notEqual(endIndex, -1, `Missing end marker: ${end}`);
  return source.slice(startIndex, endIndex);
}

const moduleToggleBlock = sliceBetween(
  editor,
  '<details v-if="presetModules.length" class="preset-editor-section" open>',
  '<details class="preset-editor-section">'
);

const moduleSmallLabels = moduleToggleBlock.match(/<small[\s\S]*?<\/small>/g) || [];
for (const label of moduleSmallLabels) {
  assert.equal(
    /module\.id|sourceFormat|sourceRole|st_/iu.test(label),
    false,
    "Structured module metadata must not expose ST/source/id labels."
  );
}

assert.match(
  moduleToggleBlock,
  /<details\s+v-for="\(module, index\) in presetModules"[\s\S]*class="preset-module-toggle-row"/u,
  "Each structured module row should be expandable."
);

assert.match(
  moduleToggleBlock,
  /:value="module\.content \|\| ''"[\s\S]*@input="onModuleContentChange\(index, \(\$event\.target as HTMLTextAreaElement\)\.value\)"/u,
  "Each structured module row should show and edit its own content."
);

assert.match(
  editor,
  /function onModuleContentChange\(index: number, content: string\): void/u,
  "Module content edits should update the preset document."
);

assert.equal(
  /"v1 虚拟模块"|"v2 模块"|section\.virtual \?[\s\S]*模块/u.test(editor),
  false,
  "Workbench module cards should not render source-version/type labels."
);

assert.match(
  presetStore,
  /await savePresetDocument\(this\.currentName, this\.document\);[\s\S]*const workspaceStore = useWorkspaceStore\(\);[\s\S]*if \(workspaceStore\.activeFileBindingOrPath === this\.currentName && !workspaceStore\.isDirty\) \{[\s\S]*await workspaceStore\.openFile\(this\.currentName, \{ forceReload: true \}\);[\s\S]*\}/u,
  "Saving a preset should refresh the same markdown file when it is open in the workspace editor."
);
