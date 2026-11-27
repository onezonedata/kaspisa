import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { jsonResponse, sentBody, stubFetch } from "@/test/http";
import { RegisterForm } from "./RegisterForm";

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

let fetchMock: ReturnType<typeof stubFetch>;

beforeEach(() => {
  fetchMock = stubFetch();
  router.replace.mockReset();
});

afterEach(() => vi.unstubAllGlobals());

function submit(password: string) {
  fireEvent.change(screen.getByLabelText(t.ui.email), { target: { value: "new@b.kz" } });
  fireEvent.change(screen.getByLabelText(t.ui.password), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: t.ui.registerAction }));
}

describe("RegisterForm (Т14)", () => {
  it("пароль из 9 символов: текст у поля, запроса нет", () => {
    render(<RegisterForm />);
    submit("123456789");
    expect(screen.getByText(t.ui.passwordTooShort)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("пароль из 10 символов отправляется, затем переход в кабинет", async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, { id: "u1", email: "new@b.kz" }));
    render(<RegisterForm />);
    submit("1234567890");
    await vi.waitFor(() => expect(router.replace).toHaveBeenCalledWith("/shops"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/register");
    expect(sentBody(fetchMock)).toEqual({ email: "new@b.kz", password: "1234567890" });
  });

  it("поле пароля — new-password", () => {
    render(<RegisterForm />);
    const field = screen.getByLabelText(t.ui.password);
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "new-password");
  });

  it("closed: 409 оставляет форму (ответ «e-mail занят» недостижим, ADR 0027)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { code: "conflict" }));
    render(<RegisterForm />);
    submit("1234567890");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByLabelText(t.ui.email)).toBeInTheDocument();
    expect(screen.queryByText(t.ui.registrationClosed)).not.toBeInTheDocument();
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("closed: 403 registration_closed заменяет форму сообщением и ссылкой на вход (Т15)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(403, { code: "registration_closed" }));
    render(<RegisterForm />);
    submit("1234567890");
    expect(await screen.findByText(t.ui.registrationClosed)).toBeInTheDocument();
    expect(screen.queryByLabelText(t.ui.email)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(t.ui.password)).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: t.ui.toLogin })).toHaveAttribute("href", "/login");
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("closed: 429 оставляет форму", async () => {
    fetchMock.mockResolvedValue(jsonResponse(429, { code: "too_many_attempts" }));
    render(<RegisterForm />);
    submit("1234567890");
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.too_many_attempts);
    expect(screen.getByLabelText(t.ui.email)).toBeInTheDocument();
    expect(screen.queryByText(t.ui.registrationClosed)).not.toBeInTheDocument();
  });
});
