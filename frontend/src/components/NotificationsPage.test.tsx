import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Activity } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fill, t } from "@/i18n";
import { emptyResponse, jsonResponse, sentBody, stubFetch } from "@/test/http";
import { NotificationsPage } from "./NotificationsPage";

const URL = "/api/notifications/telegram";
const TOKEN = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123";

let fetchMock: ReturnType<typeof stubFetch>;
let settings: { token_set: boolean; chat_id: string | null };
/** Ответы на изменяющие запросы по очереди; пустая очередь — успех. */
let queue: Response[];

beforeEach(() => {
  fetchMock = stubFetch();
  settings = { token_set: false, chat_id: null };
  queue = [];
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/auth/confirm-password") return Promise.resolve(emptyResponse(204));
    if (method === "GET") return Promise.resolve(jsonResponse(200, settings));
    const next = queue.shift();
    if (next !== undefined) return Promise.resolve(next);
    if (method === "PUT") {
      const body = JSON.parse(init?.body as string) as { chat_id: string };
      return Promise.resolve(jsonResponse(200, { token_set: true, chat_id: body.chat_id }));
    }
    return Promise.resolve(emptyResponse(204));
  });
});
afterEach(() => vi.unstubAllGlobals());

/** Вызовы изменяющих запросов к точке настроек: [метод, адрес]. */
function writes(): Array<[string, string]> {
  return fetchMock.mock.calls
    .map((c) => [(c[1] as RequestInit | undefined)?.method ?? "GET", c[0] as string] as [string, string])
    .filter(([method, url]) => method !== "GET" && url.startsWith(URL));
}
function callIndexes(method: string): number[] {
  return fetchMock.mock.calls.flatMap((c, i) =>
    ((c[1] as RequestInit | undefined)?.method ?? "GET") === method && String(c[0]).startsWith(URL) ? [i] : [],
  );
}

async function loaded(initial = settings) {
  settings = initial;
  render(<NotificationsPage />);
  await screen.findByLabelText(t.ui.tgTokenLabel);
  await waitFor(() => expect(screen.queryByText(t.ui.loading)).not.toBeInTheDocument());
}
const tokenField = () => screen.getByLabelText(t.ui.tgTokenLabel);
const chatField = () => screen.getByLabelText(t.ui.tgChatLabel);
const button = (name: string) => screen.getByRole("button", { name });

describe("NotificationsPage: атрибуты полей против автозаполнения (Т41)", () => {
  it("токен — new-password; chat id — без inputmode, off, имя и id telegram-chat", async () => {
    await loaded();
    const token = tokenField();
    expect(token).toHaveAttribute("type", "password");
    expect(token).toHaveAttribute("autocomplete", "new-password");
    const chat = chatField();
    expect(chat).not.toHaveAttribute("inputmode");
    expect(chat).toHaveAttribute("autocomplete", "off");
    expect(chat).toHaveAttribute("name", "telegram-chat");
    expect(chat).toHaveAttribute("id", "telegram-chat");
    const loginLike = /(^|[-_])(id|login|user|username|e?mail)$/i;
    expect(chat.getAttribute("name")).not.toMatch(loginLike);
    expect(chat.getAttribute("id")).not.toMatch(loginLike);
  });
});

describe("NotificationsPage: состояния (Т38)", () => {
  it("не настроено: поля пусты, «Проверить» недоступна", async () => {
    await loaded();
    expect(screen.getByText(t.ui.tgStatusOff)).toBeInTheDocument();
    expect(tokenField()).toHaveValue("");
    expect(chatField()).toHaveValue("");
    expect(button(t.ui.tgTest)).toBeDisabled();
  });

  it("настроено: токен не показан, chat id заполнен, «Проверить» доступна, есть подсказка", async () => {
    await loaded({ token_set: true, chat_id: "42" });
    expect(screen.getByText(fill(t.ui.tgStatusOn, { chat_id: "42" }))).toBeInTheDocument();
    expect(tokenField()).toHaveValue("");
    expect(chatField()).toHaveValue("42");
    expect(button(t.ui.tgTest)).toBeEnabled();
    expect(screen.getByText(t.ui.tgTokenKeepHint)).toBeInTheDocument();
    expect(screen.getByText(t.ui.tgOldChatHint)).toBeInTheDocument();
  });
});

describe("NotificationsPage: сохранение (Т39)", () => {
  it("отправляет токен и chat id; после 200 поле токена пусто", async () => {
    await loaded();
    fireEvent.change(tokenField(), { target: { value: TOKEN } });
    fireEvent.change(chatField(), { target: { value: "42" } });
    fireEvent.click(button(t.ui.save));
    await screen.findByText(fill(t.ui.tgStatusOn, { chat_id: "42" }));
    const [put] = callIndexes("PUT");
    expect(sentBody(fetchMock, put)).toEqual({ token: TOKEN, chat_id: "42" });
    expect(tokenField()).toHaveValue("");
  });

  it("отрицательный chat id отправляется как есть", async () => {
    await loaded();
    fireEvent.change(tokenField(), { target: { value: TOKEN } });
    fireEvent.change(chatField(), { target: { value: "-1001234567890" } });
    fireEvent.click(button(t.ui.save));
    await screen.findByText(fill(t.ui.tgStatusOn, { chat_id: "-1001234567890" }));
    const [put] = callIndexes("PUT");
    expect(sentBody(fetchMock, put)).toEqual({ token: TOKEN, chat_id: "-1001234567890" });
  });

  it("после 422 поле токена тоже пусто", async () => {
    await loaded();
    queue.push(jsonResponse(422, { code: "validation_error", fields: ["body.token"] }));
    fireEvent.change(tokenField(), { target: { value: "bad" } });
    fireEvent.change(chatField(), { target: { value: "42" } });
    fireEvent.click(button(t.ui.save));
    await screen.findByRole("alert");
    expect(tokenField()).toHaveValue("");
  });

  it("при сохранённых настройках пустой токен не отправляется", async () => {
    await loaded({ token_set: true, chat_id: "42" });
    fireEvent.change(chatField(), { target: { value: "43" } });
    fireEvent.click(button(t.ui.save));
    await screen.findByText(fill(t.ui.tgStatusOn, { chat_id: "43" }));
    const [put] = callIndexes("PUT");
    const body = sentBody(fetchMock, put);
    expect(body).toEqual({ chat_id: "43" });
    expect(Object.keys(body as object)).not.toContain("token");
  });
});

