// SPDX-License-Identifier: GPL-3.0-only
import { describe, expect, it } from "vitest";

import { formatBytes, formatDateTime } from "./format";

describe("formatBytes", () => {
  it.each([
    [0, "0 B"],
    [-5, "0 B"],
    [Number.NaN, "0 B"],
    [512, "512 B"],
    [1536, "1,5 KB"],
    [3.4 * 1024 * 1024, "3,4 MB"],
    [5 * 1024 ** 3, "5 GB"],
    [2 * 1024 ** 5, "2.048 TB"],
  ])("formata %s como %s", (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });
});

describe("formatDateTime", () => {
  it("formata data e hora em pt-BR no fuso informado", () => {
    expect(formatDateTime("2026-03-05T14:30:00Z", "UTC")).toMatch(/05\/03\/2026,? 14:30/);
  });

  it("usa o fuso local quando nenhum é informado", () => {
    expect(formatDateTime("2026-03-05T14:30:00Z")).toMatch(/\d{2}\/\d{2}\/\d{4}/);
  });

  it.each([null, "não é data"])("devolve traço para %s", (value) => {
    expect(formatDateTime(value)).toBe("—");
  });
});
