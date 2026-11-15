import assert from "node:assert/strict";
import fs from "node:fs";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  exportTarget,
  pythonModuleFor,
  runValidation,
  scanSource,
  subpathKey,
} from "../../scripts/dev-env/validate-contract-consumers.mjs";

const MANIFEST = {
  name: "@speechnexus/contracts",
  private: true,
  exports: {
    ".": "./src/index.ts",
    "./workbench": "./src/workbench.ts",
    "./workbench-http": "./src/workbench-http.ts",
  },
};

const CANDIDATE_BINDINGS = {
  profiles: {
    workbench_http: {
      client: false,
      source: "internal/workbench-candidate.schema.json",
    },
  },
};

function write(root, relative, content) {
  const file = path.join(root, relative);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, typeof content === "string" ? content : JSON.stringify(content, null, 2));
  return file;
}

function makeFixture(overrides = {}) {
  const root = mkdtempSync(path.join(tmpdir(), "contract-consumers-"));
  write(root, "packages/contracts/package.json", MANIFEST);
  write(root, "packages/contracts/src/index.ts", "export const parse = () => {};\n");
  write(root, "packages/contracts/src/workbench.ts", "export const parse = () => {};\n");
  write(root, "packages/contracts/src/workbench-http.ts", "export const validate = () => {};\n");
  write(root, "packages/contracts/src/speechnexus_contracts/workbench_http.py", "validate = 1\n");
  write(root, "web/app/package.json", {
    name: "@speechnexus/web-app",
    dependencies: { "@speechnexus/contracts": "workspace:*" },
  });
  write(
    root,
    "web/app/src/main.ts",
    'import { parse } from "@speechnexus/contracts";\nimport { parse as workbench } from "@speechnexus/contracts/workbench";\n',
  );
  write(root, "backend/app.py", "from speechnexus_contracts.workbench_http import validate\n");
  for (const [relative, content] of Object.entries(overrides)) write(root, relative, content);
  return root;
}

