import { Suspense, type ReactNode } from "react";

import { AppShell } from "@/components/AppShell";
import { t } from "@/i18n";

export default function AppLayout({ children }: { children: ReactNode }) {
  // usePathname() в каркасе известен только во время запроса: до него показывается «Загрузка».
  return (
    <Suspense fallback={<p className="loading">{t.ui.loading}</p>}>
      <AppShell>{children}</AppShell>
    </Suspense>
  );
}
