"use client";

import { useState, type FormEvent } from "react";

import { addUser, deleteUser, listUsers, type ApiError } from "@/api/client";
import type { UserAdmin } from "@/api/types";
import { fill, t } from "@/i18n";
import { formatDateTime } from "@/lib/format";
import { useLoad } from "@/lib/use-load";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";
import { PasswordConfirm } from "./PasswordConfirm";

/** Действие, отложенное до повторного ввода пароля (ADR 0024). */
type Pending = { kind: "add"; email: string } | { kind: "delete"; user: UserAdmin };

/**
 * Пользователи установки (только администратор, ADR 0039).
 * Ссылка для задания пароля живёт лишь в состоянии компонента: ни в хранилище, ни в адресе, ни в логах.
 */
export function UsersPage() {
  const { data: users, error: loadError, loading, reload } = useLoad(listUsers);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState<"add" | string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [ask, setAsk] = useState<Pending | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  /** Выполняет действие; `password_required` не ошибка, а просьба ввести пароль. */
  async function run(action: Pending) {
    setBusy(action.kind === "add" ? "add" : action.user.id);
    setError(null);
    setAsk(null);
    try {
      if (action.kind === "add") {
        const added = await addUser(action.email);
        setLink(`${window.location.origin}/reset-password#token=${added.reset_token}`);
        setCopied(false);
        setEmail("");
      } else {
        await deleteUser(action.user.id);
      }
      reload();
    } catch (e) {
      const apiError = e as ApiError;
      if (apiError.code === "password_required") setAsk(action);
      else {
        setError(apiError);
        // Удаление могло пройти частично (500 после commit) или уже быть сделанным (404).
        if (action.kind === "delete") reload();
      }
    } finally {
      setBusy(null);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const value = email.trim();
    if (value === "") return;
    void run({ kind: "add", email: value });
  }

  function onDelete(user: UserAdmin) {
    if (!window.confirm(fill(t.ui.userConfirmDelete, { email: user.email }))) return;
    void run({ kind: "delete", user });
  }

  async function onCopy() {
    if (link === null) return;
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      // буфер недоступен: ссылку можно выделить в поле
    }
  }

  return (
    <main className="page-narrow">
      <h1>{t.ui.usersTitle}</h1>
      <ErrorMessage error={loadError} />
      {users === null && loading ? <p>{t.ui.loading}</p> : null}
      {users !== null ? (
        <>
          <section className="card">
            <h2>{t.ui.userAddTitle}</h2>
            <form method="post" onSubmit={onSubmit}>
              <label>
                {t.ui.email}
                <input
                  type="email"
                  name="new-user-email"
                  autoComplete="off"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              <BusyButton type="submit" busy={busy === "add"} busyText={t.ui.userAdding} disabled={busy !== null}>
                {t.ui.userAdd}
              </BusyButton>
            </form>
          </section>
          {link !== null ? (
            <section className="banner">
              <label>
                {t.ui.userLinkTitle}
                <input readOnly value={link} onFocus={(e) => e.currentTarget.select()} />
              </label>
              <p>{t.ui.userLinkHint}</p>
              <div className="actions">
                <button type="button" onClick={() => void onCopy()}>
                  {copied ? t.ui.userLinkCopied : t.ui.userLinkCopy}
                </button>
                <button type="button" onClick={() => setLink(null)}>
                  {t.ui.userLinkDone}
                </button>
              </div>
            </section>
          ) : null}
          <ErrorMessage error={error} />
          {ask !== null ? (
            <PasswordConfirm onConfirmed={() => void run(ask)} onCancel={() => setAsk(null)} />
          ) : null}
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{t.ui.colUserEmail}</th>
                  <th>{t.ui.colUserRole}</th>
                  <th>{t.ui.colUserCreated}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.id}>
                    <td className="wrap">{user.email}</td>
                    <td>{user.is_admin ? t.ui.userAdminBadge : null}</td>
                    <td>{formatDateTime(user.created_at)}</td>
                    <td>
                      {user.is_admin ? null : (
                        <BusyButton
                          type="button"
                          busy={busy === user.id}
                          busyText={t.ui.userDeleting}
                          disabled={busy !== null}
                          onClick={() => onDelete(user)}
                        >
                          {t.ui.userDelete}
                        </BusyButton>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </main>
  );
}
