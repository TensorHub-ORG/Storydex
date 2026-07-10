import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const root = new URL("..", import.meta.url).pathname.replace(/^\/(?:[A-Za-z]:)/, (value) => value.slice(1));

test("icon font initialization cannot permanently hide glyphs", () => {
  const main = readFileSync(join(root, "src", "main.ts"), "utf8");
  const iconFont = readFileSync(join(root, "src", "utils", "iconFont.ts"), "utf8");
  const theme = readFileSync(join(root, "src", "assets", "theme.css"), "utf8");

  assert.match(main, /initializeIconFontState\(\)/);
  assert.match(iconFont, /fonts\.load\(ICON_FONT_SPEC/);
  assert.match(iconFont, /ICON_FONT_CLASS_FAILED/);
  assert.doesNotMatch(theme, /:root:not\(\.icon-font-ready\)[\s\S]{0,120}font-size:\s*0/);
  assert.match(theme, /:root\.icon-font-failed \.material-symbols-rounded/);
});

test("production build contains local Material Symbols font assets", () => {
  const assets = join(root, "dist", "assets");
  assert.equal(existsSync(assets), true, "run npm run build before this regression test");
  const files = readdirSync(assets);
  assert.equal(files.some((name) => /^material-symbols-rounded.*\.woff2$/.test(name)), true);
  assert.equal(files.some((name) => /^material-symbols-rounded.*\.woff$/.test(name)), true);
  const cssName = files.find((name) => name.endsWith(".css"));
  assert.ok(cssName);
  const css = readFileSync(join(assets, cssName), "utf8");
  assert.match(css, /Material Symbols Rounded/);
  assert.match(css, /material-symbols-rounded[^)]*\.woff2/);
});
