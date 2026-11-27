import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { stubFetch } from "@/test/http";
import { AddShopForm } from "./AddShopForm";
import { CredentialsForm } from "./CredentialsForm";
import { LoginForm } from "./LoginForm";
import { RegisterForm } from "./RegisterForm";
import { ResetPasswordForm } from "./ResetPasswordForm";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));

beforeEach(() => {
  stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

/** Форма с паролем без method="post" уйдёт GET-ом: пароль окажется в URL (С1). */
function expectPost(container: HTMLElement) {
  const form = container.querySelector("form");
  expect(form).not.toBeNull();
  expect(form?.getAttribute("method")).toBe("post");
  expect(form?.hasAttribute("action")).toBe(false);
}

describe("формы с паролем отправляются только POST-ом (С1)", () => {
  it("вход", () => expectPost(render(<LoginForm />).container));
  it("регистрация", () => expectPost(render(<RegisterForm />).container));
  it("сброс пароля", () => expectPost(render(<ResetPasswordForm />).container));
  it("добавление магазина", () => expectPost(render(<AddShopForm />).container));
  it("доступы Kaspi", () => {
    const { container } = render(<CredentialsForm shop={makeShop()} onShop={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: t.ui.changeCredentials }));
    expectPost(container);
  });
});
