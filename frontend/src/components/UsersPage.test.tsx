import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UserAdmin } from "@/api/types";
import { fill, t } from "@/i18n";
import { emptyResponse, jsonResponse, sentBody, stubFetch } from "@/test/http";
import { UsersPage } from "./UsersPage";

const ADMIN: UserAdmin = {
  id: "00000000-0000-4000-8000-00000000000a",
  email: "boss@example.kz",
  is_admin: true,
  created_at: "2026-11-01T10:00:00Z",
};
const STAFF: UserAdmin = {
  id: "00000000-0000-4000-8000-00000000000b",
  email: "staff@example.kz",
  is_admin: false,
  created_at: "2026-11-02T10:00:00Z",
};
const NEW: UserAdmin = {
  id: "00000000-0000-4000-8000-00000000000c",
  email: "new@example.kz",
  is_admin: false,
  created_at: "2026-11-27T10:00:00Z",
};
// Вымышленный токен собран из частей, чтобы gitleaks при сборке зеркала не принял его за секрет.
const TOKEN = "t-" + "x".repeat(12) + "_y";

let fetchMock: ReturnType<typeof stubFetch>;
let users: UserAdmin[];
/** Ответы на изменяющие запросы к /api/users по очереди; пустая очередь — успех. */
let queue: Response[];

beforeEach(() => {
  fetchMock = stubFetch();
  users = [ADMIN, STAFF];
  queue = [];
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/auth/confirm-password") return Promise.resolve(emptyResponse(204));
    if (method === "GET") return Promise.resolve(jsonResponse(200, users));
    const next = queue.shift();
    if (next !== undefined) return Promise.resolve(next);
    if (method === "POST") {
      users = [...users, NEW];
      return Promise.resolve(
        jsonResponse(201, { user: NEW, reset_token: TOKEN, expires_in_seconds: 3600 }),
      );
    }
    const id = url.split("/").pop();
    users = users.filter((u) => u.id !== id);
    return Promise.resolve(emptyResponse(204));
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

const row = (email: string) => screen.getByRole("row", { name: new RegExp(email) });
async function loaded() {
  render(<UsersPage />);
  await screen.findByText(ADMIN.email);
}
function enterPassword() {
  fireEvent.change(screen.getByLabelText(t.ui.password), { target: { value: "secret-pass-1" } });
  fireEvent.click(screen.getByRole("button", { name: t.ui.passwordConfirm }));
}
const writes = () =>
  fetchMock.mock.calls.filter((c) => ((c[1] as RequestInit | undefined)?.method ?? "GET") !== "GET");

describe("UsersPage: список (Т30)", () => {
  it("администратор с отметкой и без «Удалить», обычный — с кнопкой", async () => {
    await loaded();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/users");
    expect(within(row(ADMIN.email)).getByText(t.ui.userAdminBadge)).toBeInTheDocument();
    expect(within(row(ADMIN.email)).queryByRole("button", { name: t.ui.userDelete })).toBeNull();
    expect(within(row(STAFF.email)).queryByText(t.ui.userAdminBadge)).toBeNull();
    expect(within(row(STAFF.email)).getByRole("button", { name: t.ui.userDelete })).toBeInTheDocument();
  });
});

describe("UsersPage: добавление (Т31)", () => {
  it("password_required → пароль → повтор → ссылка один раз, нигде не хранится", async () => {
    queue = [jsonResponse(403, { code: "password_required" })];
    await loaded();
    fireEvent.change(screen.getByLabelText(t.ui.email), { target: { value: NEW.email } });
    fireEvent.click(screen.getByRole("button", { name: t.ui.userAdd }));
    await screen.findByText(t.ui.passwordTitle);
    enterPassword();

    const link = (await screen.findByLabelText(t.ui.userLinkTitle)) as HTMLInputElement;
    expect(link.value).toBe(`${window.location.origin}/reset-password#token=${TOKEN}`);
    expect(link).toHaveAttribute("readonly");
    expect(screen.getByText(t.ui.userLinkHint)).toBeInTheDocument();
    expect(await screen.findByText(NEW.email)).toBeInTheDocument();
    const posts = writes().filter((c) => c[0] === "/api/users");
    expect(posts).toHaveLength(2);
    expect(sentBody(fetchMock, fetchMock.mock.calls.indexOf(posts[1]))).toEqual({ email: NEW.email });
    expect(JSON.stringify({ ...localStorage })).not.toContain(TOKEN);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(TOKEN);

    fireEvent.click(screen.getByRole("button", { name: t.ui.userLinkDone }));
    expect(screen.queryByLabelText(t.ui.userLinkTitle)).toBeNull();
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("e-mail занят — текст ошибки, ссылки нет", async () => {
    queue = [jsonResponse(409, { code: "email_taken" })];
    await loaded();
    fireEvent.change(screen.getByLabelText(t.ui.email), { target: { value: STAFF.email } });
    fireEvent.click(screen.getByRole("button", { name: t.ui.userAdd }));
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.email_taken);
    expect(screen.queryByLabelText(t.ui.userLinkTitle)).toBeNull();
  });
});

describe("UsersPage: удаление (Т32)", () => {
  it("подтверждение → пароль → повтор → строка исчезает", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    queue = [jsonResponse(403, { code: "password_required" })];
    await loaded();
    fireEvent.click(within(row(STAFF.email)).getByRole("button", { name: t.ui.userDelete }));
    expect(confirm).toHaveBeenCalledWith(fill(t.ui.userConfirmDelete, { email: STAFF.email }));
    await screen.findByText(t.ui.passwordTitle);
    enterPassword();
    await vi.waitFor(() => expect(screen.queryByText(STAFF.email)).toBeNull());
    const deletes = writes().filter((c) => (c[1] as RequestInit).method === "DELETE");
    expect(deletes).toHaveLength(2);
    expect(deletes[0][0]).toBe(`/api/users/${STAFF.id}`);
  });

  it("отказ в подтверждении — запроса нет", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await loaded();
    fireEvent.click(within(row(STAFF.email)).getByRole("button", { name: t.ui.userDelete }));
    expect(writes()).toHaveLength(0);
  });

  it("409 cannot_delete_self — текст ошибки, строка на месте", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    queue = [jsonResponse(409, { code: "cannot_delete_self" })];
    await loaded();
    fireEvent.click(within(row(STAFF.email)).getByRole("button", { name: t.ui.userDelete }));
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.cannot_delete_self);
    expect(screen.getByText(STAFF.email)).toBeInTheDocument();
  });

  it("404 при удалении — ошибка и список перезагружен: строки удалённого нет", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    queue = [jsonResponse(404, { code: "not_found" })];
    await loaded();
    users = [ADMIN]; // пользователя уже удалили (другая вкладка)
    fireEvent.click(within(row(STAFF.email)).getByRole("button", { name: t.ui.userDelete }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await vi.waitFor(() => expect(screen.queryByText(STAFF.email)).toBeNull());
  });
});

describe("UsersPage: права (Т33)", () => {
  it("403 admin_required на загрузку — «Страница доступна только администратору»", async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { code: "admin_required" }));
    render(<UsersPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Страница доступна только администратору",
    );
    expect(screen.queryByLabelText(t.ui.email)).toBeNull();
  });
});
