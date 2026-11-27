// Сквозной путь Т38: администратор добавляет и удаляет пользователя; обычному пункта нет. На заглушке API.
import { expect, test } from "@playwright/test";

import { fill, t } from "../src/i18n";
import { FakeApi } from "./fake-api";
import { DEMO_EMAIL, DEMO_PASSWORD } from "./seed";

const NEW_EMAIL = "new@example.com";

test("администратор добавляет и удаляет пользователя, обычному пользователю раздел закрыт", async ({
  page,
  baseURL,
}) => {
  const fake = new FakeApi();
  fake.account = { email: DEMO_EMAIL, password: DEMO_PASSWORD };
  await fake.install(page, new URL(baseURL!).origin);

  await page.goto("/login");
  const login = page.locator("form:visible");
  await login.getByLabel(t.ui.email).fill(DEMO_EMAIL);
  await login.getByLabel(t.ui.password).fill(DEMO_PASSWORD);
  await page.getByRole("button", { name: t.ui.signIn }).click();
  await expect(page).toHaveURL(/\/shops$/);

  /** Форма повторного пароля; страницы предыдущих экранов остаются в DOM скрытыми, поэтому поиск внутри неё. */
  async function confirmPassword() {
    const form = page.locator("section.banner", { hasText: t.ui.passwordTitle });
    await expect(form).toBeVisible();
    await form.getByLabel(t.ui.password).fill(DEMO_PASSWORD);
    await form.getByRole("button", { name: t.ui.passwordConfirm }).click();
  }

  // Раздел открывается из меню.
  await page.getByRole("link", { name: t.ui.usersLink }).click();
  await expect(page).toHaveURL(/\/users$/);
  await expect(page.getByRole("heading", { name: t.ui.usersTitle })).toBeVisible();
  await expect(page.getByRole("row", { name: new RegExp(DEMO_EMAIL) })).toBeVisible();

  // Добавление с повторным паролем: ссылка с токеном во фрагменте и строка в списке.
  await page.locator('input[name="new-user-email"]').fill(NEW_EMAIL);
  await page.getByRole("button", { name: t.ui.userAdd }).click();
  await confirmPassword();
  const link = page.getByLabel(t.ui.userLinkTitle);
  await expect(link).toHaveValue(/\/reset-password#token=.+/);
  const row = page.getByRole("row", { name: new RegExp(NEW_EMAIL) });
  await expect(row).toBeVisible();
  expect(fake.users.map((u) => u.email)).toContain(NEW_EMAIL);

  // «Готово» стирает ссылку.
  await page.getByRole("button", { name: t.ui.userLinkDone }).click();
  await expect(link).toHaveCount(0);

  // Удаление: подтверждение, пароль, строка пропала.
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toBe(fill(t.ui.userConfirmDelete, { email: NEW_EMAIL }));
    void dialog.accept();
  });
  await row.getByRole("button", { name: t.ui.userDelete }).click();
  await confirmPassword();
  await expect(row).toHaveCount(0);
  expect(fake.users.map((u) => u.email)).not.toContain(NEW_EMAIL);

  // Обычный пользователь: пункта нет, прямой адрес даёт отказ.
  fake.user = { id: "00000000-0000-4000-8000-0000000000bb", email: "staff@example.com", is_admin: false };
  await page.goto("/shops");
  await expect(page.getByRole("link", { name: t.ui.eventsLink })).toBeVisible();
  await expect(page.getByRole("link", { name: t.ui.usersLink })).toHaveCount(0);
  await page.goto("/users");
  await expect(page.locator("p.error")).toHaveText(t.errors.admin_required);
  expect(fake.csrfMissing).toBe(0);
  expect(fake.unhandled).toBe(0);
});
