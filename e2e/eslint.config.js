// SPDX-License-Identifier: GPL-3.0-only
import js from "@eslint/js";
import playwright from "eslint-plugin-playwright";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["node_modules/**", "playwright-report/**", "test-results/**"] },
  js.configs.recommended,
  {
    files: ["**/*.ts"],
    extends: [
      ...tseslint.configs.strictTypeChecked,
      ...tseslint.configs.stylisticTypeChecked,
      playwright.configs["flat/recommended"],
    ],
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
      globals: globals.node,
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "no-console": "error",
      // Fixtures usadas só pelo efeito (`signedIn`) entram como `_nome` na assinatura do teste.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["**/*.js"],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: globals.node },
  },
);
