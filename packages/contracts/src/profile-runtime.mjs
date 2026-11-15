import { ContractValidationError } from "./errors.mjs";

/**
 * Build one contract parser from a generated validator record.
 *
 * Validators are precompiled by `packages/contracts/scripts/generate-standalone-validators.mjs`
 * at generation time. Compiling at runtime (`ajv.compile()`) needs `new Function()`,
 * which the deployment CSP forbids (`deploy/web/nginx.conf`: `script-src 'self'`);
 * a deployed page failed on its first parse with `EvalError`. Nothing here evaluates
 * strings, so the CSP does not have to be weakened.
 */
export function createProfileParser(validators) {
  if (!validators || typeof validators !== "object") {
    throw new TypeError(
      "createProfileParser needs the generated validator record; " +
        "run `make contracts-generate` and import `<profile>.validators.generated.mjs`",
    );
  }
  return (name, value) => {
    if (!Object.hasOwn(validators, name)) throw new Error("Unknown profile contract");
    if (!validators[name](value)) {
      throw new ContractValidationError(`Invalid ${name}`);
    }
    return value;
  };
}
