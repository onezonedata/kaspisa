import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Shop } from "@/api/types";
import { t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { emptyResponse, jsonResponse, stubFetch } from "@/test/http";
import { AppShell } from "./AppShell";
import { NAV_ITEMS, type NavItem } from "./nav";
import { useShellShop } from "./shell-shop";

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
const nav = vi.hoisted(() => ({ pathname: "/shops/s1/products" }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  usePathname: () => nav.pathname,
}));
const assign = vi.fn();

let fetchMock: ReturnType<typeof stubFetch>;

beforeEach(() => {
  fetchMock = stubFetch();
  router.replace.mockReset();
  assign.mockReset();
  nav.pathname = "/shops/s1/products";
  vi.stubGlobal("location", { ...window.location, assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete document.documentElement.dataset.sidebar;
  delete document.documentElement.dataset.theme;
  localStorage.clear();
});

const noUpdate = { update_available: false, latest: null, current: "0.1.0", release_url: null };

function signedIn() {
  fetchMock.mockImplementation((url: string) =>
    Promise.resolve(
      url === "/api/auth/me"
        ? jsonResponse(200, { id: "u1", email: "seller@b.kz" })
        : url === "/api/shops"
          ? jsonResponse(200, [])
          : emptyResponse(204),
    ),
  );
}

/** Вход выполнен; магазины и остальное — по заданным ответам. */
function signedInShops(shops: () => Promise<Response>) {
  fetchMock.mockImplementation((url: string) =>
    url === "/api/auth/me"
      ? Promise.resolve(jsonResponse(200, { id: "u1", email: "seller@b.kz" }))
      : url === "/api/shops"
        ? shops()
        : Promise.resolve(emptyResponse(204)),
  );
}

const shopsOf = (...shops: Shop[]) => () => Promise.resolve(jsonResponse(200, shops));
const aside = () => document.getElementById("app-sidebar") as HTMLElement;
const shell = () => document.querySelector(".shell") as HTMLElement;
const content = () => document.querySelector(".shell-content") as HTMLElement;

describe("AppShell: журнал действий", () => {
  it("в меню есть ссылка на /events", async () => {
    signedIn();
    render(<AppShell>х</AppShell>);
    expect(await screen.findByRole("link", { name: t.ui.eventsLink })).toHaveAttribute("href", "/events");
  });
});

describe("AppShell: уведомления (Т44)", () => {
  it("в меню есть ссылка на /notifications", async () => {
    signedIn();
    render(<AppShell>х</AppShell>);
    expect(await screen.findByRole("link", { name: t.ui.notificationsLink })).toHaveAttribute(
      "href",
      "/notifications",
    );
  });
});

describe("AppShell (Т16)", () => {
  it("пока ждёт ответ — «Загрузка», затем e-mail и содержимое", async () => {
    signedIn();
    render(<AppShell>содержимое кабинета</AppShell>);
    expect(screen.getByText(t.ui.loading)).toBeInTheDocument();
    expect(await screen.findByText("seller@b.kz")).toBeInTheDocument();
    expect(screen.getByText("содержимое кабинета")).toBeInTheDocument();
  });

  it("401 ведёт на /login и не показывает содержимое", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { code: "unauthenticated" }));
    render(<AppShell>содержимое кабинета</AppShell>);
    await vi.waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByText("содержимое кабинета")).not.toBeInTheDocument();
  });

  it("«Выйти» отправляет logout и ведёт на /login", async () => {
    signedIn();
    render(<AppShell>x</AppShell>);
    fireEvent.click(await screen.findByRole("button", { name: t.ui.logout }));
    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
    const call = fetchMock.mock.calls.find((c) => c[0] === "/api/auth/logout");
    expect((call?.[1] as RequestInit).method).toBe("POST");
  });

  it("«Выйти на всех устройствах» отправляет logout-all и ведёт на /login", async () => {
    signedIn();
    render(<AppShell>x</AppShell>);
    fireEvent.click(await screen.findByRole("button", { name: t.ui.logoutAll }));
    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
    expect(fetchMock.mock.calls.some((c) => c[0] === "/api/auth/logout-all")).toBe(true);
  });

  function signedInWith(logoutAllResponse: () => Promise<Response>) {
    fetchMock.mockImplementation((url: string) =>
      url === "/api/auth/me"
        ? Promise.resolve(jsonResponse(200, { id: "u1", email: "seller@b.kz" }))
        : url === "/api/version"
          ? Promise.resolve(jsonResponse(200, noUpdate))
          : url === "/api/shops"
            ? Promise.resolve(jsonResponse(200, []))
            : logoutAllResponse(),
    );
  }

  it("503 на logout-all: ошибка на странице, перехода нет, кнопки снова активны", async () => {
    signedInWith(() => Promise.resolve(jsonResponse(503, { code: "server_busy" })));
    render(<AppShell>x</AppShell>);
    fireEvent.click(await screen.findByRole("button", { name: t.ui.logoutAll }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер занят");
    expect(assign).not.toHaveBeenCalled();
    expect(router.replace).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: t.ui.logoutAll })).toBeEnabled();
  });

  it("сетевой обрыв на logout-all: ошибка, перехода нет", async () => {
    signedInWith(() => Promise.reject(new TypeError("fail")));
    render(<AppShell>x</AppShell>);
    fireEvent.click(await screen.findByRole("button", { name: t.ui.logoutAll }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });

  it("401 на logout: сессии уже нет — переход на /login", async () => {
    signedInWith(() => Promise.resolve(jsonResponse(401, { code: "unauthenticated" })));
    render(<AppShell>x</AppShell>);
    fireEvent.click(await screen.findByRole("button", { name: t.ui.logout }));
    await vi.waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
  });

  it("во время запроса обе кнопки выхода заблокированы", async () => {
    let finish: (r: Response) => void = () => {};
    signedInWith(() => new Promise<Response>((resolve) => (finish = resolve)));
    render(<AppShell>x</AppShell>);
    fireEvent.click(await screen.findByRole("button", { name: t.ui.logoutAll }));
    expect(screen.getByRole("button", { name: t.ui.logout })).toBeDisabled();
    expect(screen.getByRole("button", { name: t.ui.logoutAll })).toBeDisabled();
    finish(emptyResponse(204));
    await vi.waitFor(() => expect(assign).toHaveBeenCalled());
  });

  it("Т15: плашка обновления видна в каркасе при update_available", async () => {
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url === "/api/auth/me"
          ? jsonResponse(200, { id: "u1", email: "seller@b.kz" })
          : url === "/api/shops"
            ? jsonResponse(200, [])
            : jsonResponse(200, {
                update_available: true,
                latest: "0.2.0",
                current: "0.1.0",
                release_url: "https://github.com/o/r/releases/tag/v0.2.0",
              }),
      ),
    );
    render(<AppShell>x</AppShell>);
    expect(await screen.findByRole("link", { name: t.ui.updateHow })).toBeInTheDocument();
  });

  it("ошибка me() не 401: текст ошибки и кнопка повтора вместо вечной загрузки", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(503, { code: "server_busy" }));
    render(<AppShell>x</AppShell>);
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер занят");
    signedIn();
    fireEvent.click(screen.getByRole("button", { name: t.ui.retry }));
    expect(await screen.findByText("seller@b.kz")).toBeInTheDocument();
  });

  it("повторное нажатие после ошибки убирает прежнюю ошибку", async () => {
    let call = 0;
    signedInWith(() =>
      ++call === 1
        ? Promise.resolve(jsonResponse(503, { code: "server_busy" }))
        : new Promise<Response>(() => {}),
    );
    render(<AppShell>x</AppShell>);
    fireEvent.click(await screen.findByRole("button", { name: t.ui.logoutAll }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: t.ui.logoutAll }));
    await vi.waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
});

