import { fireEvent, render, screen } from "@testing-library/react";
import { Activity } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { jsonResponse, sentBody, stubFetch } from "@/test/http";
import { AddShopForm } from "./AddShopForm";

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

let fetchMock: ReturnType<typeof stubFetch>;

beforeEach(() => {
  fetchMock = stubFetch();
  router.push.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

function fill(login = "kaspi-user", secret = "kaspi-pass") {
  fireEvent.change(screen.getByLabelText(t.ui.kaspiLogin), { target: { value: login } });
  fireEvent.change(screen.getByLabelText(t.ui.kaspiPassword), { target: { value: secret } });
  fireEvent.click(screen.getByRole("button", { name: t.ui.addShop }));
}

describe("AddShopForm", () => {
  it("показывает подсказку про «Подключить» и код из письма (Т23)", () => {
    render(<AddShopForm />);
    expect(screen.getByText(t.ui.addShopHint)).toBeInTheDocument();
  });

  it("отправляет логин и пароль и переходит на страницу магазина (Т17)", async () => {
    const shop = makeShop({ id: "new-1" });
    fetchMock.mockResolvedValue(jsonResponse(201, shop));
    const onAdded = vi.fn();
    render(<AddShopForm onAdded={onAdded} />);
    fill();
    await vi.waitFor(() => expect(router.push).toHaveBeenCalledWith("/shops/new-1"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/shops");
    expect(sentBody(fetchMock)).toEqual({ login: "kaspi-user", password: "kaspi-pass" });
    expect(onAdded).toHaveBeenCalledWith(shop);
  });

  it("поля доступов Kaspi не похожи на поля кабинета (Т44)", () => {
    render(<AddShopForm />);
    const login = screen.getByLabelText(t.ui.kaspiLogin);
    const secret = screen.getByLabelText(t.ui.kaspiPassword);
    expect(login).toHaveAttribute("name", "kaspi-login");
    expect(secret).toHaveAttribute("name", "kaspi-secret");
    expect(login).toHaveAttribute("autocomplete", "off");
    expect(secret).toHaveAttribute("autocomplete", "off");
    expect(secret).toHaveAttribute("type", "password");
  });

  it("после успеха пароль и логин пусты (Т44)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, makeShop()));
    render(<AddShopForm />);
    fill();
    await vi.waitFor(() => expect(router.push).toHaveBeenCalled());
    expect(screen.getByLabelText(t.ui.kaspiPassword)).toHaveValue("");
    expect(screen.getByLabelText(t.ui.kaspiLogin)).toHaveValue("");
  });

  it("после ошибки пароль пуст, логин сохранён (Т44)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { code: "validation_error", fields: ["body.login"] }),
    );
    render(<AddShopForm />);
    fill();
    expect(await screen.findByRole("alert")).toHaveTextContent("логин");
    expect(screen.getByLabelText(t.ui.kaspiPassword)).toHaveValue("");
    expect(screen.getByLabelText(t.ui.kaspiLogin)).toHaveValue("kaspi-user");
    expect(router.push).not.toHaveBeenCalled();
  });

  it("скрытая страница (Activity) не хранит введённый пароль", () => {
    const tree = (mode: "visible" | "hidden") => (
      <Activity mode={mode}>
        <AddShopForm />
      </Activity>
    );
    const { rerender } = render(tree("visible"));
    fireEvent.change(screen.getByLabelText(t.ui.kaspiPassword), { target: { value: "x" } });
    rerender(tree("hidden"));
    rerender(tree("visible"));
    expect(screen.getByLabelText(t.ui.kaspiPassword)).toHaveValue("");
  });
});

describe("AddShopForm: лимит одного магазина (Т19)", () => {
  const limit = () => jsonResponse(409, { code: "shop_limit_reached" });
  const addCalls = () => fetchMock.mock.calls.filter((c) => (c[1] as RequestInit).method === "POST");

  it("409 и один магазин в списке — переход на его страницу, пароль пуст", async () => {
    fetchMock
      .mockResolvedValueOnce(limit())
      .mockResolvedValueOnce(jsonResponse(200, [makeShop({ id: "old-1" })]));
    render(<AddShopForm />);
    fill();
    await vi.waitFor(() => expect(router.push).toHaveBeenCalledWith("/shops/old-1"));
    expect(screen.getByLabelText(t.ui.kaspiPassword)).toHaveValue("");
    expect(addCalls()).toHaveLength(1);
  });

  it("409 и два магазина — текст ошибки, перезагрузка списка, без перехода", async () => {
    fetchMock
      .mockResolvedValueOnce(limit())
      .mockResolvedValueOnce(jsonResponse(200, [makeShop({ id: "a" }), makeShop({ id: "b" })]));
    const onAdded = vi.fn();
    render(<AddShopForm onAdded={onAdded} />);
    fill();
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.shop_limit_reached);
    expect(router.push).not.toHaveBeenCalled();
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(addCalls()).toHaveLength(1);
  });

  it("409 и сбой чтения списка — текст ошибки, перезагрузка списка, без перехода", async () => {
    fetchMock
      .mockResolvedValueOnce(limit())
      .mockResolvedValueOnce(jsonResponse(500, { code: "internal_error" }));
    const onAdded = vi.fn();
    render(<AddShopForm onAdded={onAdded} />);
    fill();
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.shop_limit_reached);
    expect(router.push).not.toHaveBeenCalled();
    expect(onAdded).toHaveBeenCalledTimes(1);
    expect(addCalls()).toHaveLength(1);
  });
});
