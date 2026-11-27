// Сквозные проверки вида (этап 22): основной путь в светлой и тёмной теме и на телефоне,
// тема без мигания, меню, цели касания, шрифты своей сборки. Заглушка API — как в других спецификациях.
import { expect, test, type Page } from "@playwright/test";

import { fill, t } from "../src/i18n";
import { FakeApi } from "./fake-api";
import { DEMO_EMAIL, DEMO_PASSWORD, seedCabinet } from "./seed";

async function startFake(page: Page, baseURL: string | undefined): Promise<FakeApi> {
  const fake = new FakeApi();
  await fake.install(page, new URL(baseURL!).origin);
  return fake;
}

async function expectNoHorizontalScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/** Пункт меню по подписи; панель на телефоне может быть закрыта, поэтому поиск не по роли. */
function navLink(page: Page, label: string) {
  return page
    .locator("#app-sidebar nav a")
    .filter({ has: page.getByText(label, { exact: true }) });
}

async function openMenuIfPhone(page: Page) {
  const open = page.getByRole("button", { name: t.ui.menuOpen });
  if (await open.isVisible()) {
    await open.click();
    await expect(page.locator(".shell")).toHaveAttribute("data-open", "true");
  }
}

/** Переход через меню: на телефоне сначала открывается панель. `hrefTail` — ждать загрузки магазина. */
async function goViaMenu(page: Page, label: string, hrefTail?: RegExp) {
  if (hrefTail) await expect(navLink(page, label)).toHaveAttribute("href", hrefTail);
  await openMenuIfPhone(page);
  await navLink(page, label).click();
  await expect(page.locator(".shell")).toHaveAttribute("data-open", "false");
}

async function expectActive(page: Page, label: string) {
  const active = page.locator('#app-sidebar nav a[aria-current="page"]');
  await expect(active).toHaveCount(1);
  await expect(active).toHaveText(label);
  await expectNoHorizontalScroll(page);
}

