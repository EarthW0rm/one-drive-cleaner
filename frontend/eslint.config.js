// SPDX-License-Identifier: GPL-3.0-only
import js from "@eslint/js";
import vitest from "@vitest/eslint-plugin";
import { createTypeScriptImportResolver } from "eslint-import-resolver-typescript";
import { importX } from "eslint-plugin-import-x";
import jsxA11y from "eslint-plugin-jsx-a11y";
import playwright from "eslint-plugin-playwright";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import sonarjs from "eslint-plugin-sonarjs";
import testingLibrary from "eslint-plugin-testing-library";
import globals from "globals";
import tseslint from "typescript-eslint";

import requireColocatedTest from "./eslint-rules/require-colocated-test.js";

const TEST_FILES = ["**/*.test.{ts,tsx,js}"];

export default tseslint.config(
  { ignores: ["node_modules/**", "coverage/**", "dist/**", "src/api/schema.gen.ts"] },
  js.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    extends: [
      ...tseslint.configs.strictTypeChecked,
      ...tseslint.configs.stylisticTypeChecked,
      react.configs.flat.recommended,
      react.configs.flat["jsx-runtime"],
      reactHooks.configs.flat.recommended,
      jsxA11y.flatConfigs.strict,
      importX.flatConfigs.recommended,
      importX.flatConfigs.typescript,
      sonarjs.configs.recommended,
    ],
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
      globals: globals.browser,
    },
    settings: {
      react: { version: "19.0" },
      "import-x/resolver-next": [createTypeScriptImportResolver()],
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "no-console": "error",
      "import-x/order": ["error", { alphabetize: { order: "asc" }, "newlines-between": "always" }],
      "import-x/no-cycle": "error",
      "sonarjs/cognitive-complexity": ["error", 15],
      "no-restricted-imports": [
        "error",
        {
          paths: [],
        },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "CallExpression[callee.object.name='message'][callee.property.name=/^(success|error|info|warning|loading|open)$/]",
          message: "API estática message.* proibida: use App.useApp().",
        },
        {
          selector:
            "CallExpression[callee.object.name='Modal'][callee.property.name=/^(confirm|info|success|error|warning)$/]",
          message: "API estática Modal.* proibida: use App.useApp().",
        },
      ],
      "local/require-colocated-test": [
        "error",
        { exclude: ["src/main.tsx", "src/api/schema.gen.ts"] },
      ],
    },
    plugins: { local: { rules: { "require-colocated-test": requireColocatedTest } } },
  },
  {
    files: ["src/test/**/*.ts", "vite.config.ts", "vitest.config.ts"],
    rules: { "local/require-colocated-test": "off" },
  },
  {
    files: TEST_FILES,
    extends: [vitest.configs.recommended, testingLibrary.configs["flat/react"]],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["**/*.js"],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["e2e/**/*.ts"],
    extends: [playwright.configs["flat/recommended"]],
  },
);
