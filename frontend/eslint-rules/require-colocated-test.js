// SPDX-License-Identifier: GPL-3.0-only
import { existsSync } from "node:fs";
import path from "node:path";

const SOURCE = /\.(ts|tsx)$/;
const TEST = /\.test\.(ts|tsx)$/;
const DECLARATION = /\.d\.ts$/;

/** Regra local: arquivo `.ts/.tsx` de produção exige `*.test.ts(x)` co-localizado. */
export default {
  meta: {
    type: "problem",
    docs: { description: "Exige teste unitário co-localizado para cada arquivo de produção." },
    schema: [
      {
        type: "object",
        properties: { exclude: { type: "array", items: { type: "string" } } },
        additionalProperties: false,
      },
    ],
    messages: {
      missing: "Arquivo de produção sem teste co-localizado: esperado {{expected}}.",
    },
  },
  create(context) {
    const [options] = context.options;
    const exclude = options?.exclude ?? [];
    const filename = context.filename;
    const relative = path.relative(context.cwd, filename).split(path.sep).join("/");
    const skip =
      !SOURCE.test(filename) ||
      TEST.test(filename) ||
      DECLARATION.test(filename) ||
      exclude.includes(relative);
    if (skip) {
      return {};
    }
    const parsed = path.parse(filename);
    const expectedNames = [`${parsed.name}.test${parsed.ext}`];
    if (parsed.ext === ".ts") {
      expectedNames.push(`${parsed.name}.test.tsx`);
    }
    return {
      Program(node) {
        const found = expectedNames.some((name) => existsSync(path.join(parsed.dir, name)));
        if (!found) {
          context.report({
            node,
            messageId: "missing",
            data: { expected: expectedNames[0] },
          });
        }
      },
    };
  },
};
