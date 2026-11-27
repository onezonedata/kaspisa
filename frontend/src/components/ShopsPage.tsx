"use client";

import { useState } from "react";

import { listShops, type ApiError } from "@/api/client";
import type { Shop } from "@/api/types";
import { t } from "@/i18n";
import { useLoad } from "@/lib/use-load";
import { AddShopForm } from "./AddShopForm";
import { ErrorMessage } from "./ErrorMessage";
import { ShopList } from "./ShopList";

export function ShopsPage() {
  const { data, error, loading, reload } = useLoad(listShops);
  const [limitError, setLimitError] = useState<ApiError | null>(null);
  const hasShops = data !== null && data.length > 0;

  function onAdded(_shop: Shop | null, limit?: ApiError) {
    setLimitError(limit ?? null);
    reload();
  }

  return (
    <main className="page-narrow">
      <h1>{t.ui.shopsTitle}</h1>
      <ErrorMessage error={error} />
      {hasShops ? <ErrorMessage error={limitError} /> : null}
      {loading && data === null ? <p>{t.ui.loading}</p> : null}
      {data !== null ? <ShopList shops={data} /> : null}
      {data !== null && data.length === 0 ? <AddShopForm onAdded={onAdded} /> : null}
    </main>
  );
}
