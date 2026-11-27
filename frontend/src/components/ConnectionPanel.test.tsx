import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectResult, Shop } from "@/api/types";
import { fill, t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { htmlResponse, jsonResponse, sentBody, stubFetch } from "@/test/http";
import { ConnectionPanel } from "./ConnectionPanel";

let fetchMock: ReturnType<typeof stubFetch>;
const onShop = vi.fn();

beforeEach(() => {
  fetchMock = stubFetch();
  onShop.mockReset();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** Родитель хранит магазин, как ShopPage. */
function Harness({ initial }: { initial: Shop }) {
  const [shop, setShop] = useState(initial);
  return (
    <ConnectionPanel
      shop={shop}
      onShop={(s) => {
        onShop(s);
        setShop(s);
      }}
    />
  );
}

const ID = makeShop().id;
const awaiting = (o: Partial<Shop> = {}) => makeShop({ connection_state: "awaiting_code", ...o });
const connected = () => makeShop({ connection_state: "connected", merchant_uid: "30000001" });
const result = (outcome: ConnectResult["outcome"], shop: Shop, retry: number | null = null): ConnectResult => ({
  outcome,
  shop,
  retry_after_seconds: retry,
});

function typeCode(value: string) {
  fireEvent.change(screen.getByLabelText(t.ui.code), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: t.ui.sendCode }));
}

describe("ConnectionPanel: состояния (Т18)", () => {
  it("not_connected — «Подключить»", () => {
    render(<Harness initial={makeShop()} />);
    expect(screen.getByText(t.connectionState.not_connected)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t.ui.connect })).toBeEnabled();
  });

  it("reauth_required с известным кодом — текст словаря и «Подключить»", () => {
    render(<Harness initial={makeShop({ connection_state: "reauth_required", connection_error: "CREDENTIALS_INVALID" })} />);
    expect(screen.getByText(t.connectionError.CREDENTIALS_INVALID)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t.ui.connect })).toBeInTheDocument();
  });

  it("reauth_required с незнакомым кодом — запасной текст с кодом", () => {
    render(<Harness initial={makeShop({ connection_state: "reauth_required", connection_error: "NEW_KASPI_CODE" })} />);
    expect(screen.getByText(/NEW_KASPI_CODE/)).toBeInTheDocument();
  });

  it("connected — «Подключён», кнопок входа нет", () => {
    render(<Harness initial={connected()} />);
    expect(screen.getByText(t.connectionState.connected)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t.ui.connect })).toBeNull();
    expect(screen.queryByLabelText(t.ui.code)).toBeNull();
  });

  it("awaiting_code — поле кода и две кнопки", () => {
    render(<Harness initial={awaiting()} />);
    expect(screen.getByLabelText(t.ui.code)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t.ui.resendCode })).toBeEnabled();
    expect(screen.getByRole("button", { name: t.ui.restartLogin })).toBeEnabled();
  });

  it.each(["off", "dry_run", "live"] as const)("режим бота %s — своё название (Т29)", (mode) => {
    render(<Harness initial={makeShop({ bot_mode: mode })} />);
    expect(screen.getByText(fill(t.ui.botModeStatus, { mode: t.botMode[mode] }))).toBeInTheDocument();
    for (const other of ["off", "dry_run", "live"] as const) {
      if (other !== mode) {
        expect(screen.queryByText(fill(t.ui.botModeStatus, { mode: t.botMode[other] }))).toBeNull();
      }
    }
  });

  it("подсказка про «Подключить» видна до подключения и скрыта после (Т22)", () => {
    const { unmount } = render(<Harness initial={makeShop()} />);
    expect(screen.getByText(t.ui.connectHint)).toBeInTheDocument();
    unmount();
    render(<Harness initial={connected()} />);
    expect(screen.queryByText(t.ui.connectHint)).toBeNull();
  });
});

describe("ConnectionPanel: подключение (Т19, Т20)", () => {
  it("пока идёт запрос — кнопка заблокирована, виден текст ожидания; code_required даёт поле кода", async () => {
    let finish: (r: Response) => void = () => {};
    fetchMock.mockReturnValue(new Promise<Response>((resolve) => (finish = resolve)));
    render(<Harness initial={makeShop()} />);
    fireEvent.click(screen.getByRole("button", { name: t.ui.connect }));
    const busy = await screen.findByRole("button", { name: t.ui.connecting });
    expect(busy).toBeDisabled();
    expect(t.ui.connecting).toContain("до минуты");
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/shops/${ID}/login`);

    const next = awaiting({ resend_available_in_seconds: 180 });
    await act(async () => finish(jsonResponse(200, result("code_required", next))));
    expect(onShop).toHaveBeenCalledWith(next);
    expect(screen.getByText(t.connectOutcome.code_required)).toBeInTheDocument();
    expect(screen.getByLabelText(t.ui.code)).toBeInTheDocument();
  });

  it("неверный формат кода — текст, запроса нет", () => {
    render(<Harness initial={awaiting()} />);
    typeCode("12ab");
    expect(screen.getByText(t.ui.codeFormat)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("код отправляется как есть; code_invalid — «Неверный код»", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, result("code_invalid", awaiting())));
    render(<Harness initial={awaiting()} />);
    typeCode("123456");
    expect(await screen.findByText(t.connectOutcome.code_invalid)).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/shops/${ID}/code`);
    expect(sentBody(fetchMock)).toEqual({ code: "123456" });
  });

  it("исход connected — onShop и сообщение", async () => {
    const shop = connected();
    fetchMock.mockResolvedValue(jsonResponse(200, result("connected", shop)));
    render(<Harness initial={awaiting()} />);
    typeCode("123456");
    expect(await screen.findByText(t.connectOutcome.connected)).toBeInTheDocument();
    expect(onShop).toHaveBeenCalledWith(shop);
    expect(screen.queryByLabelText(t.ui.code)).toBeNull();
  });
});

