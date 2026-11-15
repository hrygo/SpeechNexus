#!/usr/bin/env node
/**
 * Emit one eval-free validator module for one committed schema bundle.
 *
 * The runtime used to call `ajv.compile()` on first use. Compilation builds a
 * function from generated source text (`Function(...)`), which the deployment CSP
 * treats exactly like `eval`: `deploy/web/nginx.conf` sends `script-src 'self'`, so
 * every page that parsed a contract threw `EvalError` and rendered blank (#840).
 * Ajv's standalone mode emits the same compiled validation code as plain source at
 * generation time, so the shipped bundle contains no dynamic code construction.
 *
 * The emitted module is a checked artifact with the same authority as the Python
 * models and the TypeScript wrappers. It still imports Ajv's small runtime helpers
 * (`ajv/dist/runtime/{equal,ucs2length}.js`) and `ajv-formats/dist/formats.js`:
 * those are data/code helpers, not the compiler, and they are why `ajv` and
 * `ajv-formats` remain runtime dependencies of this package.
 *
 * Formats are part of the frozen contract. `addFormats` registers the full set, and
 * this script fails when a bundle uses a format that is not registered: under
 * `strict: false` Ajv only warns "unknown format ... ignored" and then accepts any
 * string in that slot, so an unregistered format would silently weaken the contract
 * with no schema diff, no drift failure and no failing test. `contracts/` currently
 * uses only `date-time` and `binary`, both in `ajv-formats`' full set; adding
 * `uuid`, `email` or anything else unregistered must fail here instead.
 *
 * Usage: node generate-standalone-validators.mjs <schema.json> <output.mjs>
 *
 * Every entrypoint in `x-nexus-entrypoints` becomes one validation function in a
 * single exported `validators` record. The bundle is registered once and every
 * entrypoint references it, so shared subschema code is emitted once instead of
 * once per entrypoint (about 5x smaller for the large HTTP bundles).
 */
import { readFileSync, writeFileSync } from "node:fs";
import { basename } from "node:path";

import { Ajv2020 } from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import standaloneCode from "ajv/dist/standalone/index.js";

const [schemaPath, outputPath] = process.argv.slice(2);
if (!schemaPath || !outputPath) {
  throw new Error("Usage: generate-standalone-validators.mjs <schema.json> <output.mjs>");
}

const IDENTIFIER = /^[A-Za-z_$][A-Za-z0-9_$]*$/;

const source = readFileSync(schemaPath, "utf8");
const bundle = JSON.parse(source);
const entrypoints = bundle["x-nexus-entrypoints"];
if (!Array.isArray(entrypoints) || entrypoints.length === 0) {
  throw new Error(`${schemaPath} has no x-nexus-entrypoints; refusing to emit an empty module`);
}

// Stable synthetic identifiers: the emitted bytes must not depend on the build
// directory, or `make contracts-drift` could never be byte-exact.
const bundleId = `https://speechnexus.invalid/contracts/${basename(schemaPath)}`;
const entryId = (name) =>
  `https://speechnexus.invalid/contracts/${basename(schemaPath)}/entry/${name}`;

// Ajv reports an unregistered `format` through its logger and then ignores it. Turn
// that into a failure: a warning here means the emitted validator silently accepts
// values the source schema meant to reject.
const compileWarnings = [];
const ajv = new Ajv2020({
  strict: false,
  coerceTypes: false,
  useDefaults: false,
  removeAdditional: false,
  validateFormats: true,
  // `source` is required by standalone mode; `esm` emits `export` statements.
  code: { source: true, esm: true },
  logger: {
    log: () => {},
    warn: (message) => compileWarnings.push(message),
    error: (message) => compileWarnings.push(message),
  },
});
addFormats(ajv);

/** Every `format` string in the bundle, with the JSON pointer that uses it. */
function collectFormats(node, pointer = "") {
  if (Array.isArray(node)) {
    return node.flatMap((value, index) => collectFormats(value, `${pointer}/${index}`));
  }
  if (!node || typeof node !== "object") return [];
  return Object.entries(node).flatMap(([key, value]) => {
    if (key === "format" && typeof value === "string") return [[value, pointer]];
    return collectFormats(value, `${pointer}/${key}`);
  });
}

