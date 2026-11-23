// SPDX-License-Identifier: GPL-3.0-only

const KIB = 1024;
const UNITS = ["B", "KB", "MB", "GB", "TB"] as const;
const LOCALE = "pt-BR";

const NUMBER_FORMAT = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 1 });

/** Volume legível em unidades binárias ("3,4 MB"); entradas inválidas viram "0 B". */
export function formatBytes(bytes: number): string {
  let value = Number.isFinite(bytes) && bytes > 0 ? bytes : 0;
  let unit = 0;
  while (value >= KIB && unit < UNITS.length - 1) {
    value /= KIB;
    unit += 1;
  }
  return `${NUMBER_FORMAT.format(value)} ${UNITS[unit] ?? "B"}`;
}

/** Data e hora locais em pt-BR; ausente ou inválida vira "—". */
export function formatDateTime(iso: string | null, timeZone?: string): string {
  if (iso === null) {
    return "—";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return new Intl.DateTimeFormat(LOCALE, {
    dateStyle: "short",
    timeStyle: "short",
    ...(timeZone === undefined ? {} : { timeZone }),
  }).format(date);
}