describe("NotificationsPage: повторный пароль (Т40)", () => {
  const needPassword = () => jsonResponse(403, { code: "password_required" });

  async function confirm() {
    fireEvent.change(screen.getByLabelText(t.ui.password), { target: { value: "pw" } });
    fireEvent.click(button(t.ui.passwordConfirm));
  }

  it("PUT: форма пароля, затем ровно один повтор", async () => {
    await loaded({ token_set: true, chat_id: "42" });
    queue.push(needPassword());
    fireEvent.change(chatField(), { target: { value: "43" } });
    fireEvent.click(button(t.ui.save));
    await screen.findByText(t.ui.passwordTitle);
    expect(writes()).toHaveLength(1);
    await confirm();
    await screen.findByText(fill(t.ui.tgStatusOn, { chat_id: "43" }));
    expect(writes()).toEqual([
      ["PUT", URL],
      ["PUT", URL],
    ]);
    expect(sentBody(fetchMock, callIndexes("PUT")[1])).toEqual({ chat_id: "43" });
    expect(screen.queryByText(t.ui.passwordTitle)).not.toBeInTheDocument();
  });

  it("DELETE: форма пароля, затем ровно один повтор", async () => {
    await loaded({ token_set: true, chat_id: "42" });
    queue.push(needPassword());
    fireEvent.click(button(t.ui.tgDisable));
    await screen.findByText(t.ui.passwordTitle);
    await confirm();
    await screen.findByText(t.ui.tgStatusOff);
    expect(writes()).toEqual([
      ["DELETE", URL],
      ["DELETE", URL],
    ]);
  });

  it("«Отмена» скрывает форму без запроса", async () => {
    await loaded({ token_set: true, chat_id: "42" });
    queue.push(needPassword());
    fireEvent.click(button(t.ui.tgDisable));
    await screen.findByText(t.ui.passwordTitle);
    fireEvent.click(button(t.ui.passwordCancel));
    expect(screen.queryByText(t.ui.passwordTitle)).not.toBeInTheDocument();
    expect(writes()).toHaveLength(1);
    expect(fetchMock.mock.calls.some((c) => c[0] === "/api/auth/confirm-password")).toBe(false);
  });
});

describe("NotificationsPage: проверка (Т41)", () => {
  it("204 — «Сообщение отправлено»", async () => {
    await loaded({ token_set: true, chat_id: "42" });
    fireEvent.click(button(t.ui.tgTest));
    expect(await screen.findByText(t.ui.tgTestSent)).toBeInTheDocument();
    expect(writes()).toEqual([["POST", `${URL}/test`]]);
  });

  it.each([
    ["telegram_not_configured", 409],
    ["telegram_token_invalid", 409],
    ["telegram_chat_invalid", 409],
    ["telegram_unavailable", 502],
    ["telegram_token_unreadable", 409],
  ])("%s — свой текст, тело ответа не показывается", async (code, status) => {
    await loaded({ token_set: true, chat_id: "42" });
    queue.push(jsonResponse(status, { code, message: "СЕКРЕТ-ИЗ-ТЕЛА" }));
    fireEvent.click(button(t.ui.tgTest));
    const alert = await screen.findByRole("alert");
    const expected = t.errors[code as keyof typeof t.errors];
    expect(expected).toBeTruthy();
    expect(alert).toHaveTextContent(expected);
    expect(screen.queryByText(/СЕКРЕТ-ИЗ-ТЕЛА/)).not.toBeInTheDocument();
  });
});

describe("NotificationsPage: отключение (Т42)", () => {
  it("DELETE, затем «Не настроено»", async () => {
    await loaded({ token_set: true, chat_id: "42" });
    fireEvent.click(button(t.ui.tgDisable));
    await screen.findByText(t.ui.tgStatusOff);
    expect(writes()).toEqual([["DELETE", URL]]);
    expect(chatField()).toHaveValue("");
  });
});

describe("NotificationsPage: поля (Т43)", () => {
  it("токен — password без автозаполнения; форма method=post", async () => {
    await loaded();
    expect(tokenField()).toHaveAttribute("type", "password");
    expect(tokenField()).toHaveAttribute("autocomplete", "new-password");
    const form = tokenField().closest("form");
    expect(form?.getAttribute("method")).toBe("post");
    expect(form?.hasAttribute("action")).toBe(false);
  });

  it("скрытая страница (Activity) не хранит введённый токен", async () => {
    const tree = (mode: "visible" | "hidden") => (
      <Activity mode={mode}>
        <NotificationsPage />
      </Activity>
    );
    const { rerender } = render(tree("visible"));
    await screen.findByLabelText(t.ui.tgTokenLabel);
    fireEvent.change(tokenField(), { target: { value: TOKEN } });
    rerender(tree("hidden"));
    rerender(tree("visible"));
    expect(tokenField()).toHaveValue("");
  });
});
