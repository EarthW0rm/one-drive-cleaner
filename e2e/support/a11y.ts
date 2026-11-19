// SPDX-License-Identifier: GPL-3.0-only
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

const BLOCKING_IMPACTS = new Set(["serious", "critical"]);

/**
 * Varre a página com o axe-core (regras WCAG 2.x A/AA e de boas práticas) e exige zero violações
 * `serious` ou `critical`; as demais (`minor`, `moderate`) não bloqueiam. A mensagem de falha
 * lista regra, impacto e nós afetados.
 */
export async function expectNoBlockingA11yViolations(
  page: Page,
  screen: string,
  /** Seletor a varrer; em modais e gavetas, só o diálogo (o fundo fica sob a máscara). */
  scope?: string,
): Promise<void> {
  // Transições em andamento (fade de botões, ondas do antd) distorcem as cores medidas.
  await page.addStyleTag({
    content: "*, *::before, *::after { transition: none !important; animation: none !important; }",
  });
  const axe = new AxeBuilder({ page });
  const { violations } = await (scope === undefined ? axe : axe.include(scope)).analyze();
  const blocking = violations
    .filter((violation) => BLOCKING_IMPACTS.has(violation.impact ?? ""))
    .map(
      (violation) =>
        `${violation.id} (${violation.impact ?? "?"}): ${violation.help}\n` +
        violation.nodes
          .slice(0, 5)
          .map(
            (node) =>
              `    ${node.target.join(" ")} ${node.html.slice(0, 120)}\n      ${(node.failureSummary ?? "").replace(/\s+/g, " ")}`,
          )
          .join("\n"),
    );
  expect.soft(blocking, `violações de acessibilidade em ${screen}`).toEqual([]);
}
