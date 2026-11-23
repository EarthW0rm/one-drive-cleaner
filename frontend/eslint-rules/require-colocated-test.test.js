// SPDX-License-Identifier: GPL-3.0-only
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { RuleTester } from "eslint";
import tseslint from "typescript-eslint";
import { afterAll, describe, it } from "vitest";

import rule from "./require-colocated-test.js";

RuleTester.describe = describe;
RuleTester.it = it;
RuleTester.afterAll = afterAll;

const dir = mkdtempSync(path.join(tmpdir(), "odc-rule-"));
const touch = (name) => {
  const file = path.join(dir, name);
  writeFileSync(file, "");
  return file;
};
const withTest = touch("with-test.ts");
touch("with-test.test.ts");
const tsxWithTest = touch("Widget.tsx");
touch("Widget.test.tsx");
const tsWithTsxTest = touch("hook.ts");
touch("hook.test.tsx");
const withoutTest = touch("orphan.ts");
const tsxWithoutTest = touch("Orphan.tsx");
const testFile = touch("legacy.test.ts");
const declaration = touch("types.d.ts");
const script = touch("script.js");
const excluded = touch("excluded.ts");

afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

const tester = new RuleTester({
  languageOptions: { parser: tseslint.parser },
});

tester.run("require-colocated-test", rule, {
  valid: [
    { code: "export const a = 1;", filename: withTest },
    { code: "export const a = 1;", filename: tsxWithTest },
    { code: "export const a = 1;", filename: tsWithTsxTest },
    { code: "export const a = 1;", filename: testFile },
    { code: "export const a = 1;", filename: declaration },
    { code: "export const a = 1;", filename: script },
    {
      code: "export const a = 1;",
      filename: excluded,
      options: [{ exclude: [path.relative(process.cwd(), excluded).split(path.sep).join("/")] }],
    },
  ],
  invalid: [
    {
      code: "export const a = 1;",
      filename: withoutTest,
      errors: [{ messageId: "missing", data: { expected: "orphan.test.ts" } }],
    },
    {
      code: "export const a = 1;",
      filename: tsxWithoutTest,
      errors: [{ messageId: "missing", data: { expected: "Orphan.test.tsx" } }],
    },
  ],
});
