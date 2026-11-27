import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { jsonResponse, sentBody, stubFetch } from "@/test/http";
import { LoginForm } from "./LoginForm";

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

let fetchMock: ReturnType<typeof stubFetch>;

beforeEach(() => {
  fetchMock = stubFetch();
  router.replace.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function submit(email = "a@b.kz", password = "secret-pass") {
  fireEvent.change(screen.getByLabelText(t.ui.email), { target: { value: email } });
  fireEvent.change(screen.getByLabelText(t.ui.password), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: t.ui.signIn }));
}

describe("LoginForm", () => {
  it("отправляет e-mail и пароль и переходит в кабинет (Т12)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { id: "u1", email: "a@b.kz" }));
    render(<LoginForm />);
    submit();
    await vi.waitFor(() => expect(router.replace).toHaveBeenCalledWith("/shops"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/login");
    expect(sentBody(fetchMock)).toEqual({ email: "a@b.kz", password: "secret-pass" });
  });

  it("поле пароля — password с autocomplete current-password", () => {
    render(<LoginForm />);
    const field = screen.getByLabelText(t.ui.password);
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "current-password");
  });

  it("неверный пароль показывает текст ошибки (Т12)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { code: "invalid_credentials" }));
    render(<LoginForm />);
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Неверный e-mail или пароль");
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("429 показывает время ожидания (Т12)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(429, { code: "too_many_attempts", retry_after_seconds: 900 }),
    );
    render(<LoginForm />);
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("15 мин");
  });

  it("503 server_busy: один запрос, кнопка снова активна, повтора нет (Т13)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchMock.mockResolvedValue(
      jsonResponse(503, { code: "server_busy", retry_after_seconds: 1 }),
    );
    render(<LoginForm />);
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер занят");
    expect(screen.getByRole("button", { name: t.ui.signIn })).toBeEnabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("во время запроса кнопка заблокирована", async () => {
    let finish: (r: Response) => void = () => {};
    fetchMock.mockReturnValue(new Promise<Response>((resolve) => (finish = resolve)));
    render(<LoginForm />);
    submit();
    expect(await screen.findByRole("button", { name: t.ui.signingIn })).toBeDisabled();
    await act(async () => finish(jsonResponse(200, { id: "u1", email: "a@b.kz" })));
  });
});