describe("ConnectionPanel: restart_required (Т21)", () => {
  it("при restart_required поле кода и повторная отправка скрыты", () => {
    render(<Harness initial={awaiting({ restart_required: true })} />);
    expect(screen.queryByLabelText(t.ui.code)).toBeNull();
    expect(screen.queryByRole("button", { name: t.ui.resendCode })).toBeNull();
    expect(screen.getByText(t.ui.restartHint)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t.ui.restartLogin })).toBeEnabled();
  });

  it("после исхода restart_required", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, result("restart_required", awaiting({ restart_required: true }))),
    );
    render(<Harness initial={awaiting()} />);
    typeCode("123456");
    expect(await screen.findByText(t.ui.restartHint)).toBeInTheDocument();
    expect(screen.queryByLabelText(t.ui.code)).toBeNull();
    expect(screen.getByRole("button", { name: t.ui.restartLogin })).toBeInTheDocument();
  });

  it("после 409 restart_required магазин перечитывается", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(409, { code: "restart_required" }))
      .mockResolvedValueOnce(jsonResponse(200, awaiting({ restart_required: true })));
    render(<Harness initial={awaiting()} />);
    typeCode("123456");
    await vi.waitFor(() => expect(screen.queryByLabelText(t.ui.code)).toBeNull());
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/shops/${ID}`);
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("GET");
    expect(screen.getByRole("button", { name: t.ui.restartLogin })).toBeInTheDocument();
  });
});

describe("ConnectionPanel: отсчёт повторной отправки (Т22)", () => {
  it("кнопка заблокирована до конца отсчёта, 429 запускает новый", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchMock.mockResolvedValue(
      jsonResponse(429, { code: "resend_too_early", retry_after_seconds: 30 }),
    );
    render(<Harness initial={awaiting({ resend_available_in_seconds: 5 })} />);
    expect(screen.getByRole("button", { name: /через 5 с/ })).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    const button = screen.getByRole("button", { name: t.ui.resendCode });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(await screen.findByRole("button", { name: /через 30 с/ })).toBeDisabled();
  });

  it("null — активна сразу", () => {
    render(<Harness initial={awaiting({ resend_available_in_seconds: null })} />);
    expect(screen.getByRole("button", { name: t.ui.resendCode })).toBeEnabled();
  });
});

describe("ConnectionPanel: прочие исходы (Т23)", () => {
  it("temporary_error с retry_after_seconds 60 — время ожидания", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, result("temporary_error", makeShop(), 60)));
    render(<Harness initial={makeShop()} />);
    fireEvent.click(screen.getByRole("button", { name: t.ui.connect }));
    expect(await screen.findByText(/1 мин/)).toHaveTextContent(t.connectOutcome.temporary_error);
  });

  it("rejected — текст по connection_error магазина из ответа", async () => {
    const shop = makeShop({ connection_state: "reauth_required", connection_error: "multiple_merchants" });
    fetchMock.mockResolvedValue(jsonResponse(200, result("rejected", shop)));
    render(<Harness initial={makeShop()} />);
    fireEvent.click(screen.getByRole("button", { name: t.ui.connect }));
    const hits = await screen.findAllByText(t.connectionError.multiple_merchants);
    expect(hits.length).toBeGreaterThan(0);
  });

  it("409 state_changed — сообщение и перечитывание магазина", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(409, { code: "state_changed" }))
      .mockResolvedValueOnce(jsonResponse(200, connected()));
    render(<Harness initial={makeShop()} />);
    fireEvent.click(screen.getByRole("button", { name: t.ui.connect }));
    expect(await screen.findByText(t.errors.state_changed)).toBeInTheDocument();
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/shops/${ID}`);
    expect(onShop).toHaveBeenCalledWith(connected());
  });
});

describe("ConnectionPanel: обрыв связи (Т45)", () => {
  it("«Подключить»: fetch отклонён, магазин перечитан и уже подключён", async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError("fail"))
      .mockResolvedValueOnce(jsonResponse(200, connected()));
    render(<Harness initial={makeShop()} />);
    fireEvent.click(screen.getByRole("button", { name: t.ui.connect }));
    expect(await screen.findByText(t.ui.connectionInterrupted)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/shops/${ID}`);
    expect(onShop).toHaveBeenCalledWith(connected());
    expect(screen.queryByLabelText(t.ui.code)).toBeNull();
  });

  it("код: ответ 502 с HTML, магазин всё ещё ждёт код — поле пусто, повтора кода нет", async () => {
    fetchMock
      .mockResolvedValueOnce(htmlResponse(502))
      .mockResolvedValueOnce(jsonResponse(200, awaiting()));
    render(<Harness initial={awaiting()} />);
    typeCode("123456");
    expect(await screen.findByText(t.ui.connectionInterrupted)).toBeInTheDocument();
    expect(screen.getByLabelText(t.ui.code)).toHaveValue("");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.filter((c) => String(c[0]).endsWith("/code"))).toHaveLength(1);
  });

  it("перечитать не удалось — исходная ошибка", async () => {
    fetchMock.mockRejectedValue(new TypeError("fail"));
    render(<Harness initial={makeShop()} />);
    fireEvent.click(screen.getByRole("button", { name: t.ui.connect }));
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.network_error);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
