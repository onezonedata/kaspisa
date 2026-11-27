import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { generateSchemaTypes } from "../../scripts/api-types.mjs";

const hasUv = spawnSync("uv", ["--version"]).error === undefined;

// Т11: schema.d.ts совпадает со схемой бэкенда. Без uv тест пропущен и виден как skipped.
it.skipIf(!hasUv)(
  hasUv ? "schema.d.ts совпадает со схемой бэкенда" : "schema.d.ts не сверен: нет uv",
  async () => {
    const saved = readFileSync(join(__dirname, "schema.d.ts"), "utf8");
    const fresh: string = await generateSchemaTypes();
    expect(fresh === saved, "схема API изменилась: npm run api:types").toBe(true);
  },
  60_000,
);
