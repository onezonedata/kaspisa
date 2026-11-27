// Запуск сквозного теста: без браузера явно пропускает его, иначе запускает Playwright.
import { spawnSync } from "node:child_process";

import { chromium } from "@playwright/test";

const channel = process.env.KASPISA_E2E_CHANNEL ?? "chrome";

try {
  const browser = await chromium.launch({ channel });
  await browser.close();
} catch (error) {
  const first = String(error?.message ?? error).split("\n")[0];
  console.error(
    [
      "",
      "################################################################",
      `СКВОЗНОЙ ТЕСТ ПРОПУЩЕН: браузер ${channel} не запускается (${first}).`,
      "Установите Google Chrome или выполните npx playwright install chromium",
      "и задайте KASPISA_E2E_CHANNEL=chromium.",
      "################################################################",
      "",
    ].join("\n"),
  );
  // В CI пропуск недопустим: без браузера сквозные тесты молча не проходят.
  process.exit(process.env.CI === "true" ? 1 : 0);
}

const run = spawnSync("npx", ["playwright", "test"], { stdio: "inherit" });
process.exit(run.status ?? 1);
