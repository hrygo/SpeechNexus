/**
 * Contract `format` stays enforced in the precompiled validators.
 *
 * The runtime no longer compiles schemas (#840): `createProfileParser` only runs the
 * functions emitted by `scripts/generate-standalone-validators.mjs`. Those functions
 * enforce `format` only because that script registers `ajv-formats` before compiling.
 *
 * Two guards keep that true, in this order:
 *
 * 1. `make contracts-generate` refuses a bundle whose `format` values are not all
 *    registered in `ajv-formats` (see `collectFormats` in the script), so adding e.g.
 *    `uuid` or `email` works while a typo fails generation instead of being ignored.
 * 2. **Deleting the `addFormats(ajv)` call makes the negative assertions below go
 *    green** once that generation guard is also bypassed, and nothing else reports it:
 *    `strict: false` downgrades the format to a warning, the schema bytes do not
 *    change, so there is no `make contracts-drift` failure and no browser symptom.
 *    That is the silent weakening this file exists to catch.
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";

import * as ajvFormats from "ajv-formats/dist/formats.js";

import { validators as fileValidators } from "../src/speechnexus_contracts/file.validators.generated.mjs";

const fixtures = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/contracts/file-workflow.json", import.meta.url), {
    encoding: "utf8",
  }),
);
const assetExample = fixtures.examples.find((entry) => entry.id === "ready-audio");

test("the shipped validator accepts the valid Asset example", () => {
  assert.equal(fileValidators.Asset(structuredClone(assetExample.value)), true);
});

test("the shipped validator rejects a malformed date-time", () => {
  const broken = structuredClone(assetExample.value);
  broken.created_at = "2026-13-45T99:99:99Z";
  assert.equal(fileValidators.Asset(broken), false);
});

test("every format used by the shipped bundles is registered in ajv-formats", () => {
  const directory = new URL("../src/speechnexus_contracts/", import.meta.url);
  const bundles = readdirSync(directory).filter((name) => name.endsWith(".schema.json"));
  assert.ok(bundles.length > 0, "no generated schema bundles found");

  const used = new Map();
  const walk = (node, bundle, pointer) => {
    if (Array.isArray(node)) {
      node.forEach((value, index) => walk(value, bundle, `${pointer}/${index}`));
      return;
    }
    if (!node || typeof node !== "object") return;
    for (const [key, value] of Object.entries(node)) {
      if (key === "format" && typeof value === "string") {
        used.set(value, `${bundle}:${pointer}`);
      } else {
        walk(value, bundle, `${pointer}/${key}`);
      }
    }
  };
  for (const bundle of bundles) {
    walk(JSON.parse(readFileSync(new URL(bundle, directory), { encoding: "utf8" })), bundle, "");
  }

  assert.ok(used.size > 0, "no format constraints found; the walk is not looking at schemas");
  const unregistered = [...used].filter(
    ([format]) => !Object.hasOwn(ajvFormats.fullFormats, format),
  );
  assert.deepEqual(
    unregistered,
    [],
    `unregistered format(s) would be ignored by Ajv: ${JSON.stringify(unregistered)}`,
  );
});
