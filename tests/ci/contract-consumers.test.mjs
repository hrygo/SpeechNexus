import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { runValidation } from "../../scripts/dev-env/validate-contract-consumers.mjs";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

/**
 * Build a minimal repository where `packages/contracts` declares `exports` and
 * `web/app` is the consumer. `stale(...)` reasons replace the default empty
 * registry so each test states only the fact it is about.
 */
function fixture(
  t,
  {
    exports: exportsField = { ".": "./src/index.ts" },
    registry = { unbound: {} },
    files = { "src/index.ts": "export const root = 1;\n" },
    consumer = 'import { root } from "@speechnexus/contracts";\n',
    dependency = "workspace:*",
    consumerFile = "web/app/src/view.ts",
  } = {},
) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-contract-consumers-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const write = (name, value) => {
    const target = path.join(root, name);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, value);
  };
  write(
    "packages/contracts/package.json",
    JSON.stringify({ name: "@speechnexus/contracts", version: "0.1.0", exports: exportsField }),
  );
  for (const [name, content] of Object.entries(files)) write(`packages/contracts/${name}`, content);
  write("packages/contracts/export-consumers.json", JSON.stringify(registry));
  write(
    "web/app/package.json",
    JSON.stringify({
      name: "@speechnexus/app",
      dependencies:
        dependency === null ? {} : { "@speechnexus/contracts": dependency, react: "catalog:" },
    }),
  );
  if (consumer !== null) write(consumerFile, consumer);
  return { root, write, errors: () => runValidation(root) };
}

function messages(errors) {
  return errors.map((error) => error.message).join("\n");
}

test("valid fixture is accepted", (t) => {
  assert.deepEqual(fixture(t).errors(), []);
});

test("a declared export without a consumer fails", (t) => {
  const errors = fixture(t, {
    exports: {
      ".": "./src/index.ts",
      "./workbench": "./src/workbench.ts",
      "./execution": "./src/execution.ts",
    },
    files: {
      "src/index.ts": "export const root = 1;\n",
      "src/workbench.ts": "export {};\n",
      "src/execution.ts": "export {};\n",
    },
  }).errors();
  assert.match(messages(errors), /Export '\.\/execution' has no consumer/);
});

test("a registered unbound export needs no consumer", (t) => {
  const errors = fixture(t, {
    exports: { ".": "./src/index.ts", "./workbench": "./src/workbench.ts" },
    files: {
      "src/index.ts": "export const root = 1;\n",
      "src/workbench.ts": "export {};\n",
    },
    registry: { unbound: { "./workbench": "Python consumer only; no Web surface yet." } },
  }).errors();
  assert.deepEqual(errors, []);
});

test("a stale registration fails once the export gains a consumer", (t) => {
  const errors = fixture(t, {
    exports: { ".": "./src/index.ts", "./workbench": "./src/workbench.ts" },
    files: {
      "src/index.ts": "export const root = 1;\n",
      "src/workbench.ts": "export {};\n",
    },
    registry: { unbound: { "./workbench": "Python consumer only; no Web surface yet." } },
    consumer: 'import "@speechnexus/contracts/workbench";\n',
  }).errors();
  assert.match(messages(errors), /registered as unbound but is consumed/);
});

test("a registered export that is not declared fails", (t) => {
  const errors = fixture(t, { registry: { unbound: { "./ghost": "not declared" } } }).errors();
  assert.match(messages(errors), /'\.\/ghost' is not declared/);
});

test("a registration without a reason fails", (t) => {
  const errors = fixture(t, {
    exports: { ".": "./src/index.ts", "./workbench": "./src/workbench.ts" },
    files: {
      "src/index.ts": "export const root = 1;\n",
      "src/workbench.ts": "export {};\n",
    },
    registry: { unbound: { "./workbench": "" } },
  }).errors();
  assert.match(messages(errors), /needs a non-empty reason/);
});

test("an export whose generated artifact is missing fails", (t) => {
  const errors = fixture(t, {
    exports: { ".": "./src/index.ts", "./moved": "./src/moved.ts" },
  }).errors();
  assert.match(messages(errors), /Export '\.\/moved' targets a missing generated artifact/);
});

test("an import of an undeclared exports subpath fails", (t) => {
  const errors = fixture(t, {
    consumer:
      'import { root } from "@speechnexus/contracts";\nimport "@speechnexus/contracts/runtime";\n',
  }).errors();
  assert.match(messages(errors), /is not a declared exports subpath/);
});

test("an import without the workspace dependency fails", (t) => {
  const errors = fixture(t, { dependency: null }).errors();
  assert.match(messages(errors), /without declaring "@speechnexus\/contracts": "workspace:\*"/);
});

test("a non-workspace dependency range fails", (t) => {
  const errors = fixture(t, { dependency: "^0.1.0" }).errors();
  assert.match(messages(errors), /requires the workspace protocol/);
});

test("web may not reach the package sources by relative path", (t) => {
  const errors = fixture(t, {
    consumer:
      'import { root } from "../../../packages/contracts/src/index";\nimport "@speechnexus/contracts";\n',
  }).errors();
  assert.match(messages(errors), /imports the generated package by relative path/);
});

test("commented-out specifiers are not consumers", (t) => {
  const errors = fixture(t, {
    exports: { ".": "./src/index.ts", "./workbench": "./src/workbench.ts" },
    files: {
      "src/index.ts": "export const root = 1;\n",
      "src/workbench.ts": "export {};\n",
    },
    registry: { unbound: { "./workbench": "Python consumer only; no Web surface yet." } },
    consumer:
      'import { root } from "@speechnexus/contracts";\n// import "@speechnexus/contracts/workbench";\n',
  }).errors();
  assert.deepEqual(errors, []);
});

test("repository-internal scripts stay out of the Web binding scope", (t) => {
  // The root package runs repository scripts against a checkout; it is not a
  // workspace consumer of the generated package, so relative paths there are out
  // of scope. Only `web/` is required to bind through the package manifest.
  const { root } = fixture(t);
  const script = path.join(root, "scripts/dev-env/probe.mjs");
  fs.mkdirSync(path.dirname(script), { recursive: true });
  fs.writeFileSync(script, 'import { root } from "../../packages/contracts/src/index";\n');
  assert.deepEqual(runValidation(root), []);
});

test("the checked-out repository satisfies the binding rules", () => {
  assert.deepEqual(runValidation(repoRoot), []);
});