describe("AppShell: меню из одного массива (Т10)", () => {
  it("ссылки меню совпадают с NAV_ITEMS по порядку и подписям", async () => {
    signedIn();
    render(<AppShell>х</AppShell>);
    const menu = await screen.findByRole("navigation", { name: t.ui.navLabel });
    const links = within(menu).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(NAV_ITEMS.map((i) => i.label));
    expect(links.map((a) => a.getAttribute("href"))).toEqual(NAV_ITEMS.map((i) => i.href(null)));
  });

  it("новая строка массива даёт новую ссылку без других правок", async () => {
    const probe: NavItem = { id: "products", label: "Проба", icon: "box", href: () => "/probe" };
    (NAV_ITEMS as NavItem[]).push(probe);
    try {
      signedIn();
      render(<AppShell>х</AppShell>);
      expect(await screen.findByRole("link", { name: "Проба" })).toHaveAttribute("href", "/probe");
    } finally {
      (NAV_ITEMS as NavItem[]).pop();
    }
  });
});

describe("AppShell: доступность меню (Т11)", () => {
  it("текущий раздел один и отмечен aria-current; кнопка открытия имеет имя", async () => {
    nav.pathname = "/shops/s1/journal";
    signedInShops(shopsOf(makeShop({ id: "s1" })));
    render(<AppShell>х</AppShell>);
    await screen.findByText("—");
    const current = document.querySelectorAll('a[aria-current="page"]');
    expect(current).toHaveLength(1);
    expect(current[0]).toHaveTextContent(t.ui.navPriceJournal);
    expect(screen.getByRole("button", { name: t.ui.menuOpen })).toBeInTheDocument();
  });
});

