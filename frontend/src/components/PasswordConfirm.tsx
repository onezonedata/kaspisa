"use client";

import { useState, type FormEvent } from "react";

import { confirmPassword, type ApiError } from "@/api/client";
import { t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

/**
 * Форма повторного пароля (ADR 0024): показывается вместо ошибки `password_required`.
 * После успешного подтверждения вызывает `onConfirmed` один раз; повтор исходной правки — дело родителя.
 * Пароль живёт только в поле: после запроса поле очищается, в адрес и журнал он не попадает.
 */
export function PasswordConfirm({
  onConfirmed,
  onCancel,
}: {
  onConfirmed: () => void;
  onCancel: () => void;
}) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (password === "") return;
    setBusy(true);
    setError(null);
    try {
      await confirmPassword(password);
      setPassword("");
      onConfirmed();
    } catch (e) {
      setPassword("");
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="banner">
      <h3>{t.ui.passwordTitle}</h3>
      <p>{t.ui.passwordHint}</p>
      <form method="post" onSubmit={onSubmit}>
        <label>
          {t.ui.password}
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <div className="actions">
          <BusyButton type="submit" busy={busy} busyText={t.ui.passwordConfirming}>
            {t.ui.passwordConfirm}
          </BusyButton>
          <button type="button" disabled={busy} onClick={onCancel}>
            {t.ui.passwordCancel}
          </button>
        </div>
      </form>
      <ErrorMessage error={error} />
      {error?.code === "too_many_attempts" ? <p>{t.ui.passwordLockedHint}</p> : null}
    </section>
  );
}
