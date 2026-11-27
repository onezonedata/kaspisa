"use client";

import { useState } from "react";

import { readPrices, type ApiError } from "@/api/client";
import type { Product } from "@/api/types";
import { t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

/**
 * Разовое чтение цен карточки Kaspi: кнопка и ошибка. Результат не показывается:
 * ответ — обновлённый товар, его цифры выводит строка таблицы или карточка (ADR 0033).
 */
export function PriceRead({
  shopId,
  product,
  onRead,
}: {
  shopId: string;
  product: Product;
  onRead: (result: Product) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      onRead(await readPrices(shopId, product.id));
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="actions">
      <BusyButton type="button" busy={busy} busyText={t.ui.priceReading} onClick={() => void run()}>
        {t.ui.priceRead}
      </BusyButton>
      <ErrorMessage error={error} />
    </div>
  );
}