describe("AppShell: свернуть меню (Т12)", () => {
  const collapse = () => screen.getByRole("button", { name: t.ui.sidebarCollapse });

  it("состояние в атрибуте <html> и хранилище, переживает повторное открытие", async () => {
    signedIn();
    const first = render(<AppShell>х</AppShell>);
    await screen.findByText("seller@b.kz");
    expect(collapse()).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(collapse());
    expect(collapse()).toHaveAttribute("aria-pressed", "true");
    expect(document.documentElement.dataset.sidebar).toBe("collapsed");
    expect(localStorage.getItem("kaspisa.sidebar")).toBe("collapsed");
    for (const item of NAV_ITEMS) {
      expect(screen.getByRole("link", { name: item.label })).toBeInTheDocument();
    }

    first.unmount();
    render(<AppShell>х</AppShell>);
    await screen.findByText("seller@b.kz");
    expect(collapse()).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(collapse());
    expect(document.documentElement.dataset.sidebar).toBeUndefined();
    expect(localStorage.getItem("kaspisa.sidebar")).toBe("expanded");
  });

  it("хранилище не пишется — меню всё равно сворачивается", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    signedIn();
    render(<AppShell>х</AppShell>);
    await screen.findByText("seller@b.kz");
    fireEvent.click(collapse());
    expect(collapse()).toHaveAttribute("aria-pressed", "true");
    expect(document.documentElement.dataset.sidebar).toBe("collapsed");
  });
});

