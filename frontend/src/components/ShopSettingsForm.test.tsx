import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BotMode, Shop } from "@/api/types";
import { t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { jsonResponse, sentBody, stubFetch } from "@/test/http";
import { ShopSettingsForm, shopPatch } from "./ShopSettingsForm";

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function Harness({ initial, next }: { initial: Shop; next?: Shop }) {
  const [shop, setShop] = useState(initial);
  return (
    <>
      <ShopSettingsForm shop={shop} onShop={setShop} />
      {next ? <button onClick={() => setShop(next)}>родитель</button> : null}
    </>
  );
}

const save = () => screen.getByRole("button", { name: t.ui.save });
const change = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const pickMode = (mode: BotMode) => fireEvent.click(screen.getByLabelText(t.botMode[mode]));

describe("ShopSettingsForm: режим бота (Т27)", () => {
  it("три режима видны, отмечен текущий", () => {
    render(<Harness initial={makeShop({ bot_mode: "dry_run", city_id: "750000000" })} />);
    expect(screen.getAllByRole("radio")).toHaveLength(3);
    for (const mode of ["off", "dry_run", "live"] as const) {
      expect(screen.getByLabelText(t.botMode[mode])).toBeInTheDocument();
    }
    expect(screen.getByLabelText(t.botMode.dry_run)).toBeChecked();
  });

  it("shopPatch кладёт bot_mode только при изменении", () => {
    const shop = makeShop({ bot_mode: "dry_run", city_id: "750000000" });
    const input = (botMode: BotMode) => ({
      strategy: shop.strategy,
      cityId: "750000000",
      excluded: "",
      defaultStep: String(shop.default_step),
      autoRaise: shop.auto_raise_enabled,
      botMode,
    });
    expect(shopPatch(shop, input("dry_run")).patch).toEqual({});
    expect(shopPatch(shop, input("live")).patch).toEqual({ bot_mode: "live" });
    expect(shopPatch(shop, input("off")).patch).toEqual({ bot_mode: "off" });
  });
});

describe("ShopSettingsForm: подтверждение live (Т28)", () => {
  const dryShop = (extra: Partial<Shop> = {}) =>
    makeShop({ bot_mode: "dry_run", city_id: "750000000", server_dry_run: false, ...extra });

  it("«Отмена» в confirm — запроса нет", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Harness initial={dryShop()} />);
    pickMode("live");
    fireEvent.click(save());
    expect(confirm).toHaveBeenCalledWith(t.ui.liveConfirm);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("«ОК» — запрос с bot_mode live", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValue(jsonResponse(200, dryShop({ bot_mode: "live" })));
    render(<Harness initial={dryShop()} />);
    pickMode("live");
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(sentBody(fetchMock)).toEqual({ bot_mode: "live" });
  });

  it.each([true, null])("server_dry_run = %s — в тексте подтверждения запрет на сервере", (flag) => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Harness initial={dryShop({ server_dry_run: flag })} />);
    pickMode("live");
    fireEvent.click(save());
    const text = confirm.mock.calls[0][0] as string;
    expect(text).toContain(t.ui.liveConfirm);
    expect(text).toContain(t.ui.liveConfirmServerBlocked);
    expect(text).toContain("запрещена на сервере");
  });

  it("server_dry_run = false — фразы о запрете нет", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Harness initial={dryShop()} />);
    pickMode("live");
    fireEvent.click(save());
    expect(confirm.mock.calls[0][0]).not.toContain("запрещена на сервере");
  });

  it("live → off без confirm; после сохранения — текст про уже отправленную цену", async () => {
    const confirm = vi.spyOn(window, "confirm");
    fetchMock.mockResolvedValue(jsonResponse(200, dryShop({ bot_mode: "off" })));
    render(<Harness initial={dryShop({ bot_mode: "live" })} />);
    expect(screen.queryByText(t.ui.leftLiveNotice)).toBeNull();
    pickMode("off");
    fireEvent.click(save());
    expect(await screen.findByText(t.ui.leftLiveNotice)).toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
    expect(sentBody(fetchMock)).toEqual({ bot_mode: "off" });
  });

  it("off → dry_run: текста про отправленную цену нет", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, dryShop()));
    render(<Harness initial={dryShop({ bot_mode: "off" })} />);
    pickMode("dry_run");
    fireEvent.click(save());
    await screen.findByText(t.ui.saved);
    expect(screen.queryByText(t.ui.leftLiveNotice)).toBeNull();
  });
});

