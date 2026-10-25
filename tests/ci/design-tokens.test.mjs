import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { createHash } from "node:crypto";
import { runValidation } from "../../scripts/dev-env/validate-design-tokens.mjs";

function fixture(t, { css = "", source = "", file = "web/admin/src/view.tsx", registry } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-tokens-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const write = (name, value) => {
    fs.mkdirSync(path.dirname(path.join(root, name)), { recursive: true });
    fs.writeFileSync(path.join(root, name), value);
  };
  write(
    "web/shared/src/ui/styles.css",
    `:root { --nexus-color-test-500: red; --ink: var(--nexus-color-test-500); }\n${css}`,
  );
  write(
    "web/shared/src/tokens/tokens.json",
    registry ??
      JSON.stringify({
        color: { primitive: { test: { 500: { $type: "color", $value: "red" } } } },
        ink: { $type: "color", $value: "var(--ink)" },
      }),
  );
  write("web/shared/src/tokens/index.ts", 'export const ink = "var(--ink)";');
  write(file, source);
  return { root, write, errors: () => runValidation(root) };
}

test("valid fixture and root-relative palette exemption", (t) => {
  assert.deepEqual(fixture(t).errors(), []);
});

for (const reference of [
  "var(--missing)",
  "var( --missing )",
  "var(--missing, var(--ink))",
  "var(\n--missing\n)",
]) {
  test(`reject undefined consumer reference: ${JSON.stringify(reference)}`, (t) => {
    const f = fixture(t, { source: `const x = '${reference}';` });
    assert.match(JSON.stringify(f.errors()), /--missing/);
  });
}

test("comments cannot define a referenced variable", (t) => {
  const f = fixture(t, { css: "/* --phantom: red; */", source: 'const x = "var(--phantom)";' });
  assert.match(JSON.stringify(f.errors()), /--phantom/);
});

test("check CSS alias dependencies, including fallback dependencies", (t) => {
  const f = fixture(t, { css: ":root { --other: var(--ink, var(--typo)); }" });
  assert.match(JSON.stringify(f.errors()), /--typo/);
});

for (const file of [
  "web/admin/src/view.css",
  "web/admin/index.html",
  "web/workbench/public/icon.svg",
  "web/shared/src/view.tsx",
]) {
  test(`reject raw color in ${file}`, (t) => {
    const source = file.endsWith("css") ? ".x { color: #abcdef; }" : '<svg fill="#abcdef" />';
    assert.match(JSON.stringify(fixture(t, { file, source }).errors()), /Hardcoded color/);
  });
}

for (const color of [
  "lab(50% 10 20)",
  "lch(50% 10 20)",
  "oklab(.5 0 0)",
  "color(display-p3 1 0 0)",
  "red",
]) {
  test(`reject color syntax ${color}`, (t) => {
    assert.match(
      JSON.stringify(fixture(t, { source: `<svg fill="${color}" />` }).errors()),
      /Hardcoded color/,
    );
  });
}

test("fragment href cannot exempt another attribute on the same line", (t) => {
  assert.match(
    JSON.stringify(fixture(t, { source: '<use href="#abc" fill="#fff" />' }).errors()),
    /Hardcoded color/,
  );
});

test("fragment IDs and token based values are allowed", (t) => {
  assert.deepEqual(
    fixture(t, { source: '<use href="#abc" fill="url(#abc)" stroke="var( --ink )" />' }).errors(),
    [],
  );
});

test("block comments do not cause phantom color/reference errors", (t) => {
  assert.deepEqual(
    fixture(t, { source: '/*\n #fff var(--missing)\n */\nconst x = "var(--ink)";' }).errors(),
    [],
  );
});

test("tests are excluded, including .spec files", (t) => {
  assert.deepEqual(
    fixture(t, {
      file: "web/admin/src/view.spec.tsx",
      source: 'const x = "#fff var(--missing)";',
    }).errors(),
    [],
  );
});

test("invalid JSON fails with an actionable diagnostic", (t) => {
  assert.match(JSON.stringify(fixture(t, { registry: "{ broken" }).errors()), /registry|JSON/i);
});

test("primitive exports require registration, without a prefix escape hatch", (t) => {
  const f = fixture(t, { css: ":root { --nexus-color-unregistered: #fff; }" });
  f.write("web/shared/src/tokens/index.ts", 'const x = "var(--nexus-color-unregistered)";');
  assert.match(JSON.stringify(f.errors()), /missing from|registered/i);
});