describe("AppShell: меню на телефоне (Т13)", () => {
  function stubMedia() {
    const listeners = new Set<(e: { matches: boolean }) => void>();
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: false,
      media: query,
      addEventListener: (_: string, l: (e: { matches: boolean }) => void) => {
        if (query.includes("min-width")) listeners.add(l);
      },
      removeEventListener: (_: string, l: (e: { matches: boolean }) => void) => listeners.delete(l),
    }));
    return () => act(() => listeners.forEach((l) => l({ matches: true })));
  }

  const open = () => screen.getByRole("button", { name: t.ui.menuOpen });
  const close = () => screen.getByRole("button", { name: t.ui.menuClose });

  async function mounted() {
    signedIn();
    const view = render(<AppShell>х</AppShell>);
    await screen.findByText("seller@b.kz");
    return view;
  }

  it("кнопка открывает панель: атрибуты, фокус, inert у фона", async () => {
    await mounted();
    expect(open()).toHaveAttribute("aria-expanded", "false");
    expect(open()).toHaveAttribute("aria-controls", "app-sidebar");
    expect(aside()).toBeInTheDocument();
    expect(content()).not.toHaveAttribute("inert");
    const source = screen.getByRole("link", { name: t.ui.sourceCode });
    expect(source.closest(".shell-content")).not.toBeNull();
    expect(source.closest("[inert]")).toBeNull();
    fireEvent.click(open());
    expect(open()).toHaveAttribute("aria-expanded", "true");
    expect(shell()).toHaveAttribute("data-open", "true");
    expect(close()).toHaveFocus();
    expect(content()).toHaveAttribute("inert");
    expect(source.closest("[inert]")).not.toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(source.closest("[inert]")).toBeNull();
  });

  it("Escape закрывает и возвращает фокус на кнопку открытия", async () => {
    await mounted();
    fireEvent.click(open());
    fireEvent.keyDown(document, { key: "Escape" });
    expect(shell()).toHaveAttribute("data-open", "false");
    expect(open()).toHaveAttribute("aria-expanded", "false");
    expect(open()).toHaveFocus();
    expect(content()).not.toHaveAttribute("inert");
  });

  it("кнопка «Закрыть меню» и подложка закрывают панель", async () => {
    await mounted();
    fireEvent.click(open());
    fireEvent.click(close());
    expect(shell()).toHaveAttribute("data-open", "false");
    expect(open()).toHaveFocus();
    fireEvent.click(open());
    fireEvent.click(document.querySelector(".backdrop") as HTMLElement);
    expect(shell()).toHaveAttribute("data-open", "false");
    expect(open()).toHaveFocus();
  });

  it("переход на другую страницу закрывает панель", async () => {
    const { rerender } = await mounted();
    fireEvent.click(open());
    nav.pathname = "/events";
    rerender(<AppShell>х</AppShell>);
    expect(shell()).toHaveAttribute("data-open", "false");
  });

  it("нажатие на ссылку текущей страницы тоже закрывает панель", async () => {
    await mounted();
    fireEvent.click(open());
    const link = screen.getAllByRole("link", { name: t.ui.eventsLink })[0];
    // Переход в тесте не выполняется: адрес не меняется, панель закрывает само нажатие.
    link.addEventListener("click", (e) => e.preventDefault());
    fireEvent.click(link);
    expect(shell()).toHaveAttribute("data-open", "false");
    expect(content()).not.toHaveAttribute("inert");
  });

  it("расширение окна до компьютера закрывает панель", async () => {
    const widen = stubMedia();
    await mounted();
    fireEvent.click(open());
    expect(shell()).toHaveAttribute("data-open", "true");
    widen();
    expect(shell()).toHaveAttribute("data-open", "false");
    expect(content()).not.toHaveAttribute("inert");
  });
});