describe("ShopSettingsForm: частичный PATCH (Т25)", () => {
  it("без изменений «Сохранить» заблокирована", () => {
    render(<Harness initial={makeShop({ city_id: "750000000" })} />);
    expect(screen.getByLabelText(t.ui.cityLabel)).toHaveValue("750000000");
    expect(screen.queryByText(t.ui.cityDefaulted)).toBeNull();
    expect(save()).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("изменена только стратегия — тело ровно из одного поля; ответ заменяет исходный", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, makeShop({ city_id: "750000000", strategy: "hold_second" })),
    );
    render(<Harness initial={makeShop({ city_id: "750000000" })} />);
    change(t.ui.strategyLabel, "hold_second");
    expect(save()).toBeEnabled();
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/shops/${makeShop().id}/settings`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("PATCH");
    expect(sentBody(fetchMock)).toEqual({ strategy: "hold_second" });
    await vi.waitFor(() => expect(save()).toBeDisabled());
    expect(screen.getByLabelText(t.ui.strategyLabel)).toHaveValue("hold_second");
  });

  it("очищенный город — city_id null", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop()));
    render(<Harness initial={makeShop({ city_id: "750000000" })} />);
    change(t.ui.cityLabel, "  ");
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sentBody(fetchMock)).toEqual({ city_id: null });
  });

  it("исключённые магазины — по строкам, пустые и пробелы отбрасываются", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ city_id: "750000000" })));
    render(<Harness initial={makeShop({ city_id: "750000000" })} />);
    change(t.ui.excludedLabel, " 111 \n\n222\n");
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sentBody(fetchMock)).toEqual({ excluded_merchant_ids: ["111", "222"] });
  });

  it("флажки уходят булевыми значениями", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ city_id: "750000000" })));
    render(<Harness initial={makeShop({ city_id: "750000000" })} />);
    fireEvent.click(screen.getByLabelText(t.ui.autoRaiseLabel));
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sentBody(fetchMock)).toEqual({ auto_raise_enabled: true });
  });
});

describe("ShopSettingsForm: без настройки минимальной цены (Т56)", () => {
  it("поля «Если конкуренты ниже минимальной цены» нет, выпадающий список один", () => {
    render(<Harness initial={makeShop({ city_id: "750000000" })} />);
    expect(screen.queryByText(/Если конкуренты ниже минимальной цены/)).toBeNull();
    expect(screen.getAllByRole("combobox")).toHaveLength(1);
  });

  it("сохранение не шлёт below_min", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ city_id: "750000000" })));
    render(<Harness initial={makeShop({ city_id: "750000000" })} />);
    change(t.ui.strategyLabel, "hold_second");
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sentBody(fetchMock)).not.toHaveProperty("below_min");
  });
});

describe("ShopSettingsForm: ошибки (Т26)", () => {
  it("422 по городу при включении бота — подсказка у поля города", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { code: "validation_error", fields: ["body.city_id"] }),
    );
    render(<Harness initial={makeShop()} />);
    change(t.ui.cityLabel, "");
    pickMode("dry_run");
    fireEvent.click(save());
    expect(await screen.findByText(t.ui.cityRequired)).toBeInTheDocument();
    expect(sentBody(fetchMock)).toEqual({ bot_mode: "dry_run" });
    expect(screen.getByLabelText(t.botMode.dry_run)).toBeChecked();
  });

  it("422 по городу и другому полю — подсказка у города и остальные поля общим текстом", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { code: "validation_error", fields: ["body.city_id", "body.step"] }),
    );
    render(<Harness initial={makeShop()} />);
    change(t.ui.cityLabel, "");
    pickMode("dry_run");
    fireEvent.click(save());
    expect(await screen.findByText(t.ui.cityRequired)).toBeInTheDocument();
    const alerts = screen.getAllByRole("alert").map((a) => a.textContent ?? "");
    expect(alerts.some((a) => a.includes(t.fields["body.step"]))).toBe(true);
  });

  it.each([
    ["0", t.ui.range],
    ["1.5", t.ui.invalidInt],
    ["", t.ui.required],
  ])("шаг по умолчанию %j — текст у поля, запроса нет", (value, text) => {
    render(<Harness initial={makeShop()} />);
    change(t.ui.defaultStepLabel, value);
    fireEvent.click(save());
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("код города не из цифр — текст, запроса нет", () => {
    render(<Harness initial={makeShop()} />);
    change(t.ui.cityLabel, "Алматы");
    fireEvent.click(save());
    expect(screen.getByText(t.ui.cityInvalid)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("ShopSettingsForm: переинициализация (Т48, шаг магазина)", () => {
  it("новый объект от родителя сбрасывает несохранённые правки", () => {
    render(
      <Harness
        initial={makeShop({ default_step: 1, city_id: "750000000" })}
        next={makeShop({ default_step: 7, city_id: "750000000" })}
      />,
    );
    change(t.ui.defaultStepLabel, "5");
    expect(save()).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "родитель" }));
    expect(screen.getByLabelText(t.ui.defaultStepLabel)).toHaveValue("7");
    expect(save()).toBeDisabled();
  });

  it("422 по городу: само поле города не дублируется в общем тексте", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { code: "validation_error", fields: ["body.city_id", "body.step"] }),
    );
    render(<Harness initial={makeShop()} />);
    change(t.ui.cityLabel, "");
    pickMode("dry_run");
    fireEvent.click(save());
    await screen.findByText(t.ui.cityRequired);
    const general = screen.getAllByRole("alert").filter((a) => a.textContent !== t.ui.cityRequired);
    expect(general.length).toBeGreaterThan(0);
    for (const a of general) expect(a.textContent).not.toContain(t.fields["body.city_id"]);
  });

  it("422 без города: общий текст с полями, подсказки у города нет", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { code: "validation_error", fields: ["body.step"] }),
    );
    render(<Harness initial={makeShop()} />);
    pickMode("dry_run");
    fireEvent.click(save());
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(t.fields["body.step"]);
    expect(screen.queryByText(t.ui.cityRequired)).toBeNull();
  });

  it("422 с ожиданием: время повтора доходит до текста ошибки", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        code: "validation_error",
        fields: ["body.city_id", "body.step"],
        retry_after_seconds: 90,
      }),
    );
    render(<Harness initial={makeShop()} />);
    change(t.ui.cityLabel, "");
    pickMode("dry_run");
    fireEvent.click(save());
    await screen.findByText(t.ui.cityRequired);
    const texts = screen.getAllByRole("alert").map((a) => a.textContent ?? "");
    expect(texts.some((x) => x.includes(t.common.retryPrefix))).toBe(true);
  });
});

describe("ShopSettingsForm: город по умолчанию (Н10, ADR 0014)", () => {
  it("город не задан — подставлен 710000000 с пометкой, «Сохранить» активна", () => {
    render(<Harness initial={makeShop()} />);
    expect(screen.getByLabelText(t.ui.cityLabel)).toHaveValue("710000000");
    expect(screen.getByText(t.ui.cityDefaulted)).toBeInTheDocument();
    expect(t.ui.cityDefaulted).toContain("Астана");
    expect(t.ui.cityHint).toContain("710000000 — Астана");
    expect(save()).toBeEnabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("«Сохранить» без правок шлёт только город; после ответа пометка исчезает", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ city_id: "710000000" })));
    render(<Harness initial={makeShop()} />);
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/shops/${makeShop().id}/settings`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("PATCH");
    expect(sentBody(fetchMock)).toEqual({ city_id: "710000000" });
    await vi.waitFor(() => expect(save()).toBeDisabled());
    expect(screen.queryByText(t.ui.cityDefaulted)).toBeNull();
    expect(screen.getByLabelText(t.ui.cityLabel)).toHaveValue("710000000");
  });

  it("включённый продавцом бот уходит вместе с подставленным городом", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, makeShop({ city_id: "710000000", bot_mode: "dry_run" })),
    );
    render(<Harness initial={makeShop({ bot_mode: "off" })} />);
    pickMode("dry_run");
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sentBody(fetchMock)).toEqual({ bot_mode: "dry_run", city_id: "710000000" });
  });

  it("стёртое поле — пометки нет, «Сохранить» заблокирована, запросов нет", () => {
    render(<Harness initial={makeShop()} />);
    change(t.ui.cityLabel, "  ");
    expect(screen.queryByText(t.ui.cityDefaulted)).toBeNull();
    expect(save()).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("свой код города — пометки нет, уходит без пробелов", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ city_id: "123" })));
    render(<Harness initial={makeShop()} />);
    change(t.ui.cityLabel, " 123 ");
    expect(screen.queryByText(t.ui.cityDefaulted)).toBeNull();
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sentBody(fetchMock)).toEqual({ city_id: "123" });
  });

  it("новый объект без города подставляет город заново", () => {
    render(
      <Harness
        initial={makeShop({ city_id: "710000000" })}
        next={makeShop({ city_id: null })}
      />,
    );
    expect(screen.queryByText(t.ui.cityDefaulted)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "родитель" }));
    expect(screen.getByLabelText(t.ui.cityLabel)).toHaveValue("710000000");
    expect(screen.getByText(t.ui.cityDefaulted)).toBeInTheDocument();
    expect(save()).toBeEnabled();
  });
});
