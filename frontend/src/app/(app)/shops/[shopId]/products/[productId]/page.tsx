import { Suspense } from "react";

import { ProductPage } from "@/components/ProductPage";
import { t } from "@/i18n";

// Без Suspense next build падает при cacheComponents: useParams читает параметры маршрута.
export default function Page() {
  return (
    <Suspense fallback={<p>{t.ui.loading}</p>}>
      <ProductPage />
    </Suspense>
  );
}
