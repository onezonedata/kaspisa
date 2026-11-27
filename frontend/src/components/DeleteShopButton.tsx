"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { deleteShop, type ApiError } from "@/api/client";
import { t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

export function DeleteShopButton({ shopId }: { shopId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function onClick() {
    if (!window.confirm(t.ui.confirmDeleteShop)) return;
    setBusy(true);
    setError(null);
    try {
      await deleteShop(shopId);
      router.push("/shops");
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <ErrorMessage error={error} />
      <BusyButton className="btn-danger" type="button" busy={busy} busyText={t.ui.deleting} onClick={onClick}>
        {t.ui.deleteShop}
      </BusyButton>
    </section>
  );
}
