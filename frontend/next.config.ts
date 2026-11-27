import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Минимальная сборка для образа Docker: .next/standalone/server.js.
  output: "standalone",
  cacheComponents: true,
  partialPrefetching: true,
  // Сквозной тест собирается в отдельный каталог и не мешает dev-серверу.
  distDir: process.env.KASPISA_E2E_DIST_DIR ?? ".next",
  experimental: {
    // Прокси rewrites по умолчанию рвёт запрос через 30 с, а каталог грузится до 240 с.
    proxyTimeout: 300_000,
  },
  rewrites: async () => [
    {
      source: "/api/:path*",
      destination: `${process.env.KASPISA_API_ORIGIN || "http://127.0.0.1:8000"}/api/:path*`,
    },
  ],
};

export default nextConfig;