/** Т24. Вход, магазин, разделы через меню, выход; без внешних запросов. */
async function mainPath(page: Page, baseURL: string | undefined) {
  const fake = await startFake(page, baseURL);
  fake.account = { email: DEMO_EMAIL, password: DEMO_PASSWORD };

  await page.goto("/login");
  await expectNoHorizontalScroll(page);
  const login = page.locator("form:visible");
  await login.getByLabel(t.ui.email).fill(DEMO_EMAIL);
  await login.getByLabel(t.ui.password).fill(DEMO_PASSWORD);
  await page.getByRole("button", { name: t.ui.signIn }).click();
  await expect(page).toHaveURL(/\/shops$/);
  await expectNoHorizontalScroll(page);

  const add = page.locator("form:visible");
  await add.getByLabel(t.ui.kaspiLogin).fill("shop-login");
  await add.getByLabel(t.ui.kaspiPassword).fill("shop-secret");
  await add.getByRole("button", { name: t.ui.addShop }).click();
  await expect(page).toHaveURL(/\/shops\/[^/]+$/);
  await page.getByRole("button", { name: t.ui.connect }).click();
  await page.getByLabel(t.ui.code).fill("123456");
  await page.getByRole("button", { name: t.ui.sendCode }).click();
  await expect(
    page.locator("strong").filter({ hasText: t.connectionState.connected }),
  ).toBeVisible();

  await goViaMenu(page, t.ui.navShopSettings, /\/shops\/[^/]+$/);
  await expect(page).toHaveURL(/\/shops\/[^/]+$/);
  await expectActive(page, t.ui.navShopSettings);

  await goViaMenu(page, t.ui.productsLink, /\/shops\/[^/]+\/products$/);
  await expect(page).toHaveURL(/\/products$/);
  const main = page.locator("main:visible");
  await page.getByRole("button", { name: t.ui.syncCatalog }).click();
  await expect(main.getByText(fill(t.ui.productsCount, { total: 3, tracked: 0 }))).toBeVisible();
  await expect(page.getByRole("link", { name: "Товар 1", exact: true })).toBeVisible();
  await expectActive(page, t.ui.productsLink);

  await page.getByRole("link", { name: "Товар 1", exact: true }).click();
  await expect(page).toHaveURL(/\/products\/1$/);
  await expect(page.getByLabel(t.ui.minPriceLabel)).toBeVisible();
  await expectActive(page, t.ui.productsLink);

  await goViaMenu(page, t.ui.navPriceJournal, /\/shops\/[^/]+\/journal$/);
  await expect(page).toHaveURL(/\/journal$/);
  await expectActive(page, t.ui.navPriceJournal);

  await goViaMenu(page, t.ui.eventsLink);
  await expect(page).toHaveURL(/\/events$/);
  await expectActive(page, t.ui.eventsLink);

  await goViaMenu(page, t.ui.notificationsLink);
  await expect(page).toHaveURL(/\/notifications$/);
  await expectActive(page, t.ui.notificationsLink);

  await openMenuIfPhone(page);
  await page.getByRole("button", { name: t.ui.logout, exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);

  expect(fake.csrfMissing).toBe(0);
  expect(fake.unhandled).toBe(0);
  expect(fake.external).toBe(0);
}

/** Т25. Переключатель темы: фон страницы, запоминание после перезагрузки. */
async function themePath(page: Page, baseURL: string | undefined, systemDark: boolean) {
  const fake = await startFake(page, baseURL);
  const shopId = seedCabinet(fake, 3);
  const LIGHT = "rgb(244, 246, 248)";
  const DARK = "rgb(15, 20, 27)";
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const toggle = page.getByRole("button", { name: t.ui.themeDark });

  await page.goto(`/shops/${shopId}/products`);
  await expect(toggle).toBeVisible();
  expect(await background()).toBe(systemDark ? DARK : LIGHT);
  await expect(toggle).toHaveAttribute("aria-pressed", String(systemDark));

  await toggle.click();
  expect(await background()).toBe(systemDark ? LIGHT : DARK);
  await expect(toggle).toHaveAttribute("aria-pressed", String(!systemDark));

  await page.reload();
  await expect(toggle).toBeVisible();
  expect(await background()).toBe(systemDark ? LIGHT : DARK);

  await toggle.click();
  expect(await background()).toBe(systemDark ? DARK : LIGHT);
  expect(fake.external).toBe(0);
}

test.describe("светлая системная тема", () => {
  test("основной путь по меню", async ({ page, baseURL }) => {
    await mainPath(page, baseURL);
  });

  test.describe("компьютер", () => {
    test.skip(({ isMobile }) => isMobile, "только компьютер");

    test("тема: переключатель и запоминание", async ({ page, baseURL }) => {
      await themePath(page, baseURL, false);
    });
  });
});

test.describe("тёмная системная тема", () => {
  test.use({ colorScheme: "dark" });
  test.skip(({ isMobile }) => isMobile, "на телефоне — только светлая");

  test("основной путь по меню", async ({ page, baseURL }) => {
    await mainPath(page, baseURL);
  });

  test("тема: переключатель и запоминание", async ({ page, baseURL }) => {
    await themePath(page, baseURL, true);
  });
});

test.describe("компьютер", () => {
  test.skip(({ isMobile }) => isMobile, "только компьютер");

  // Т26. Первый кадр уже в сохранённой теме и свёрнутом меню: встроенный скрипт сработал до отрисовки.
  test("тема и меню без мигания", async ({ page }) => {
    await page.addInitScript(() => {
      try {
        localStorage.setItem("kaspisa.theme", "dark");
        localStorage.setItem("kaspisa.sidebar", "collapsed");
      } catch {
        // хранилище недоступно — тест упадёт на проверке ниже
      }
      requestAnimationFrame(() => {
        const root = document.documentElement;
        (window as unknown as Record<string, unknown>).__firstFrame = {
          theme: root.dataset.theme,
          sidebar: root.dataset.sidebar,
          bg: getComputedStyle(root).getPropertyValue("--bg").trim(),
        };
      });
    });
    await page.goto("/login");
    await page.waitForFunction(() => "__firstFrame" in window);
    const frame = await page.evaluate(
      () => (window as unknown as Record<string, unknown>).__firstFrame,
    );
    expect(frame).toEqual({ theme: "dark", sidebar: "collapsed", bg: "#0f141b" });
  });

  // Т27. Шрифты только из сборки: свой адрес, woff2.
  test("шрифты своей сборки", async ({ page, baseURL }) => {
    const fake = await startFake(page, baseURL);
    const origin = new URL(baseURL!).origin;
    let fonts: string[] = [];
    page.on("request", (request) => {
      if (request.resourceType() === "font") fonts.push(request.url());
    });
    const loaded = (family: string) =>
      page.evaluate(
        (name) =>
          document.fonts.ready.then(() =>
            [...document.fonts].some((f) => f.family.includes(name) && f.status === "loaded"),
          ),
        family,
      );
    const checkFonts = () => {
      expect(fonts.length).toBeGreaterThanOrEqual(1);
      for (const raw of fonts) {
        const url = new URL(raw);
        expect(url.origin).toBe(origin);
        expect(url.pathname.startsWith("/_next/static/media/")).toBe(true);
        expect(url.pathname.endsWith(".woff2")).toBe(true);
      }
    };

    await page.goto("/login");
    await expect.poll(() => loaded("IBM Plex Sans")).toBe(true);
    checkFonts();

    fonts = [];
    const shopId = seedCabinet(fake);
    await page.goto(`/shops/${shopId}/products`);
    await expect(page.getByRole("row").nth(1)).toBeVisible();
    await expect.poll(() => loaded("IBM Plex Mono")).toBe(true);
    await expect.poll(() => loaded("IBM Plex Sans")).toBe(true);
    checkFonts();
    expect(fonts.some((u) => /\.(woff|ttf)(\?|$)/.test(u))).toBe(false);
    expect(fake.external).toBe(0);
  });

  // Т38. Разделитель тысяч в моноширинном шрифте не шире ~0.7 цифры.
  test("цена без широких пробелов", async ({ page, baseURL }) => {
    const fake = await startFake(page, baseURL);
    const shopId = seedCabinet(fake);
    await page.goto(`/shops/${shopId}/products/4`);
    const price = page.locator('dd.num[data-field="current_price"]');
    await expect(price).toHaveText(/^1\s575\s₸$/);
    const { w1, gap } = await price.evaluate((el) => {
      const text = el.firstChild as Text;
      const rect = (i: number) => {
        const range = document.createRange();
        range.setStart(text, i);
        range.setEnd(text, i + 1);
        return range.getBoundingClientRect();
      };
      const first = rect(0);
      return { w1: first.width, gap: rect(2).left - first.right };
    });
    expect(gap).toBeLessThanOrEqual(0.7 * w1);
  });

  // Т41. Колонки «Проверка» и «Отказ» не растягивают таблицу: текст переносится в узкой колонке,
  // а товар без границ (таких в новом каталоге большинство) — короткая подпись в строку обычной высоты.
  test("текстовые колонки узкие, строка без границ невысокая", async ({ page, baseURL }) => {
    const fake = await startFake(page, baseURL);
    const shopId = seedCabinet(fake);
    await page.goto(`/shops/${shopId}/products`);
    const noBounds = page.locator("td.text-cell [data-tone='nobounds']").first();
    await expect(noBounds).toBeVisible();
    const widths = await page.locator("td.text-cell").evaluateAll((cells) =>
      cells.map((c) => c.getBoundingClientRect().width),
    );
    expect(widths.length).toBeGreaterThan(0);
    for (const w of widths) expect(w).toBeLessThanOrEqual(400);
    await expect(noBounds).toHaveText(t.ui.noBoundsShort);
    await expect(noBounds).toHaveAttribute("title", /границы цены не заданы/i);
    const lines = await noBounds.evaluate((el) => el.getClientRects().length);
    expect(lines).toBe(1);
  });

  // Т40. Меню во всю высоту, подвал правее него.
  test("меню во всю высоту", async ({ page, baseURL }) => {
    const fake = await startFake(page, baseURL);
    const shopId = seedCabinet(fake, 50);
    await page.goto(`/shops/${shopId}/products`);
    await expect(page.getByRole("row").nth(1)).toBeVisible();
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    const source = page.getByRole("link", { name: t.ui.sourceCode });
    await expect(source).toBeVisible();
    const sidebar = (await page.locator("#app-sidebar").boundingBox())!;
    const footer = (await page.locator("footer:visible").boundingBox())!;
    expect(footer.x).toBeGreaterThanOrEqual(sidebar.x + sidebar.width);
    expect(sidebar.y + sidebar.height).toBeGreaterThanOrEqual(footer.y + footer.height - 1);
  });

  // Т30. Видимый фокус с клавиатуры.
  test("фокус виден", async ({ page, baseURL }) => {
    const fake = await startFake(page, baseURL);
    const shopId = seedCabinet(fake);
    await page.goto(`/shops/${shopId}/products`);
    await expect(page.getByRole("row").nth(1)).toBeVisible();

    const focusOutline = () =>
      page.evaluate(() => {
        const style = getComputedStyle(document.activeElement!);
        return { style: style.outlineStyle, width: parseFloat(style.outlineWidth) };
      });
    const tabUntil = async (kind: "menu" | "search") => {
      for (let i = 0; i < 60; i += 1) {
        await page.keyboard.press("Tab");
        const hit = await page.evaluate(
          ([what, searchText]) => {
            const el = document.activeElement as HTMLElement;
            return what === "menu"
              ? el.matches("#app-sidebar nav a")
              : el.tagName === "BUTTON" && el.textContent?.trim() === searchText;
          },
          [kind, t.ui.searchAction],
        );
        if (hit) return;
      }
      throw new Error(`фокус не дошёл до ${kind}`);
    };

    await tabUntil("menu");
    const link = await focusOutline();
    expect(link.style).not.toBe("none");
    expect(link.width).toBeGreaterThanOrEqual(2);

    await tabUntil("search");
    const button = await focusOutline();
    expect(button.style).not.toBe("none");
    expect(button.width).toBeGreaterThanOrEqual(2);
  });

  // Т31. Свёрнутое меню: узкая полоса, подсказки не обрезаются, состояние запоминается.
  test("свёрнутое меню", async ({ page, baseURL }) => {
    const fake = await startFake(page, baseURL);
    const shopId = seedCabinet(fake);
    await page.goto(`/shops/${shopId}/products`);
    const sidebar = page.locator("#app-sidebar");
    const link = page.getByRole("link", { name: t.ui.productsLink, exact: true });
    const label = link.locator(".nav-label");

    await page.getByRole("button", { name: t.ui.sidebarCollapse }).click();
    await expect.poll(async () => (await sidebar.boundingBox())!.width).toBeLessThanOrEqual(64);
    await expect(link).toHaveCount(1);
    expect((await label.boundingBox())!.width).toBeLessThanOrEqual(1);

    const expectTooltip = async () => {
      const bar = (await sidebar.boundingBox())!;
      const tip = (await label.boundingBox())!;
      expect(tip.width).toBeGreaterThan(1);
      expect(tip.x + tip.width).toBeGreaterThan(bar.x + bar.width);
      const top = await page.evaluate(
        ({ x, y }) => !!document.elementFromPoint(x, y)?.closest("a .nav-label"),
        { x: tip.x + tip.width / 2, y: tip.y + tip.height / 2 },
      );
      expect(top).toBe(true);
    };

    // «Выйти на всех устройствах» остаётся на экране и в порядке Tab (значок + подсказка).
    const logoutAll = page.getByRole("button", { name: t.ui.logoutAll });
    await expect(logoutAll).toBeVisible();
    const barBox = (await sidebar.boundingBox())!;
    const allBox = (await logoutAll.boundingBox())!;
    expect(allBox.x + allBox.width).toBeLessThanOrEqual(barBox.x + barBox.width);
    await page.getByRole("button", { name: t.ui.logout, exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(logoutAll).toBeFocused();
    const allLabel = (await logoutAll.locator(".nav-label").boundingBox())!;
    expect(allLabel.x + allLabel.width).toBeGreaterThan(barBox.x + barBox.width);

    await link.hover();
    await expectTooltip();

    await page.mouse.move(700, 500);
    await expect.poll(async () => (await label.boundingBox())!.width).toBeLessThanOrEqual(1);
    await link.focus();
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Tab");
    await expect(link).toBeFocused();
    await expectTooltip();

    await page.reload();
    await expect.poll(async () => (await sidebar.boundingBox())!.width).toBeLessThanOrEqual(64);

    // На телефоне свёрнутость не действует: панель открывается полной шириной с подписями.
    await page.setViewportSize({ width: 375, height: 740 });
    await page.getByRole("button", { name: t.ui.menuOpen }).click();
    await expect.poll(async () => (await sidebar.boundingBox())!.width).toBeGreaterThan(200);
    expect((await label.boundingBox())!.width).toBeGreaterThan(20);
    expect(fake.external).toBe(0);
  });
});

test.describe("телефон", () => {
  test.skip(({ isMobile }) => !isMobile, "только телефон");

  // Т28. Страница не прокручивается вбок, таблица — в своей рамке.
  test("без прокрутки вбок", async ({ page, baseURL }) => {
    const fake = await startFake(page, baseURL);
    const shopId = seedCabinet(fake, 50);

    await page.goto(`/shops/${shopId}/products`);
    await expect(page.getByRole("row").nth(1)).toBeVisible();
    await expectNoHorizontalScroll(page);
    const scrolls = await page.evaluate(() => {
      const box = document.querySelector(".table-scroll")!;
      return box.scrollWidth > box.clientWidth;
    });
    expect(scrolls).toBe(true);

    await page.goto(`/shops/${shopId}/products/1`);
    await expect(page.getByLabel(t.ui.minPriceLabel)).toBeVisible();
    await expectNoHorizontalScroll(page);

    await page.goto(`/shops/${shopId}`);
    await expect(page.getByRole("button", { name: t.ui.save, exact: true }).first()).toBeVisible();
    await expectNoHorizontalScroll(page);

    fake.user = null;
    await page.goto("/login");
    await expect(page.getByRole("button", { name: t.ui.signIn })).toBeVisible();
    await expectNoHorizontalScroll(page);
    expect(fake.external).toBe(0);
  });

  /** Видимые интерактивные элементы в области, у которых высота меньше 44 px. */
  async function smallTargets(page: Page, scopes: string[]) {
    const { checked, small } = await page.evaluate((scopeList) => {
      const selector = [
        "button",
        "select",
        'input:not([type="checkbox"]):not([type="radio"])',
        "label.hit",
        "label.check",
        'label:has(input[type="radio"])',
      ].join(",");
      const small: string[] = [];
      let checked = 0;
      for (const scope of scopeList) {
        for (const root of document.querySelectorAll(scope)) {
          for (const el of root.querySelectorAll<HTMLElement>(selector)) {
            if (!el.checkVisibility({ checkVisibilityCSS: true })) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            checked += 1;
            if (rect.height < 43.5) {
              small.push(`${el.tagName.toLowerCase()} «${(el.textContent ?? "").trim().slice(0, 30)}» ${rect.height}`);
            }
          }
        }
      }
      return { checked, small };
    }, scopes);
    // Проверка не должна проходить вхолостую: элементы в области есть.
    expect(checked).toBeGreaterThan(0);
    return small;
  }

  // Т29. Панель: закрытая вне порядка Tab, открывается поверх, закрывается Escape; цели касания.
  test("меню и цели касания", async ({ page, baseURL }) => {
    const fake = await startFake(page, baseURL);
    const shopId = seedCabinet(fake, 50);
    await page.goto(`/shops/${shopId}/products`);
    await expect(page.getByRole("row").nth(1)).toBeVisible();

    const open = page.getByRole("button", { name: t.ui.menuOpen });
    const products = page.getByRole("link", { name: t.ui.productsLink, exact: true });
    await expect(open).toHaveAttribute("aria-expanded", "false");
    await expect(products).toHaveCount(0);
    await expect(
      page.locator(".topbar").getByRole("img", { name: t.botMode.dry_run }),
    ).toBeVisible();

    await open.focus();
    await page.keyboard.press("Tab");
    const insideSidebar = await page.evaluate(
      () => !!document.activeElement?.closest("#app-sidebar"),
    );
    expect(insideSidebar).toBe(false);

    expect(await smallTargets(page, ["main", ".topbar", ".pager"])).toEqual([]);

    await open.click();
    await expect(open).toHaveAttribute("aria-expanded", "true");
    await expect(products).toBeVisible();
    await expect.poll(async () => (await page.locator("#app-sidebar").boundingBox())!.x).toBe(0);
    expect(await smallTargets(page, ["#app-sidebar"])).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(products).toBeHidden();
    await expect(open).toHaveAttribute("aria-expanded", "false");

    await page.goto(`/shops/${shopId}`);
    await expect(page.getByRole("button", { name: t.ui.save, exact: true }).first()).toBeVisible();
    expect(await smallTargets(page, ["main", ".topbar"])).toEqual([]);

    fake.user = null;
    await page.goto("/login");
    await expect(page.getByRole("button", { name: t.ui.signIn })).toBeVisible();
    expect(await smallTargets(page, ["main"])).toEqual([]);
    expect(fake.external).toBe(0);
  });
});