describe("AppShell: магазин в меню (Т15)", () => {
  async function shown(...shops: Shop[]) {
    signedInShops(shopsOf(...shops));
    render(<AppShell>содержимое</AppShell>);
    await screen.findByText("seller@b.kz");
  }
  const productsHref = () =>
    screen.getByRole("link", { name: t.ui.productsLink }).getAttribute("href");

  it("номер магазина и режим бота", async () => {
    await shown(makeShop({ bot_mode: "dry_run", merchant_uid: "30000001" }));
    expect(await screen.findByText("30000001")).toBeInTheDocument();
    expect(screen.getByText(t.botMode.dry_run)).toBeInTheDocument();
    expect(productsHref()).toBe("/shops/11111111-1111-4111-8111-111111111111/products");
  });

  it("остановка заменяет режим", async () => {
    await shown(makeShop({ bot_mode: "live", merchant_uid: "30000001", halt_reason: "login_errors" }));
    expect(await screen.findByText(t.ui.botHalted)).toBeInTheDocument();
    expect(screen.queryByText(t.botMode.live)).toBeNull();
  });

  it("магазин без подключения: «—» и без текста об отсутствии подключения", async () => {
    await shown(makeShop({ merchant_uid: null }));
    expect(await screen.findByText("—")).toBeInTheDocument();
    expect(screen.queryByText(t.ui.shopNoConnection)).toBeNull();
  });

  it("нет магазинов: блока нет, пункты ведут на /shops", async () => {
    await shown();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/shops", expect.anything()));
    expect(screen.queryByText(t.ui.shopTitle)).toBeNull();
    expect(productsHref()).toBe("/shops");
  });

  it("два магазина: блока нет, пункты ведут на /shops", async () => {
    await shown(makeShop({ id: "a" }), makeShop({ id: "b" }));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/shops", expect.anything()));
    expect(screen.queryByText(t.ui.shopTitle)).toBeNull();
    expect(productsHref()).toBe("/shops");
  });

  it("ошибка списка: блока и сообщения в меню нет, содержимое показано", async () => {
    signedInShops(() => Promise.resolve(jsonResponse(500, { code: "internal_error" })));
    render(<AppShell>содержимое</AppShell>);
    await screen.findByText("seller@b.kz");
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/shops", expect.anything()));
    expect(screen.queryByText(t.ui.shopTitle)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("содержимое")).toBeInTheDocument();
  });
});

describe("AppShell: контекст магазина (Т16)", () => {
  function Setter({ next }: { next: Shop }) {
    const { update } = useShellShop();
    return <button onClick={() => update(next)}>сообщить</button>;
  }

  it("update со своим id меняет режим в меню, с чужим — нет", async () => {
    const shop = makeShop({ bot_mode: "off" });
    signedInShops(shopsOf(shop));
    const { rerender } = render(
      <AppShell>
        <Setter next={{ ...shop, bot_mode: "live" }} />
      </AppShell>,
    );
    expect(await screen.findByText(t.botMode.off)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "сообщить" }));
    expect(await screen.findByText(t.botMode.live)).toBeInTheDocument();

    rerender(
      <AppShell>
        <Setter next={{ ...shop, id: "other", bot_mode: "dry_run" }} />
      </AppShell>,
    );
    fireEvent.click(screen.getByRole("button", { name: "сообщить" }));
    expect(screen.getByText(t.botMode.live)).toBeInTheDocument();
    expect(screen.queryByText(t.botMode.dry_run)).toBeNull();
  });
});

describe("AppShell: перечитывание магазина (Т17)", () => {
  const calls = (path: string) => fetchMock.mock.calls.filter((c) => c[0] === path).length;

  it("смена адреса перечитывает список, me — один раз", async () => {
    signedInShops(shopsOf(makeShop()));
    const { rerender } = render(<AppShell>х</AppShell>);
    await vi.waitFor(() => expect(calls("/api/shops")).toBe(1));
    nav.pathname = "/events";
    rerender(<AppShell>х</AppShell>);
    await vi.waitFor(() => expect(calls("/api/shops")).toBe(2));
    expect(calls("/api/auth/me")).toBe(1);
  });

  it("поздний ответ прежнего запроса не затирает новый", async () => {
    let finishFirst: (r: Response) => void = () => {};
    let n = 0;
    signedInShops(() =>
      ++n === 1
        ? new Promise<Response>((resolve) => (finishFirst = resolve))
        : Promise.resolve(jsonResponse(200, [makeShop({ bot_mode: "live" })])),
    );
    const { rerender } = render(<AppShell>х</AppShell>);
    await vi.waitFor(() => expect(calls("/api/shops")).toBe(1));
    nav.pathname = "/events";
    rerender(<AppShell>х</AppShell>);
    expect(await screen.findByText(t.botMode.live)).toBeInTheDocument();
    await act(async () => finishFirst(jsonResponse(200, [makeShop({ bot_mode: "off" })])));
    expect(screen.getByText(t.botMode.live)).toBeInTheDocument();
    expect(screen.queryByText(t.botMode.off)).toBeNull();
  });
});
