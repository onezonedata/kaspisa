"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { ApiError, register } from "@/api/client";
import { t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

const PASSWORD_MIN = 10;

export function RegisterForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [passwordError, setPasswordError] = useState(false);
  const [closed, setClosed] = useState(false);

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
      await register(email, password);
      router.replace("/shops");
    } catch (e) {
      if ((e as ApiError).code === "registration_closed") setClosed(true);
      else setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  if (closed) {
    return (
      <div>
        <h1>{t.ui.registerTitle}</h1>
        <p role="status">{t.ui.registrationClosed}</p>
        <p>
          <Link href="/login">{t.ui.toLogin}</Link>
        </p>
      </div>
    );
  }

  return (
    <form method="post" onSubmit={onSubmit}>
      <h1>{t.ui.registerTitle}</h1>
      <label>
        {t.ui.email}
        <input
          type="email"
          name="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        {t.ui.password}
        <input
          type="password"
          name="password"
          autoComplete="new-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      {passwordError && <p className="error">{t.ui.passwordTooShort}</p>}
      <ErrorMessage error={error} />
      <BusyButton type="submit" busy={busy} busyText={t.ui.registering}>
        {t.ui.registerAction}
      </BusyButton>
      <p>
        <Link href="/login">{t.ui.toLogin}</Link>
      </p>
    </form>
  );
}
