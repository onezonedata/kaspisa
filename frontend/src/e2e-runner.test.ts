import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

function runE2e(ci: boolean) {
  const env: NodeJS.ProcessEnv = { ...process.env, KASPISA_E2E_CHANNEL: "kaspisa-no-such-channel" };
  if (ci) env.CI = "true";
  else delete env.CI;
  return spawnSync("node", ["scripts/e2e.mjs"], {
    cwd: resolve(__dirname, ".."),
    env,
    encoding: "utf8",
  });
}

describe("запуск сквозных тестов без браузера (Т23)", () => {
  it("при CI=true завершается с кодом 1", () => {
    const run = runE2e(true);
    expect(run.status).toBe(1);
    expect(run.stderr).toContain("kaspisa-no-such-channel");
  });

  it("без CI пропускает с кодом 0 и заметным сообщением", () => {
    const run = runE2e(false);
    expect(run.status).toBe(0);
    expect(run.stderr).toContain("СКВОЗНОЙ ТЕСТ ПРОПУЩЕН");
  });
});
