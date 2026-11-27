import { fireEvent, render, screen } from "@testing-library/react";
import { Activity } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { jsonResponse, sentBody, stubFetch } from "@/test/http";
import { CredentialsForm } from "./CredentialsForm";

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

function open() {
  fireEvent.click(screen.getByRole("button", { name: t.ui.changeCredentials }));
}
function submit(login = "new-login", secret = "new-secret") {
  fireEvent.change(screen.getByLabelText(t.ui.kaspiLogin), { target: { value: login } });
  fireEvent.change(screen.getByLabelText(t.ui.kaspiPassword), { target: { value: secret } });
  fireEvent.click(screen.getByRole("button", { name: t.ui.credentialsSave }));
}

describe("CredentialsForm (Т27, Т44)", () => {
  it("форма скрыта, при открытии поля пусты, имена и autocomplete не как у кабинета", () => {
    render(<CredentialsForm shop={makeShop()} onShop={vi.fn()} />);
    expect(screen.queryByLabelText(t.ui.kaspiLogin)).toBeNull();
    open();
    const login = screen.getByLabelText(t.ui.kaspiLogin);
    const secret = screen.getByLabelText(t.ui.kaspiPassword);
    expect(login).toHaveValue("");
    expect(secret).toHaveValue("");
    expect(login).toHaveAttribute("name", "kaspi-login");
    expect(secret).toHaveAttribute("name", "kaspi-secret");
    expect(login).toHaveAttribute("autocomplete", "off");
    expect(secret).toHaveAttribute("autocomplete", "off");
    expect(secret).toHaveAttribute("type", "password");
    expect(screen.getByText(t.ui.credentialsHint)).toBeInTheDocument();
  });

  it("отправляет PUT с логином и паролем, onShop получает ответ, пароль очищен", async () => {
    const updated = makeShop({ connection_state: "reauth_required" });
    fetchMock.mockResolvedValue(jsonResponse(200, updated));
    const onShop = vi.fn();
    render(<CredentialsForm shop={makeShop()} onShop={onShop} />);
    open();
    submit();
    await vi.waitFor(() => expect(onShop).toHaveBeenCalledWith(updated));
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/shops/${makeShop().id}/credentials`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("PUT");
    expect(sentBody(fetchMock)).toEqual({ login: "new-login", password: "new-secret" });
    expect(screen.getByLabelText(t.ui.kaspiPassword)).toHaveValue("");
    expect(screen.getByLabelText(t.ui.kaspiLogin)).toHaveValue("");
  });

  it("409 price_write_open — время ожидания, пароль пуст, логин сохранён", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, { code: "price_write_open", retry_after_seconds: 125 }),
    );
    render(<CredentialsForm shop={makeShop()} onShop={vi.fn()} />);
    open();
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("2 мин 5 с");
    expect(screen.getByLabelText(t.ui.kaspiPassword)).toHaveValue("");
    expect(screen.getByLabelText(t.ui.kaspiLogin)).toHaveValue("new-login");
  });

  it("пароль не остаётся после размонтирования и повторного монтирования", () => {
    const { unmount } = render(<CredentialsForm shop={makeShop()} onShop={vi.fn()} />);
    open();
    fireEvent.change(screen.getByLabelText(t.ui.kaspiPassword), { target: { value: "x" } });
    unmount();
    render(<CredentialsForm shop={makeShop()} onShop={vi.fn()} />);
    open();
    expect(screen.getByLabelText(t.ui.kaspiPassword)).toHaveValue("");
  });

  it("скрытая страница (Activity) не хранит введённый пароль", () => {
    const tree = (mode: "visible" | "hidden") => (
      <Activity mode={mode}>
        <CredentialsForm shop={makeShop()} onShop={vi.fn()} />
      </Activity>
    );
    const { rerender } = render(tree("visible"));
    open();
    fireEvent.change(screen.getByLabelText(t.ui.kaspiPassword), { target: { value: "x" } });
    rerender(tree("hidden"));
    rerender(tree("visible"));
    expect(screen.getByLabelText(t.ui.kaspiPassword)).toHaveValue("");
  });

  it("пустой логин или пароль — текст, запроса нет", () => {
    render(<CredentialsForm shop={makeShop()} onShop={vi.fn()} />);
    open();
    submit("  ", "");
    expect(screen.getByText(t.ui.credentialsInvalid)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
