// Генерация типов OpenAPI: схему отдаёт бэкенд, типы пишет openapi-typescript.
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import openapiTS, { astToString } from "openapi-typescript";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
export const schemaPath = join(root, "src", "api", "schema.d.ts");

/** Возвращает текст schema.d.ts по схеме текущего бэкенда (нужен uv и окружение бэкенда). */
export async function generateSchemaTypes() {
  const json = execFileSync(
    "uv",
    ["--directory", "../backend", "run", "--no-sync", "python", "-m", "kaspisa.api.openapi"],
    { cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
  );
  // defaultNonNullable: false — поля со значением по умолчанию остаются необязательными.
  const ast = await openapiTS(JSON.parse(json), { defaultNonNullable: false });
  return astToString(ast);
}

if (process.argv.includes("--write")) {
  writeFileSync(schemaPath, await generateSchemaTypes());
}
