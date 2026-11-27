"use client";

import { useLayoutEffect, useState, type FormEvent } from "react";

import { setCredentials, type ApiError } from "@/api/client";
import type { Shop } from "@/api/types";
import { t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

export function CredentialsForm({ shop, onShop }: { shop: Shop; onShop: (shop: Shop) => void }) {
  const [open, setOpen] = useState(false);
  const [login, setLogin] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  // Пароль Kaspi не должен пережить скрытие страницы (Activity) и размонтирование.
  useLayoutEffect(
    () => () => {
      setSecret("");
      setSaved(false);
      setError(null);
    },
    [],
  );

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = login.trim();
    setError(null);
    setSaved(false);
    setInvalid(false);
    if (trimmed.length < 1 || trimmed.length > 254 || secret.length < 1 || secret.length > 256) {
      setInvalid(true);
      return;
    }
    setBusy(true);
    try {
      onShop(await setCredentials(shop.id, trimmed, secret));
      setLogin("");
      setSaved(true);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setSecret("");
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <section className="card">
        <button type="button" onClick={() => setOpen(true)}>
          {t.ui.changeCredentials}
        </button>
      </section>
    );
  }

  return (
    <section className="card">
      <form method="post" onSubmit={onSubmit}>
        <h2>{t.ui.changeCredentials}</h2>
        <label>
          {t.ui.kaspiLogin}
          <input
            name="kaspi-login"
            id="kaspi-login"
            autoComplete="off"
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
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
          />
        </label>
        <small>{t.ui.credentialsHint}</small>
        {invalid ? <p role="alert">{t.ui.credentialsInvalid}</p> : null}
        <ErrorMessage error={error} />
        {saved ? <p role="status">{t.ui.credentialsSaved}</p> : null}
        <BusyButton type="submit" busy={busy} busyText={t.ui.credentialsSaving}>
          {t.ui.credentialsSave}
        </BusyButton>
      </form>
    </section>
  );
}
