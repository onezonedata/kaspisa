"use client";

import { useRouter } from "next/navigation";
import { useLayoutEffect, useState, type FormEvent } from "react";

import { addShop, listShops, type ApiError } from "@/api/client";
import type { Shop } from "@/api/types";
import { t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

/**
 * `onAdded(null, limitError)` — магазин не создан (лимит): странице нужно перечитать список
 * и самой показать ошибку, потому что форма может исчезнуть.
 */
export function AddShopForm({
  onAdded,
}: {
  onAdded?: (shop: Shop | null, limitError?: ApiError) => void;
}) {
  const router = useRouter();
  const [login, setLogin] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  // Пароль Kaspi не должен пережить скрытие страницы (Activity) и размонтирование.
  useLayoutEffect(() => () => setSecret(""), []);

  // Магазин уже есть (двойное нажатие, старая вкладка): один магазин — открываем его,
  // иначе показываем ошибку и просим страницу перечитать список. addShop повторно не вызываем.
  async function onLimitReached(limitError: ApiError) {
    try {
      const shops = await listShops();
      if (shops.length === 1) {
        router.push(`/shops/${shops[0].id}`);
        return;
      }
    } catch {
      // Список не прочитан: ниже покажем ошибку лимита.
    }
    setError(limitError);
    onAdded?.(null, limitError);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const shop = await addShop(login.trim(), secret);
      setLogin("");
      onAdded?.(shop);
      router.push(`/shops/${shop.id}`);
    } catch (e) {
      const apiError = e as ApiError;
      if (apiError.code === "shop_limit_reached") {
        await onLimitReached(apiError);
      } else {
        setError(apiError);
      }
    } finally {
      setSecret("");
      setBusy(false);
    }
  }

  return (
    <form className="card" method="post" onSubmit={onSubmit}>
      <h2>{t.ui.addShopTitle}</h2>
      <p>{t.ui.addShopHint}</p>
      <label>
        {t.ui.kaspiLogin}
        <input
          name="kaspi-login"
          id="kaspi-login"
          autoComplete="off"
          required
          maxLength={254}
          value={login}
          onChange={(e) => setLogin(e.target.value)}
        />
      </label>
      <label>
        {t.ui.kaspiPassword}
        <input
          type="password"
          name="kaspi-secret"
          id="kaspi-secret"
          autoComplete="off"
          required
          maxLength={256}
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
        />
      </label>
      <ErrorMessage error={error} />
      <BusyButton type="submit" busy={busy} busyText={t.ui.addingShop}>
        {t.ui.addShop}
      </BusyButton>
    </form>
  );
}
