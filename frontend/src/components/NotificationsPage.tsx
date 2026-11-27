"use client";

import { useLayoutEffect, useState, type FormEvent } from "react";

import {
  deleteTelegram,
  getTelegram,
  saveTelegram,
  testTelegram,
  type ApiError,
} from "@/api/client";
import type { TelegramSettings, TelegramSettingsIn } from "@/api/types";
import { fill, t } from "@/i18n";
import { useLoad } from "@/lib/use-load";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";
import { PasswordConfirm } from "./PasswordConfirm";

/** Действие, отложенное до повторного ввода пароля (ADR 0024). */
type Pending = { kind: "save"; body: TelegramSettingsIn } | { kind: "delete" };
type Busy = "save" | "delete" | "test" | null;

function TelegramForm({
  settings,
  onSettings,
}: {
  settings: TelegramSettings;
  onSettings: (settings: TelegramSettings) => void;
}) {
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState(settings.chat_id ?? "");
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [sent, setSent] = useState(false);
  const [ask, setAsk] = useState<Pending | null>(null);

  // Токен не должен пережить скрытие страницы (Activity) и размонтирование.
  useLayoutEffect(
    () => () => {
      setToken("");
      setSent(false);
      setError(null);
      setAsk(null);
    },
    [],
  );

  /** Выполняет действие; `password_required` не ошибка, а просьба ввести пароль. */
  async function run(action: Pending) {
    setBusy(action.kind);
    setError(null);
    setSent(false);
    setAsk(null);
    try {
      if (action.kind === "save") {
        onSettings(await saveTelegram(action.body));
      } else {
        await deleteTelegram();
        setChatId("");
        onSettings({ token_set: false, chat_id: null });
      }
    } catch (e) {
      const apiError = e as ApiError;
      if (apiError.code === "password_required") setAsk(action);
      else setError(apiError);
    } finally {
      // Токен очищается при любом исходе.
      setToken("");
      setBusy(null);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const body: TelegramSettingsIn = { chat_id: chatId.trim() };
    if (token !== "") body.token = token;
    void run({ kind: "save", body });
  }

  async function onTest() {
    setBusy("test");
    setError(null);
    setSent(false);
    try {
      await testTelegram();
      setSent(true);
    } catch (e) {
      setError(e as ApiError);
    } finally {
      setBusy(null);
    }
  }

  /** После подтверждения пароля исходное действие повторяется один раз. */
  function onConfirmed() {
    if (ask !== null) void run(ask);
  }

  const configured = settings.token_set;
  const working = busy !== null;
  return (
    <section className="card">
      <p>
        {configured
          ? fill(t.ui.tgStatusOn, { chat_id: settings.chat_id ?? "" })
          : t.ui.tgStatusOff}
      </p>
      <form method="post" onSubmit={onSubmit}>
        <label>
          {t.ui.tgTokenLabel}
          <input
            type="password"
            name="telegram-token"
            id="telegram-token"
            autoComplete="new-password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        </label>
        {configured ? <small>{t.ui.tgTokenKeepHint}</small> : null}
        <label>
          {t.ui.tgChatLabel}
          <input
            name="telegram-chat"
            id="telegram-chat"
            autoComplete="off"
            value={chatId}
            onChange={(e) => setChatId(e.target.value)}
          />
        </label>
        <BusyButton type="submit" busy={busy === "save"} busyText={t.ui.saving} disabled={working}>
          {t.ui.save}
        </BusyButton>
        {configured ? <small>{t.ui.tgOldChatHint}</small> : null}
      </form>
      <div className="actions">
        <BusyButton
          type="button"
          busy={busy === "test"}
          busyText={t.ui.tgTesting}
          disabled={!configured || working}
          onClick={() => void onTest()}
        >
          {t.ui.tgTest}
        </BusyButton>
        {configured ? (
          <BusyButton
            type="button"
            busy={busy === "delete"}
            busyText={t.ui.tgDisabling}
            disabled={working}
            onClick={() => void run({ kind: "delete" })}
          >
            {t.ui.tgDisable}
          </BusyButton>
        ) : null}
      </div>
      <ErrorMessage error={error} />
      {sent ? <p role="status">{t.ui.tgTestSent}</p> : null}
      {ask !== null ? (
        <PasswordConfirm onConfirmed={onConfirmed} onCancel={() => setAsk(null)} />
      ) : null}
    </section>
  );
}

/** Настройка уведомлений в Telegram: токен своего бота и chat id (ADR 0025). */
export function NotificationsPage() {
  const { data, error, loading, setData } = useLoad(getTelegram);
  return (
    <main className="page-narrow">
      <h1>{t.ui.notificationsTitle}</h1>
      <ol className="card steps">
        <li>{t.ui.tgStep1}</li>
        <li>{t.ui.tgStep2}</li>
        <li>{t.ui.tgStep3}</li>
      </ol>
      <ErrorMessage error={error} />
      {data === null && loading ? <p>{t.ui.loading}</p> : null}
      {data !== null ? <TelegramForm settings={data} onSettings={setData} /> : null}
    </main>
  );
}
