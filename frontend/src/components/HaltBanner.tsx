"use client";

import { useState } from "react";

import { clearHalt, type ApiError } from "@/api/client";
import type { Shop } from "@/api/types";
import { codeText, t } from "@/i18n";
import { formatDateTime } from "@/lib/format";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

/** Остановка бота показывается независимо от состояния подключения. */
export function HaltBanner({ shop, onShop }: { shop: Shop; onShop: (shop: Shop) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  if (shop.halt_reason === null) return null;

  async function clear() {
    setBusy(true);
    setError(null);
    try {
      onShop(await clearHalt(shop.id));
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="banner tone-danger">
      <p role="status">{`${t.ui.haltedPrefix}: ${codeText(t.haltReason, shop.halt_reason)}`}</p>
      {shop.halted_at !== null ? (
        <p>
          {t.ui.haltedSince} {formatDateTime(shop.halted_at)}
        </p>
      ) : null}
      {shop.halt_reason === "login_errors" ? <p>{t.ui.haltLoginHint}</p> : null}
      <ErrorMessage error={error} />
      <BusyButton type="button" busy={busy} busyText={t.ui.clearingHalt} onClick={clear}>
        {t.ui.clearHalt}
      </BusyButton>
    </section>
  );
}
