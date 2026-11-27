import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import pkg from "../../package.json";

describe("версия (Т14)", () => {
  it("package.json и backend/pyproject.toml совпадают", () => {
    const toml = readFileSync(resolve(__dirname, "../../../backend/pyproject.toml"), "utf8");
    const match = /^version = "(\d+\.\d+\.\d+)"$/m.exec(toml);
    expect(match).not.toBeNull();
    expect(pkg.version).toBe(match![1]);
  });
});
