"use client";

import { useLayoutEffect, useState } from "react";

import { getShop, syncCatalog, type ApiError } from "@/api/client";
import type { Shop, SyncResult } from "@/api/types";
import { codeText, fill, formatDuration, t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

// Обрыв связи: загрузка могла завершиться на сервере, поэтому перечитываем магазин.
const INTERRUPTED = new Set(["network_error", "unexpected_error"]);

function resultText(result: SyncResult): string {
  if (result.outcome === "synced") {
    return fill(t.ui.syncDone, {
      added: result.added,
      updated: result.updated,
      missing: result.missing,
    });
  }
  if (result.outcome === "rejected") {
    const code = result.shop.connection_error;
    return code === null ? t.syncOutcome.rejected : codeText(t.connectionError, code);
  }
  const base = t.syncOutcome.temporary_error;
  return result.retry_after_seconds === null
    ? base
    : `${base}. ${t.common.retryPrefix} ${formatDuration(result.retry_after_seconds)}`;
}

export function CatalogSync({ shop, onShop }: { shop: Shop; onShop: (shop: Shop) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  // Итог не переживает скрытие страницы (Activity).
  useLayoutEffect(
    () => () => {
      setError(null);
      setMessage(null);
    },
    [],
  );

  async function run() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await syncCatalog(shop.id);
      onShop(result.shop);
      setMessage(resultText(result));
    } catch (e) {
      const apiError = e as ApiError;
      if (apiError.code === "sync_in_progress") {
        // Загрузка уже идёт (обработчик или другая вкладка) — это состояние, а не ошибка:
        // статус «Каталог загружается» выводит сводка по актуальному catalog_loading.
        try {
          onShop(await getShop(shop.id));
        } catch {
          setError(apiError);
        }
      } else if (INTERRUPTED.has(apiError.code)) {
        try {
          onShop(await getShop(shop.id));
          setMessage(t.ui.syncInterrupted);
        } catch {
          setError(apiError);
        }
      } else {
        setError(apiError);
      }
    } finally {
      setBusy(false);
    }
  }

  const connected = shop.connection_state === "connected";
  if (!connected && message === null && error === null) return null;
  return (
    <section>
      {connected ? (
        <>
          <BusyButton
            type="button"
            busy={busy}
            busyText={t.ui.syncing}
            disabled={shop.catalog_loading}
            onClick={() => void run()}
          >
            {t.ui.syncCatalog}
          </BusyButton>
          <p>
            <small>{t.ui.syncHint}</small>
          </p>
        </>
      ) : null}
      {message !== null ? <p role="status">{message}</p> : null}
      <ErrorMessage error={error} />
    </section>
  );
}
