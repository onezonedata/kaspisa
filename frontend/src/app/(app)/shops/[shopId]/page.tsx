import { Suspense } from "react";

import { ShopPage } from "@/components/ShopPage";
import { t } from "@/i18n";

// Без Suspense next build падает при cacheComponents: useParams читает параметры маршрута.
export default function Page() {
  return (
    <Suspense fallback={<p>{t.ui.loading}</p>}>
      <ShopPage />
    </Suspense>
  );
}
