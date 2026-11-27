import { defineConfig } from "@playwright/test";

const port = process.env.KASPISA_E2E_PORT ?? "3107";
const origin = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "e2e",
  testMatch: "*.spec.ts",
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: origin,
    // Установленный Chrome: без загрузки браузеров Playwright (загрузка — сеть).
    channel: process.env.KASPISA_E2E_CHANNEL ?? "chrome",
    // В трейсах только выдуманные данные заглушки.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "desktop",
      testIgnore: "screens.spec.ts",
      use: { viewport: { width: 1280, height: 800 }, colorScheme: "light" },
    },
    {
      name: "mobile",
      testMatch: "layout.spec.ts",
      use: {
        viewport: { width: 375, height: 740 },
        hasTouch: true,
        isMobile: true,
        colorScheme: "light",
      },
    },
    {
      name: "screens",
      testMatch: "screens.spec.ts",
      use: { viewport: { width: 1280, height: 800 }, colorScheme: "light" },
    },
  ],
  webServer: {
    command: `npx next build && npx next start --hostname 127.0.0.1 --port ${port}`,
    url: origin,
    reuseExistingServer: false,
    timeout: 180_000,
    env: { NEXT_TELEMETRY_DISABLED: "1", KASPISA_E2E_DIST_DIR: ".next-e2e" },
  },
});
