import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";
import ts from "typescript";
import {
  generateTypescript,
  normalizeReferencedHeaders,
} from "../../../scripts/dev-env/normalize-typescript-headers.mjs";

function assertConsumerCompiles(directory, consumer) {
  writeFileSync(join(directory, "consumer.ts"), consumer);
  const program = ts.createProgram([join(directory, "consumer.ts")], {
    noEmit: true,
    strict: true,
    skipLibCheck: true,
    types: [],
    target: ts.ScriptTarget.ES2023,
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
  });
  assert.deepEqual(
    ts
      .getPreEmitDiagnostics(program)
      .map((d) => ts.flattenDiagnosticMessageText(d.messageText, "\n")),
    [],
  );
}

test("actual compiler consumers preserve optional referenced headers and required controls", async () => {
  const response = {
    description: "Synthetic header compilation fixture",
    headers: {
      Optional: { $ref: "#/components/headers/Optional" },
      Alias: { $ref: "#/components/headers/Alias" },
      ExplicitOptional: { $ref: "#/components/headers/ExplicitOptional" },
      Required: { $ref: "#/components/headers/Required" },
      InlineRequired: { required: true, schema: { type: "string" } },
    },
    content: { "application/json": { schema: { type: "string" } } },
  };
  const source = {
    openapi: "3.1.0",
    info: { title: "Synthetic compile fixture", version: "0" },
    paths: {
      "/named": {
        get: {
          operationId: "getNamed",
          responses: { 200: response, 503: { $ref: "#/components/responses/Unavailable" } },
        },
      },
      "/anonymous": { get: { responses: { 200: response } } },
    },
    components: {
      headers: {
        Optional: { schema: { type: "integer", minimum: 0 } },
        Alias: { $ref: "#/components/headers/Optional" },
        ExplicitOptional: { required: false, schema: { type: "string" } },
        Required: { required: true, schema: { type: "string" } },
      },
      responses: { Unavailable: response },
    },
  };
  const before = structuredClone(source);
  const output = await generateTypescript(source);
  assert.deepEqual(source, before);
  // A same-named business field and the components registry are not response header maps.
  const bodyType =
    '\nexport interface BusinessBody { headers: { value: components["headers"]["Optional"] } }';
  const unchangedBody = normalizeReferencedHeaders(bodyType, { Optional: false });
  assert.equal(unchangedBody, bodyType);
  const directory = mkdtempSync(join(tmpdir(), "speechnexus-header-compile-"));
  try {
    writeFileSync(join(directory, "types.ts"), output + bodyType);
    assertConsumerCompiles(
      directory,
      `
      import type {components, operations, paths, BusinessBody} from './types';
      type Direct = operations['getNamed']['responses'][200]['headers'];
      type Reused = components['responses']['Unavailable']['headers'];
      type Anonymous = paths['/anonymous']['get']['responses'][200]['headers'];
      const omitted: Direct = {Required:'synthetic', InlineRequired:'synthetic'};
      const shared: Reused = omitted;
      const anonymous: Anonymous = omitted;
      const present: Direct = {...omitted,Optional:1,ExplicitOptional:'synthetic'};
      // @ts-expect-error Required referenced Header Object remains mandatory.
      const missingRequired: Direct = {InlineRequired:'synthetic'};
      // @ts-expect-error Required inline Header Object remains mandatory.
      const missingInline: Direct = {Required:'synthetic'};
      // @ts-expect-error Optional does not mean accepting the wrong value type.
      const wrongValue: Direct = {...omitted,Optional:'invalid'};
      // @ts-expect-error Components header definitions remain required registry keys.
      const registry: components['headers'] = {Required:'synthetic'};
      // @ts-expect-error Business fields called headers are not made optional.
      const body: BusinessBody = {headers:{}};
      void [shared,anonymous,present,missingRequired,missingInline,wrongValue,registry,body];
    `,
    );
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test("external response/header aliases use the same bundled identity as the compiler", async () => {
  const directory = mkdtempSync(join(tmpdir(), "speechnexus-external-header-"));
  try {
    const external = {
      openapi: "3.1.0",
      info: { title: "Synthetic external header", version: "0" },
      paths: {},
      components: {
        headers: {
          Wait: { schema: { type: "integer", minimum: 0 } },
          Required: { required: true, schema: { type: "string" } },
          Alias: { $ref: "#/components/headers/Wait" },
        },
        responses: {
          Unavailable: {
            description: "Synthetic unavailable",
            headers: {
              Wait: { $ref: "#/components/headers/Alias" },
              Required: { $ref: "#/components/headers/Required" },
            },
          },
        },
      },
    };
    writeFileSync(join(directory, "external.openapi.json"), JSON.stringify(external));
    const input = join(directory, "api.openapi.json");
    writeFileSync(
      input,
      JSON.stringify({
        openapi: "3.1.0",
        info: { title: "Synthetic external consumer", version: "0" },
        paths: {
          "/external": {
            get: {
              operationId: "getExternal",
              responses: {
                503: { $ref: "external.openapi.json#/components/responses/Unavailable" },
              },
            },
          },
        },
      }),
    );
    writeFileSync(join(directory, "types.ts"), await generateTypescript(pathToFileURL(input)));
    assertConsumerCompiles(
      directory,
      `
      import type {operations} from './types';
      type Headers=operations['getExternal']['responses'][503]['headers'];
      const omitted:Headers={Required:'synthetic'};
      const present:Headers={...omitted,Wait:1};
      // @ts-expect-error External required Header Object remains required.
      const missing:Headers={};
      // @ts-expect-error External optional Header Object retains its value type.
      const invalid:Headers={...omitted,Wait:'invalid'};
      void [present,missing,invalid];
    `,
    );
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test("unknown header metadata and non-boolean presence fail closed", () => {
  const raw =
    'export interface components {responses:{R:{headers:{H:components["headers"]["H"]}}};headers:{H:string}}';
  assert.throws(() => normalizeReferencedHeaders(raw, {}), /Unresolved referenced Header Object/);
  assert.throws(() => normalizeReferencedHeaders(raw, { H: "false" }), /must be boolean/);
});
