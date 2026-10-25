// Compiler adapter: openapi-typescript 7.13 treats every referenced header as required.
// Restore the source Header Object's required flag without changing its value type.
import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import openapiTS, { astToString, COMMENT_HEADER } from "openapi-typescript";
import ts from "typescript";

function nameOf(node) {
  return node && (ts.isIdentifier(node) || ts.isStringLiteral(node) || ts.isNumericLiteral(node))
    ? node.text
    : undefined;
}

function referencedHeader(type) {
  if (!type || !ts.isIndexedAccessTypeNode(type)) return undefined;
  const inner = type.objectType;
  if (
    !ts.isIndexedAccessTypeNode(inner) ||
    !ts.isTypeReferenceNode(inner.objectType) ||
    nameOf(inner.objectType.typeName) !== "components" ||
    !ts.isLiteralTypeNode(inner.indexType) ||
    nameOf(inner.indexType.literal) !== "headers" ||
    !ts.isLiteralTypeNode(type.indexType)
  )
    return undefined;
  return nameOf(type.indexType.literal);
}

function responseHeaders(path) {
  if (path.at(-1) !== "headers") return false;
  if (path[0] === "components" && path[1] === "responses" && path.length === 4) return true;
  if (path[0] === "operations" && path[2] === "responses" && path.length === 5) return true;
  if (["paths", "webhooks"].includes(path[0]) && path[3] === "responses" && path.length === 6)
    return true;
  return (
    path[0] === "components" &&
    path[1] === "pathItems" &&
    path[4] === "responses" &&
    path.length === 7
  );
}

export function normalizeReferencedHeaders(text, required) {
  if (
    typeof required !== "function" &&
    Object.values(required).some((value) => typeof value !== "boolean")
  ) {
    throw Error("Header required flags must be boolean");
  }
  let changes = 0;
  const source = ts.createSourceFile("generated.ts", text, ts.ScriptTarget.Latest, true);
  const result = ts.transform(source, [
    (context) => {
      function visit(node, path = []) {
        const current = ts.isInterfaceDeclaration(node)
          ? [node.name.text]
          : ts.isPropertySignature(node)
            ? [...path, nameOf(node.name)]
            : path;
        if (
          ts.isPropertySignature(node) &&
          responseHeaders(current) &&
          node.type &&
          ts.isTypeLiteralNode(node.type)
        ) {
          const members = node.type.members.map((member) => {
            if (!ts.isPropertySignature(member)) return member;
            const header = referencedHeader(member.type);
            if (header === undefined) return member;
            const flag =
              typeof required === "function"
                ? required(header)
                : Object.hasOwn(required, header)
                  ? required[header]
                  : undefined;
            if (typeof flag !== "boolean") {
              throw Error("Unresolved referenced Header Object in generated response");
            }
            const optional = !flag;
            if (optional === Boolean(member.questionToken)) return member;
            ++changes;
            return ts.factory.updatePropertySignature(
              member,
              member.modifiers,
              member.name,
              optional ? ts.factory.createToken(ts.SyntaxKind.QuestionToken) : undefined,
              member.type,
            );
          });
          return ts.factory.updatePropertySignature(
            node,
            node.modifiers,
            node.name,
            node.questionToken,
            ts.factory.updateTypeLiteralNode(node.type, members),
          );
        }
        return ts.visitEachChild(node, (child) => visit(child, current), context);
      }
      return (node) => visit(node);
    },
  ]);
  try {
    return changes
      ? ts.createPrinter({ newLine: ts.NewLineKind.LineFeed }).printFile(result.transformed[0])
      : text;
  } finally {
    result.dispose();
  }
}

export async function generateTypescript(source) {
  let resolve;
  const nodes = await openapiTS(source, {
    defaultNonNullable: false,
    postTransform(_node, options) {
      // Use the compiler's bundled document, including external aliases and
      // renamed components. No second resolver or schema rewrite is used.
      resolve ??= options.ctx.resolve;
    },
  });
  return normalizeReferencedHeaders(COMMENT_HEADER + astToString(nodes), (name) => {
    const token = name.replaceAll("~", "~0").replaceAll("/", "~1");
    const header = resolve?.(`#/components/headers/${token}`);
    if (!header || typeof header !== "object") {
      throw Error("Unresolved or invalid referenced Header Object");
    }
    const required = Object.hasOwn(header, "required") ? header.required : false;
    if (typeof required !== "boolean") throw Error("Header required flag must be boolean");
    return required;
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [source, output] = process.argv.slice(2);
  writeFileSync(output, await generateTypescript(pathToFileURL(source)));
}