function withFixture(overrides, body) {
  const root = makeFixture(overrides);
  try {
    return body(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function messages(errors) {
  return errors.map((error) => error.message);
}

function has(errors, fragment) {
  return messages(errors).some((message) => message.includes(fragment));
}

/** The third export has no consumer unless its generated module is really imported. */
function noPythonConsumer(overrides = {}) {
  return { "backend/app.py": "x = 1\n", ...overrides };
}

test("the checked-out repository satisfies the binding rules", () => {
  const repositoryRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../..");
  assert.deepEqual(runValidation(repositoryRoot), []);
});

test("a clean fixture passes: manifest consumers plus a real Python import", () => {
  withFixture({}, (root) => {
    assert.deepEqual(runValidation(root), []);
  });
});

test("a missing export target fails", () => {
  withFixture(
    {
      "packages/contracts/package.json": {
        ...MANIFEST,
        exports: { ...MANIFEST.exports, "./workbench": "./src/moved.ts" },
      },
    },
    (root) => {
      assert.ok(has(runValidation(root), "targets a missing artifact './src/moved.ts'"));
    },
  );
});

test("a nested condition object without a string target gets a readable error", () => {
  const exports = {
    ...MANIFEST.exports,
    "./workbench-http": { node: { import: "./src/workbench-http.ts" } },
  };
  withFixture({ "packages/contracts/package.json": { ...MANIFEST, exports } }, (root) => {
    assert.ok(has(runValidation(root), "no string import/types target"));
  });
  assert.equal(exportTarget({ node: { import: "./x.ts" } }), null);
  assert.equal(exportTarget({ import: "./x.ts" }), "./x.ts");
});

test("a relative import into packages/contracts/src from web fails", () => {
  withFixture(
    {
      "web/app/src/legacy.ts":
        'import { parse } from "../../../packages/contracts/src/workbench";\n',
    },
    (root) => {
      assert.ok(has(runValidation(root), "imports the generated package by relative path"));
    },
  );
});

test("a symlinked directory reports one error, not one per generated file", () => {
  const overrides = {
    "web/app/src/legacy.ts": 'import { validate } from "./contracts-src/workbench-http";\n',
  };
  withFixture(overrides, (root) => {
    fs.symlinkSync(
      path.resolve(root, "packages/contracts/src"),
      path.resolve(root, "web/app/src/contracts-src"),
      "dir",
    );
    const errors = runValidation(root);
    assert.equal(errors.length, 1);
    assert.ok(has(errors, "imports the generated package by relative path"));
  });
});

test("import-like text in comments, strings and templates is not a consumer", () => {
  const overrides = {
    "web/app/src/notes.ts": [
      '// import { parse } from "@speechnexus/contracts/ghost";',
      "const example = \"import { parse } from '@speechnexus/contracts/ghost'\";",
      'const template = `import { parse } from "@speechnexus/contracts/ghost"`;',
      "export const keep = [example, template];",
      "",
    ].join("\n"),
  };
  withFixture(overrides, (root) => {
    assert.deepEqual(runValidation(root), []);
  });
});

test("a regex literal containing quotes does not swallow the import after it", () => {
  const source =
    'const quoted = /["\']/;\nimport { parse } from "@speechnexus/contracts/workbench";\n';
  const scan = scanSource(source);
  assert.deepEqual(
    scan.specifiers.map(({ specifier, line }) => ({ specifier, line })),
    [{ specifier: "@speechnexus/contracts/workbench", line: 2 }],
  );
});

test("a multi-line import reports the specifier line, not the statement start", () => {
  const source = [
    "import {",
    "  parse,",
    "  validate,",
    '} from "@speechnexus/contracts/workbench";',
    "",
  ].join("\n");
  assert.deepEqual(
    scanSource(source).specifiers.map(({ specifier, line }) => ({ specifier, line })),
    [{ specifier: "@speechnexus/contracts/workbench", line: 4 }],
  );
});

test("a Unicode identifier does not hide the import", () => {
  const scan = scanSource('import { 解析器 } from "@speechnexus/contracts/workbench";\n');
  assert.deepEqual(
    scan.specifiers.map(({ specifier }) => specifier),
    ["@speechnexus/contracts/workbench"],
  );
});

test("JSX prose does not count as an import", () => {
  const source = 'export const copy = <p>transfer files from "server" to device</p>;\n';
  assert.deepEqual(scanSource(source).specifiers, []);
});

test("a non-literal dynamic import under web fails", () => {
  withFixture(
    { "web/app/src/lazy.ts": "export const load = (name) => import(name);\n" },
    (root) => {
      assert.ok(has(runValidation(root), "non-literal specifier"));
    },
  );
});

test("an interpolated template-literal dynamic import fails", () => {
  withFixture(
    {
      "web/app/src/lazy.ts":
        "export const load = (name) => import(`@speechnexus/contracts/${name}`);\n",
    },
    (root) => {
      assert.ok(has(runValidation(root), "interpolated template-literal specifier"));
    },
  );
});

test("an interpolation-free template-literal dynamic import is a consumer", () => {
  const exports = { ...MANIFEST.exports, "./lazy": "./src/lazy.ts" };
  withFixture(
    {
      "packages/contracts/package.json": { ...MANIFEST, exports },
      "packages/contracts/src/lazy.ts": "export const parse = () => {};\n",
      "web/app/src/lazy.ts": "export const load = () => import(`@speechnexus/contracts/lazy`);\n",
    },
    (root) => {
      assert.deepEqual(runValidation(root), []);
    },
  );
});

test("a literal dynamic import counts as a consumer and needs the workspace dependency", () => {
  withFixture(
    {
      "web/app/src/lazy.ts":
        'export const load = () => import("@speechnexus/contracts/workbench");\n',
      "web/app/package.json": { name: "@speechnexus/web-app" },
    },
    (root) => {
      const errors = runValidation(root);
      assert.ok(has(errors, 'is imported without declaring "@speechnexus/contracts"'));
    },
  );
});

test("a literal require counts as a consumer", () => {
  const scan = scanSource('const { parse } = require("@speechnexus/contracts/workbench");\n');
  assert.deepEqual(
    scan.specifiers.map(({ specifier }) => specifier),
    ["@speechnexus/contracts/workbench"],
  );
});

test("an undeclared subpath fails", () => {
  withFixture(
    { "web/app/src/ghost.ts": 'import { parse } from "@speechnexus/contracts/ghost";\n' },
    (root) => {
      assert.ok(has(runValidation(root), "is not a declared exports subpath"));
    },
  );
});

test("a missing or non-workspace dependency fails", () => {
  withFixture({ "web/app/package.json": { name: "@speechnexus/web-app" } }, (root) => {
    assert.ok(has(runValidation(root), 'is imported without declaring "@speechnexus/contracts"'));
  });
  withFixture(
    {
      "web/app/package.json": {
        name: "@speechnexus/web-app",
        dependencies: { "@speechnexus/contracts": "^1.0.0" },
      },
    },
    (root) => {
      assert.ok(has(runValidation(root), 'requires the workspace protocol ("workspace:*")'));
    },
  );
});

test("a new export with no consumer and no generated module fails", () => {
  const exports = { ...MANIFEST.exports, "./ghost": "./src/index.ts" };
  withFixture({ "packages/contracts/package.json": { ...MANIFEST, exports } }, (root) => {
    assert.ok(has(runValidation(root), "Export './ghost' has no consumer and no generated module"));
  });
});

test("an unconsumed export passes when a real Python file imports its module", () => {
  withFixture(noPythonConsumer(), (root) => {
    write(root, "backend/later.py", "from speechnexus_contracts.workbench_http import validate\n");
    assert.deepEqual(runValidation(root), []);
  });
});

test("a docstring or comment mentioning the module does not count as a consumer", () => {
  withFixture(
    noPythonConsumer({
      "backend/doc-only.py":
        '"""Example: from speechnexus_contracts.workbench_http import validate."""\n# import speechnexus_contracts.workbench_http\nx = 1\n',
    }),
    (root) => {
      const errors = runValidation(root);
      assert.ok(has(errors, "Export './workbench-http' has no consumer"));
    },
  );
});

test("an unconsumed export passes as a generated candidate with a client:false profile", () => {
  withFixture(
    noPythonConsumer({
      "contracts/bindings.json": CANDIDATE_BINDINGS,
      "contracts/internal/workbench-candidate.schema.json": "{}\n",
    }),
    (root) => {
      assert.deepEqual(runValidation(root), []);
    },
  );
});

test("a candidate whose profile generates a client fails", () => {
  withFixture(
    noPythonConsumer({
      "contracts/bindings.json": {
        profiles: {
          workbench_http: {
            client: true,
            source: "internal/workbench-candidate.schema.json",
          },
        },
      },
      "contracts/internal/workbench-candidate.schema.json": "{}\n",
    }),
    (root) => {
      assert.ok(has(runValidation(root), 'no matching "client": false profile'));
    },
  );
});

test("a candidate reusing an unrelated profile fails", () => {
  withFixture(
    noPythonConsumer({
      "contracts/bindings.json": {
        profiles: {
          realtime: { client: false, source: "realtime-events.schema.json" },
        },
      },
      "contracts/realtime-events.schema.json": "{}\n",
    }),
    (root) => {
      assert.ok(has(runValidation(root), 'no matching "client": false profile'));
    },
  );
});

test("copying the old retention block next to a ghost export changes nothing", () => {
  const retention = {
    retention: {
      binding: "zero_dispatch",
      basis: "docs/components/zero-dispatch-contract-core.md",
      decision: "DEL-417",
      enable: "later",
      delete: "never",
    },
  };
  const exports = { ...MANIFEST.exports, "./ghost": "./src/index.ts" };
  withFixture(
    {
      "packages/contracts/package.json": { ...MANIFEST, exports },
      "packages/contracts/export-consumers.json": { unbound: { "./ghost": retention } },
    },
    (root) => {
      assert.ok(
        has(runValidation(root), "Export './ghost' has no consumer and no generated module"),
      );
    },
  );
});

test("a non-Python subpath bundle has no candidate channel", () => {
  const exports = {
    ...MANIFEST.exports,
    "./schemas/workbench": "./src/speechnexus_contracts/workbench.schema.json",
  };
  withFixture(
    {
      "packages/contracts/package.json": { ...MANIFEST, exports },
      "packages/contracts/src/speechnexus_contracts/workbench.schema.json": "{}\n",
    },
    (root) => {
      assert.ok(has(runValidation(root), "Non-Python subpath bundles"));
    },
  );
});

test("a Python import of a missing generated module fails", () => {
  withFixture(
    noPythonConsumer({
      "backend/later.py": "from speechnexus_contracts.workbench_http import validate\n",
      "packages/contracts/src/speechnexus_contracts/workbench_http.py": "",
    }),
    (root) => {
      // Recreate without the module file: the fixture writer already wrote it,
      // so delete it to model a broken generation output.
      fs.rmSync(path.join(root, "packages/contracts/src/speechnexus_contracts/workbench_http.py"));
      assert.ok(has(runValidation(root), "generated module"));
    },
  );
});

test("helper mappings stay in sync with the contract package", () => {
  assert.equal(subpathKey("@speechnexus/contracts"), ".");
  assert.equal(subpathKey("@speechnexus/contracts/workbench-http"), "./workbench-http");
  assert.equal(subpathKey("react"), null);
  assert.equal(pythonModuleFor("./workbench-http"), "speechnexus_contracts.workbench_http");
  assert.equal(pythonModuleFor("./schemas/workbench"), null);
  assert.equal(exportTarget("./src/index.ts"), "./src/index.ts");
});
