"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";

import { ApiError, resetPassword } from "@/api/client";
import { t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

const PASSWORD_MIN = 10;

export function ResetPasswordForm() {
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [passwordError, setPasswordError] = useState(false);

  // Токен приходит во фрагменте адреса: он не уходит на сервер. Читается один раз и стирается из адреса;
  // повторный запуск эффекта (StrictMode) фрагмента уже не находит и поле не трогает.
  useEffect(() => {
    const match = /^#token=(.+)$/.exec(window.location.hash);
    if (match === null) return;
    let value = match[1];
    try {
      value = decodeURIComponent(value);
    } catch {
      // оставляем как есть
    }
    // Синхронизация с внешним состоянием (адресная строка), а не производное значение.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setToken(value);
    window.history.replaceState(null, "", window.location.pathname);
  }, []);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (password.length < PASSWORD_MIN) {
      setPasswordError(true);
      return;
    }
    setPasswordError(false);
    setBusy(true);
    try {
      await resetPassword(token.trim(), password);
      setPassword("");
      setToken("");
      setDone(true);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div>
        <p role="status">{t.ui.passwordReset}</p>
        <Link href="/login">{t.ui.toLogin}</Link>
      </div>
    );
  }

  return (
    <form method="post" onSubmit={onSubmit}>
      <h1>{t.ui.resetTitle}</h1>
      <label>
        {t.ui.resetToken}
        <input
          name="token"
          autoComplete="one-time-code"
          required
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
      </label>
      <label>
        {t.ui.newPassword}
        <input
          type="password"
          name="new-password"
          autoComplete="new-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      {passwordError && <p className="error">{t.ui.passwordTooShort}</p>}
      <ErrorMessage error={error} />
      <BusyButton type="submit" busy={busy} busyText={t.ui.resetting}>
        {t.ui.resetAction}
      </BusyButton>
      <p>
        <Link href="/login">{t.ui.toLogin}</Link>
      </p>
    </form>
  );
}
