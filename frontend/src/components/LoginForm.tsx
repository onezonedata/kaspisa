"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { ApiError, login } from "@/api/client";
import { t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

export function LoginForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      router.replace("/shops");
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form method="post" onSubmit={onSubmit}>
      <h1>{t.ui.loginTitle}</h1>
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
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <ErrorMessage error={error} />
      <BusyButton type="submit" busy={busy} busyText={t.ui.signingIn}>
        {t.ui.signIn}
      </BusyButton>
      <p>
        <Link href="/register">{t.ui.toRegister}</Link>
      </p>
      <p>
        {t.ui.forgotPassword}. <Link href="/reset-password">{t.ui.haveResetCode}</Link>
      </p>
    </form>
  );
}
