import { Suspense } from "react";

import { JournalPage } from "@/components/JournalPage";
import { t } from "@/i18n";

// Без Suspense next build падает при cacheComponents: useParams читает параметры маршрута.
export default function Page() {
  return (
    <Suspense fallback={<p>{t.ui.loading}</p>}>
      <JournalPage />
    </Suspense>
  );
}