test("primitive registry and CSS values cannot drift", (t) => {
  const f = fixture(t);
  f.write(
    "web/shared/src/ui/styles.css",
    ":root { --nexus-color-test-500: #654321; --ink: var(--nexus-color-test-500); }",
  );
  assert.match(JSON.stringify(f.errors()), /drift|mismatch/i);
});

test("metadata cannot register an exported CSS reference", (t) => {
  const f = fixture(t, { registry: '{"description":"var(--ink)"}' });
  assert.match(JSON.stringify(f.errors()), /registry|registered/i);
});

test("unsupported DTCG alias must fail explicitly", (t) => {
  const f = fixture(t, { registry: '{"a":{"$type":"color","$value":"{color.primary}"}}' });
  assert.match(JSON.stringify(f.errors()), /unsupported|alias/i);
});

test("exceptions pin exact path and content; mutation fails closed", (t) => {
  const file = "web/admin/public/brand.svg";
  const source = '<svg fill="#abcdef" />';
  const f = fixture(t, { file, source });
  f.write(
    "web/shared/design-token-exceptions.json",
    JSON.stringify({
      [file]: {
        reason: "Standalone approved artwork",
        sha256: createHash("sha256").update(source).digest("hex"),
      },
    }),
  );
  assert.deepEqual(f.errors(), []);
  f.write(file, source.replace("abcdef", "fedcba"));
  assert.match(JSON.stringify(f.errors()), /content changed/);
});

test("exception cannot hide undefined references or external assets", (t) => {
  const file = "web/admin/public/brand.svg";
  const source = '<svg fill="var(--missing)"><image href="https://example.com/a.svg" /></svg>';
  const f = fixture(t, { file, source });
  f.write(
    "web/shared/design-token-exceptions.json",
    JSON.stringify({
      [file]: {
        reason: "Standalone artwork",
        sha256: createHash("sha256").update(source).digest("hex"),
      },
    }),
  );
  const errors = JSON.stringify(f.errors());
  assert.match(errors, /--missing/);
  assert.match(errors, /External URL/);
});

test("CSS content strings cannot declare variables", (t) => {
  const f = fixture(t, {
    css: '.x { content: "--phantom: red;"; }',
    source: 'const x = "var(--phantom)";',
  });
  assert.match(JSON.stringify(f.errors()), /Undefined CSS variable '--phantom'/);
});

test("same-rule CSS alias cycles are rejected, including fallback cycles", (t) => {
  const f = fixture(t, { css: ":root { --a: var(--b); --b: var(--ink, var(--a)); }" });
  assert.match(JSON.stringify(f.errors()), /cycle/i);
});

test("quoted style object property cannot hide a named color", (t) => {
  assert.match(
    JSON.stringify(fixture(t, { source: 'const s = {"color": "rebeccapurple"};' }).errors()),
    /Hardcoded color/,
  );
});

test("palette allows primitives but not raw colors in component declarations", (t) => {
  const f = fixture(t, { css: ".x { color: #abcdef; }" });
  assert.match(JSON.stringify(f.errors()), /Hardcoded color/);
});

for (const token of [
  { $type: "color" },
  { $type: "typo", $value: "var(--ink)" },
  { $type: "color", $value: "var(--ink" },
]) {
  test(`reject malformed registry leaf ${JSON.stringify(token)}`, (t) => {
    const f = fixture(t);
    f.write(
      "web/shared/src/tokens/tokens.json",
      JSON.stringify({ valid: { $type: "color", $value: "var(--ink)" }, broken: token }),
    );
    assert.match(JSON.stringify(f.errors()), /Invalid registry|Unsupported registry/);
  });
}

for (const source of [
  '<svg fill={"red"} />',
  'const s = { borderLeftColor: "red" };',
  'const s = {color: "var(--ink, red)"};',
]) {
  test(`reject named color in static expression ${source}`, (t) => {
    assert.match(JSON.stringify(fixture(t, { source }).errors()), /Hardcoded color/);
  });
}

test("CSS strings cannot create rule blocks or color declarations", (t) => {
  const f = fixture(t, { css: '.x { content: "{ --ink: var(--ink); } color: red"; }' });
  assert.deepEqual(f.errors(), []);
});

test("SVG floodColor is a color-bearing property", (t) => {
  assert.match(
    JSON.stringify(fixture(t, { source: '<feFlood floodColor="red" />' }).errors()),
    /Hardcoded color/,
  );
});
