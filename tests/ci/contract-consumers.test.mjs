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

const CONSUMER_REGISTRY = {
  unbound: {
    "./workbench-http": {
      reason: "Only the Python backend HTTP surface consumes this profile.",
      consumer: {
        path: "backend/app.py",
        module: "speechnexus_contracts.workbench_http",
      },
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
  write(root, "packages/contracts/export-consumers.json", CONSUMER_REGISTRY);
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

test("the checked-out repository satisfies the binding rules", () => {
  const repositoryRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../..");
  assert.deepEqual(runValidation(repositoryRoot), []);
});

test("a clean fixture passes", () => {
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

test("a relative import hidden behind a symlinked directory fails", () => {
  const overrides = {
    "web/app/src/legacy.ts": 'import { validate } from "./contracts-src/workbench-http";\n',
  };
  withFixture(overrides, (root) => {
    fs.symlinkSync(
      path.resolve(root, "packages/contracts/src"),
      path.resolve(root, "web/app/src/contracts-src"),
      "dir",
    );
    assert.ok(has(runValidation(root), "imports the generated package by relative path"));
  });
});

test("import-like text in comments, strings and templates is not a consumer", () => {
  const overrides = {
    "web/app/src/notes.ts": [
      '// import { parse } from "@speechnexus/contracts/ghost";',
      "const example = \"import { parse } from '@speechnexus/contracts/ghost'\";",
      'const template = `import { parse } from "@speechnexus/contracts/ghost"`;',
      "const prose = `see the import in \\`@speechnexus/contracts/ghost\\` docs`;",
      "export const keep = [example, template, prose];",
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

test("a template-literal dynamic import under web fails", () => {
  withFixture(
    {
      "web/app/src/lazy.ts":
        "export const load = (name) => import(`@speechnexus/contracts/${name}`);\n",
    },
    (root) => {
      assert.ok(has(runValidation(root), "template-literal specifier"));
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
      assert.ok(has(errors, 'without declaring "@speechnexus/contracts": "workspace:*"'));
      assert.equal(has(errors, "has no consumer"), false);
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

test("a missing workspace dependency fails", () => {
  withFixture({ "web/app/package.json": { name: "@speechnexus/web-app" } }, (root) => {
    assert.ok(has(runValidation(root), 'is imported without declaring "@speechnexus/contracts"'));
  });
});

test("a non-workspace dependency version fails", () => {
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

test("a new export without a consumer fails", () => {
  const exports = { ...MANIFEST.exports, "./ghost": "./src/index.ts" };
  withFixture({ "packages/contracts/package.json": { ...MANIFEST, exports } }, (root) => {
    assert.ok(has(runValidation(root), "Export './ghost' has no consumer."));
  });
});

test("a registered consumer path that does not exist fails", () => {
  const registry = {
    unbound: {
      "./workbench-http": {
        reason: "backend only",
        consumer: {
          path: "backend/does-not-exist.py",
          module: "speechnexus_contracts.workbench_http",
        },
      },
    },
  };
  withFixture({ "packages/contracts/export-consumers.json": registry }, (root) => {
    assert.ok(has(runValidation(root), "consumer path 'backend/does-not-exist.py' does not exist"));
  });
});

test("a registered consumer path that does not import the derived module fails", () => {
  const registry = {
    unbound: {
      "./workbench-http": {
        reason: "backend only",
        consumer: { path: "backend/app.py", module: "speechnexus_contracts.workbench_http" },
      },
    },
  };
  withFixture(
    { "packages/contracts/export-consumers.json": registry, "backend/app.py": "x = 1\n" },
    (root) => {
      assert.ok(has(runValidation(root), "does not import speechnexus_contracts.workbench_http"));
    },
  );
});

test("a registration without any consumer evidence fails", () => {
  const registry = {
    unbound: { "./workbench-http": { reason: "TOTALLY BOGUS: no consumer anywhere" } },
  };
  withFixture({ "packages/contracts/export-consumers.json": registry }, (root) => {
    assert.ok(has(runValidation(root), 'must declare a consumer object or "consumer": null'));
  });
});

test("a registered consumer module that differs from the derived module fails", () => {
  const registry = {
    unbound: {
      "./workbench-http": {
        reason: "backend only",
        consumer: { path: "backend/app.py", module: "speechnexus_contracts.execution" },
      },
    },
  };
  withFixture({ "packages/contracts/export-consumers.json": registry }, (root) => {
    assert.ok(
      has(runValidation(root), "must equal the derived 'speechnexus_contracts.workbench_http'"),
    );
  });
});

test("a stale registration for a consumed export fails", () => {
  withFixture(
    {
      "web/app/src/extra.ts": 'import { validate } from "@speechnexus/contracts/workbench-http";\n',
    },
    (root) => {
      assert.ok(has(runValidation(root), "is registered as unbound but is consumed by"));
    },
  );
});

test("a retained candidateneeds a decision document and enable/delete conditions", () => {
  const registry = {
    unbound: { "./workbench-http": { reason: "candidate", consumer: null, retention: {} } },
  };
  withFixture({ "packages/contracts/export-consumers.json": registry }, (root) => {
    const errors = runValidation(root);
    assert.ok(has(errors, "retention needs a non-empty 'decision' condition"));
    assert.ok(has(errors, "retention needs a non-empty 'enable' condition"));
    assert.ok(has(errors, "retention needs a non-empty 'delete' condition"));
    assert.ok(has(errors, "retention needs a decision document in 'basis'"));
    assert.ok(has(errors, "retention needs the bindings.json profile in 'binding'"));
  });
});

test("a retained candidate passes when the binding profile generates no client", () => {
  const registry = {
    unbound: {
      "./workbench-http": {
        reason: "candidate with no consumer on either side",
        consumer: null,
        retention: {
          binding: "workbench_candidate",
          basis: "docs/candidate.md",
          decision: "Candidate handed over for later wiring.",
          enable: "After the implementing owner ships the port.",
          delete: "Only together with the bindings.json profile.",
        },
      },
    },
  };
  const overrides = {
    "packages/contracts/export-consumers.json": registry,
    "docs/candidate.md": "# Candidate\n",
    "contracts/bindings.json": { profiles: { workbench_candidate: { client: false } } },
    "backend/app.py": "x = 1\n",
  };
  withFixture(overrides, (root) => {
    assert.deepEqual(runValidation(root), []);
  });
});

test("a retained candidate whose binding generates a client fails", () => {
  const registry = {
    unbound: {
      "./workbench-http": {
        reason: "candidate",
        consumer: null,
        retention: {
          binding: "workbench_candidate",
          basis: "docs/candidate.md",
          decision: "Candidate.",
          enable: "Later.",
          delete: "Never.",
        },
      },
    },
  };
  const overrides = {
    "packages/contracts/export-consumers.json": registry,
    "docs/candidate.md": "# Candidate\n",
    "contracts/bindings.json": { profiles: { workbench_candidate: { client: true } } },
  };
  withFixture(overrides, (root) => {
    assert.ok(has(runValidation(root), "generates a client"));
  });
});

test("a retained candidate that gains a Python consumer fails", () => {
  const registry = {
    unbound: {
      "./workbench-http": {
        reason: "candidate",
        consumer: null,
        retention: {
          binding: "workbench_candidate",
          basis: "docs/candidate.md",
          decision: "Candidate.",
          enable: "Later.",
          delete: "Never.",
        },
      },
    },
  };
  const overrides = {
    "packages/contracts/export-consumers.json": registry,
    "docs/candidate.md": "# Candidate\n",
    "contracts/bindings.json": { profiles: { workbench_candidate: { client: false } } },
    "backend/app.py": "x = 1\n",
  };
  withFixture(overrides, (root) => {
    write(root, "backend/later.py", "from speechnexus_contracts.workbench_http import validate\n");
    assert.ok(
      has(runValidation(root), "is registered as consumerless but backend/later.py imports"),
    );
  });
});

test("a stale registration key fails", () => {
  const registry = {
    unbound: {
      "./workbench-http": CONSUMER_REGISTRY.unbound["./workbench-http"],
      "./ghost": { reason: "ghost", consumer: { path: "backend/app.py" } },
    },
  };
  withFixture({ "packages/contracts/export-consumers.json": registry }, (root) => {
    assert.ok(has(runValidation(root), "Registered export './ghost' is not declared"));
  });
});

test("helper mappings stay in sync with the contract package", () => {
  assert.equal(subpathKey("@speechnexus/contracts"), ".");
  assert.equal(subpathKey("@speechnexus/contracts/workbench-http"), "./workbench-http");
  assert.equal(subpathKey("react"), null);
  assert.equal(pythonModuleFor("./workbench-http"), "speechnexus_contracts.workbench_http");
  assert.equal(pythonModuleFor("./schemas/workbench"), null);
  assert.equal(exportTarget("./src/index.ts"), "./src/index.ts");
});
