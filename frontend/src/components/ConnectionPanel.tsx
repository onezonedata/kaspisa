"use client";

import { useEffect, useLayoutEffect, useState, type FormEvent } from "react";

import {
  getShop,
  resendCode,
  restartLogin,
  startLogin,
  submitCode,
  type ApiError,
} from "@/api/client";
import type { ConnectResult, Shop } from "@/api/types";
import { codeText, fill, formatDuration, t } from "@/i18n";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

type Action = "start" | "restart" | "code" | "resend";

/** Сообщение по исходу запроса подключения. */
function outcomeText(result: ConnectResult): string {
  if (result.outcome === "rejected") {
    const code = result.shop.connection_error;
    return code === null ? t.connectOutcome.rejected : codeText(t.connectionError, code);
  }
  if (result.outcome === "temporary_error" && result.retry_after_seconds !== null) {
    return `${t.connectOutcome.temporary_error}. ${t.common.retryPrefix} ${formatDuration(result.retry_after_seconds)}`;
  }
  return t.connectOutcome[result.outcome];
}

// Обрыв связи и устаревшее состояние лечатся одним перечитыванием магазина.
const REREAD_CODES = new Set(["network_error", "unexpected_error", "restart_required", "state_changed"]);

export function ConnectionPanel({ shop, onShop }: { shop: Shop; onShop: (shop: Shop) => void }) {
  const [busy, setBusy] = useState<Action | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [codeInvalid, setCodeInvalid] = useState(false);
  // Отсчёт до следующей отправки письма; сбрасывается при каждом новом объекте магазина.
  const [seen, setSeen] = useState(shop);
  const [left, setLeft] = useState(shop.resend_available_in_seconds ?? 0);
  const [restartBlocked, setRestartBlocked] = useState(false);
  if (seen !== shop) {
    setSeen(shop);
    setLeft(shop.resend_available_in_seconds ?? 0);
    setRestartBlocked(false);
  }

  const counting = left > 0;
  useEffect(() => {
    if (!counting) return;
    const timer = setInterval(() => setLeft((value) => Math.max(0, value - 1)), 1000);
    return () => clearInterval(timer);
  }, [counting]);

  // Сообщения и код не переживают скрытие страницы (Activity).
  useLayoutEffect(
    () => () => {
      setError(null);
      setMessage(null);
      setCode("");
    },
    [],
  );

  async function recover(action: Action, e: ApiError) {
    if (REREAD_CODES.has(e.code)) {
      try {
        onShop(await getShop(shop.id));
        setCode("");
        if (e.code === "state_changed") setMessage(t.errors.state_changed);
        else if (e.code !== "restart_required") setMessage(t.ui.connectionInterrupted);
        return;
      } catch {
        setError(e);
        return;
      }
    }
    if (e.code === "resend_too_early" && e.retryAfterSeconds !== null) {
      setLeft(e.retryAfterSeconds);
      if (action === "restart") setRestartBlocked(true);
    }
    setError(e);
  }

  async function run(action: Action, call: () => Promise<ConnectResult>) {
    setBusy(action);
    setError(null);
    setMessage(null);
    setCodeInvalid(false);
    try {
      const result = await call();
      onShop(result.shop);
      // При restart_required подсказка уже есть на панели, второй раз её не повторяем.
      setMessage(result.outcome === "restart_required" ? null : outcomeText(result));
      setCode("");
    } catch (e) {
      await recover(action, e as ApiError);
    } finally {
      setBusy(null);
    }
  }

  function onSubmitCode(event: FormEvent) {
    event.preventDefault();
    const value = code.trim();
    if (!/^[0-9]{4,12}$/.test(value)) {
      setCodeInvalid(true);
      return;
    }
    void run("code", () => submitCode(shop.id, value));
  }

  const idle = busy === null;
  const restartWait = restartBlocked && left > 0;
  const restartButton = (
    <BusyButton
      type="button"
      busy={busy === "restart"}
      busyText={t.ui.connecting}
      disabled={!idle || restartWait}
      onClick={() => void run("restart", () => restartLogin(shop.id))}
    >
      {restartWait ? fill(t.ui.restartIn, { n: left }) : t.ui.restartLogin}
    </BusyButton>
  );

  const state = shop.connection_state;
  return (
    <section className="card">
      <p>
        <strong>{codeText(t.connectionState, state)}</strong>
        <span> {fill(t.ui.botModeStatus, { mode: t.botMode[shop.bot_mode] })}</span>
      </p>
      {state === "not_connected" ? <p>{t.ui.connectHint}</p> : null}

      {state === "reauth_required" && shop.connection_error !== null ? (
        <p>{codeText(t.connectionError, shop.connection_error)}</p>
      ) : null}

      {state === "not_connected" || state === "reauth_required" ? (
        <BusyButton
          type="button"
          busy={busy === "start"}
          busyText={t.ui.connecting}
          disabled={!idle}
          onClick={() => void run("start", () => startLogin(shop.id))}
        >
          {t.ui.connect}
        </BusyButton>
      ) : null}

      {state === "awaiting_code" && shop.restart_required ? (
        <>
          <p>{t.ui.restartHint}</p>
          {restartButton}
        </>
      ) : null}

      {state === "awaiting_code" && !shop.restart_required ? (
        <>
          <p>{t.ui.awaitingCodeHint}</p>
          <form method="post" onSubmit={onSubmitCode}>
            <label>
              {t.ui.code}
              <input
                name="code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </label>
            {codeInvalid ? <p role="alert">{t.ui.codeFormat}</p> : null}
            <BusyButton type="submit" busy={busy === "code"} busyText={t.ui.connecting} disabled={!idle}>
              {t.ui.sendCode}
            </BusyButton>
          </form>
          <div className="actions">
            <BusyButton
              type="button"
              busy={busy === "resend"}
              busyText={t.ui.connecting}
              disabled={!idle || left > 0}
              onClick={() => void run("resend", () => resendCode(shop.id))}
            >
              {left > 0 ? fill(t.ui.resendIn, { n: left }) : t.ui.resendCode}
            </BusyButton>
            {restartButton}
          </div>
        </>
      ) : null}

      {message !== null ? <p role="status">{message}</p> : null}
      <ErrorMessage error={error} />
    </section>
  );
}