const unregistered = collectFormats(bundle).filter(
  ([format]) => !Object.hasOwn(ajv.formats, format),
);
if (unregistered.length > 0) {
  const names = [...new Set(unregistered.map(([format]) => format))];
  throw new Error(
    `${schemaPath} uses JSON Schema format(s) ${names.map((name) => JSON.stringify(name)).join(", ")} ` +
      "that ajv-formats does not register. Ajv would ignore them and the emitted " +
      "validator would accept any string there, weakening the contract without any " +
      `schema diff or drift failure. Used at ${unregistered
        .map(([format, pointer]) => `${pointer || "/"} (${format})`)
        .join(", ")}. Registered: ${Object.keys(ajv.formats).join(", ")}`,
  );
}

ajv.addSchema({ ...bundle, $id: bundleId });

const exportNames = {};
for (const name of entrypoints) {
  if (!IDENTIFIER.test(name)) {
    throw new Error(
      `Entrypoint ${JSON.stringify(name)} is not a valid ES export name; ` +
        "standalone validation needs one binding per entrypoint",
    );
  }
  ajv.addSchema({ $id: entryId(name), $ref: `${bundleId}#/$defs/${name}` }, name);
  exportNames[name] = name;
}

let code = standaloneCode(ajv, exportNames);

const unknownFormat = compileWarnings.find((message) => message.includes("unknown format"));
if (unknownFormat) {
  throw new Error(
    `${schemaPath} produced an "unknown format" warning while compiling, so the emitted ` +
      `validator would not enforce it: ${unknownFormat}`,
  );
}

// The validator code is internal to the module; only the record is exported. That
// keeps the module surface stable when schemas are added or renamed.
code = code.replaceAll("export const ", "const ");
code = `${code}export const validators = {${entrypoints
  .map((name) => `${JSON.stringify(name)}: ${name}`)
  .join(",")}};`;

// Ajv 8 emits `require()` for the runtime helpers it cannot inline (equal,
// ucs2length, formats) even in ESM mode. Rewrite those into namespace imports and
// pick the CommonJS export object the way the generated call sites expect:
// `require("x").default` and `require("x").fullFormats`. Node resolves `default` to
// `module.exports`; esbuild's dev pre-bundle resolves it to `module.exports.default`
// for modules that set `__esModule`, so one shim covers both interpretations.
const runtimeModules = new Map();
code = code.replace(/require\("([^"]+)"\)/g, (_, specifier) => {
  // Ajv's helper specifiers are CommonJS-style (no extension). Node's ESM resolver
  // needs the real file path because `ajv` publishes no `exports` map.
  const resolved = specifier.endsWith(".js") ? specifier : `${specifier}.js`;
  if (!runtimeModules.has(resolved)) {
    runtimeModules.set(resolved, runtimeModules.size);
  }
  return `__cjs${runtimeModules.get(resolved)}`;
});
const header = [
  "// Generated from contracts/bindings.json; do not edit.",
  // Compiled validation code trips rules that assume hand-written code (redeclared
  // `const`s per function scope, unused branches, assignments that only feed a
  // later `return`). Disabling exactly those rules keeps ESLint parsing the artifact
  // and keeps `no-undef` active, which the ignore-file approach gave up. The bytes
  // are still checked by `make contracts-drift`.
  "/* eslint-disable no-redeclare, no-useless-assignment, no-unreachable, no-empty, " +
    "no-control-regex, @typescript-eslint/no-unused-vars, " +
    "@typescript-eslint/no-unused-expressions */",
  ...[...runtimeModules].flatMap(([specifier, index]) => [
    `import * as __module${index} from "${specifier}";`,
    `const __cjs${index} =`,
    `  __module${index}.default && __module${index}.default.__esModule === true`,
    `    ? __module${index}.default`,
    `    : __module${index};`,
  ]),
  "",
].join("\n");

writeFileSync(outputPath, `${header}${code}\n`);

// Sibling declaration so the generated TypeScript wrappers can import the module
// under `moduleResolution: Bundler` without an implicit-any error.
writeFileSync(
  `${outputPath.slice(0, -".mjs".length)}.d.mts`,
  "// Generated from contracts/bindings.json; do not edit.\n" +
    "export const validators: Record<string, (value: unknown) => boolean>;\n",
);
