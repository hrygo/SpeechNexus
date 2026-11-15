import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

// `src/speechnexus_contracts/*.d.mts` are generated declarations next to the
// generated validators; they are in `packages/contracts/tsconfig.json` so typed rules
// can read them, and they are linted (not ignored) so a broken artifact is parsed.
const browserFiles = [
  "web/**/*.{ts,tsx}",
  "packages/contracts/src/*.{ts,mts}",
  "packages/contracts/src/speechnexus_contracts/*.d.mts",
];
const appFiles = ["web/admin/src/**/*.{ts,tsx}", "web/workbench/src/**/*.{ts,tsx}"];

export default tseslint.config(
  {
    ignores: [
      "**/dist/**",
      "**/coverage/**",
      "**/node_modules/**",
      "**/.venv/**",
      "**/playwright-report/**",
      "**/test-results/**",
      "packages/contracts/src/*.generated.ts",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    files: browserFiles,
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      ...reactHooks.configs.flat.recommended.rules,
      "@typescript-eslint/consistent-type-imports": ["error", { fixStyle: "inline-type-imports" }],
      "@typescript-eslint/no-misused-promises": [
        "error",
        { checksVoidReturn: { attributes: false } },
      ],
    },
  },
  {
    files: appFiles,
    plugins: {
      "react-refresh": reactRefresh,
    },
    rules: {
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    },
  },
  {
    files: ["web/**/vite.config.ts", "eslint.config.js"],
    languageOptions: {
      globals: globals.node,
    },
  },
  {
    files: [
      "scripts/dev-env/*.mjs",
      "packages/contracts/**/*.mjs",
      "tests/web/*.mjs",
      "playwright.*.config.mjs",
    ],
    ...tseslint.configs.disableTypeChecked,
    languageOptions: {
      globals: {
        ...globals.node,
        ...globals.browser,
      },
    },
  },
  {
    // The generated validators carry one `eslint-disable` header for the rules that
    // compiled codegen output trips (see generate-standalone-validators.mjs). Which of
    // them fire differs per bundle, so the directive is broader than any single file
    // needs; the artifact bytes are checked by `make contracts-drift` instead.
    files: ["packages/contracts/src/speechnexus_contracts/*.validators.generated.mjs"],
    linterOptions: { reportUnusedDisableDirectives: "off" },
  },
);
