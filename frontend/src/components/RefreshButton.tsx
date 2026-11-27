"use client";

import { t } from "@/i18n";

export function RefreshButton({ onRefresh, busy }: { onRefresh: () => void; busy: boolean }) {
  return (
    <button type="button" onClick={onRefresh} disabled={busy}>
      {busy ? t.ui.refreshing : t.ui.refresh}
    </button>
  );
}
