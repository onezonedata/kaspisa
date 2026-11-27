import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { emptyResponse, jsonResponse, sentBody, stubFetch } from "@/test/http";
import { ResetPasswordForm } from "./ResetPasswordForm";

let fetchMock: ReturnType<typeof stubFetch>;

beforeEach(() => {
  fetchMock = stubFetch();
});

afterEach(() => vi.unstubAllGlobals());

function submit(token = "123456", password = "new-password-1") {
  fireEvent.change(screen.getByLabelText(t.ui.resetToken), { target: { value: token } });
  fireEvent.change(screen.getByLabelText(t.ui.newPassword), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: t.ui.resetAction }));
}

describe("ResetPasswordForm (Т15)", () => {
  it("отправляет код и новый пароль, затем зовёт войти", async () => {
    fetchMock.mockResolvedValue(emptyResponse(204));
    render(<ResetPasswordForm />);
    submit();
    expect(await screen.findByText(t.ui.passwordReset)).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/password-reset");
    expect(sentBody(fetchMock)).toEqual({ token: "123456", new_password: "new-password-1" });
    expect(screen.getByRole("link", { name: t.ui.toLogin })).toHaveAttribute("href", "/login");
  });

  it("поля: код — numeric one-time-code, пароль — new-password", () => {
    render(<ResetPasswordForm />);
    const token = screen.getByLabelText(t.ui.resetToken);
    expect(token).toHaveAttribute("inputmode", "numeric");
    expect(token).toHaveAttribute("autocomplete", "one-time-code");
    expect(screen.getByLabelText(t.ui.newPassword)).toHaveAttribute("autocomplete", "new-password");
  });

  it("короткий пароль не отправляется", () => {
    render(<ResetPasswordForm />);
    submit("123456", "short");
    expect(screen.getByText(t.ui.passwordTooShort)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("400 invalid_token показывает текст", async () => {
    fetchMock.mockResolvedValue(jsonResponse(400, { code: "invalid_token" }));
    render(<ResetPasswordForm />);
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.invalid_token);
  });
});
