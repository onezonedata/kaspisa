import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SyncResult } from "@/api/types";
import { fill, t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { htmlResponse, jsonResponse, stubFetch } from "@/test/http";
import { CatalogSync } from "./CatalogSync";

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

const connected = () => makeShop({ connection_state: "connected", merchant_uid: "30000001" });
const button = () => screen.getByRole("button", { name: t.ui.syncCatalog });

function result(o: Partial<SyncResult> = {}): SyncResult {
  return {
    outcome: "synced",
    added: 3,
    updated: 1,
    missing: 2,
    retry_after_seconds: null,
    shop: connected(),
    ...o,
  };
}

describe("CatalogSync (Т28)", () => {
  it.each(["not_connected", "awaiting_code", "reauth_required"] as const)(
    "при %s кнопки загрузки нет",
    (state) => {
      render(<CatalogSync shop={makeShop({ connection_state: state })} onShop={vi.fn()} />);
      expect(screen.queryByRole("button")).toBeNull();
    },
  );

  it("при connected кнопка есть и подпись про суточное обновление", () => {
    render(<CatalogSync shop={connected()} onShop={vi.fn()} />);
    expect(button()).toBeEnabled();
    expect(screen.getByText(t.ui.syncHint)).toBeInTheDocument();
  });

  it("пока запрос идёт — кнопка заблокирована, виден текст «до 4 минут»", async () => {
    let finish: (r: Response) => void = () => {};
    fetchMock.mockReturnValue(new Promise<Response>((resolve) => (finish = resolve)));
    render(<CatalogSync shop={connected()} onShop={vi.fn()} />);
    fireEvent.click(button());
    const busy = await screen.findByRole("button", { name: t.ui.syncing });
    expect(busy).toBeDisabled();
    expect(t.ui.syncing).toContain("4 минут");
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/shops/${connected().id}/products/sync`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    finish(jsonResponse(200, result()));
    await screen.findByRole("button", { name: t.ui.syncCatalog });
  });

  it("synced — итог «Добавлено 3, обновлено 1, нет в кабинете 2» и onShop", async () => {
    const onShop = vi.fn();
    fetchMock.mockResolvedValue(jsonResponse(200, result()));
    render(<CatalogSync shop={connected()} onShop={onShop} />);
    fireEvent.click(button());
    expect(
      await screen.findByText(fill(t.ui.syncDone, { added: 3, updated: 1, missing: 2 })),
    ).toBeInTheDocument();
    expect(t.ui.syncDone).toContain("Добавлено");
    expect(onShop).toHaveBeenCalledWith(connected());
  });

  it("409 sync_in_progress — не ошибка: статус «Каталог загружается», магазин перечитан (Т25)", async () => {
    const onShop = vi.fn();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(409, { code: "sync_in_progress" }))
      .mockResolvedValueOnce(jsonResponse(200, { ...connected(), catalog_loading: true }));
    render(<CatalogSync shop={connected()} onShop={onShop} />);
    fireEvent.click(button());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/shops/${connected().id}`);
    await vi.waitFor(() =>
      expect(onShop).toHaveBeenCalledWith({ ...connected(), catalog_loading: true }),
    );
  });

  it("409, но перечитанный магазин уже не загружается — устаревшего статуса нет", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(409, { code: "sync_in_progress" }))
      .mockResolvedValueOnce(jsonResponse(200, { ...connected(), catalog_loading: false }));
    render(<CatalogSync shop={connected()} onShop={vi.fn()} />);
    fireEvent.click(button());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await screen.findByRole("button", { name: t.ui.syncCatalog });
    expect(screen.queryByText(t.ui.catalogLoading)).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("rejected — текст connection_error из result.shop", async () => {
    const shop = makeShop({
      connection_state: "reauth_required",
      connection_error: "CREDENTIALS_INVALID",
    });
    fetchMock.mockResolvedValue(jsonResponse(200, result({ outcome: "rejected", shop })));
    const onShop = vi.fn();
    render(<CatalogSync shop={connected()} onShop={onShop} />);
    fireEvent.click(button());
    expect(await screen.findByText(t.connectionError.CREDENTIALS_INVALID)).toBeInTheDocument();
    expect(onShop).toHaveBeenCalledWith(shop);
  });

  it("temporary_error — текст и время повтора", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, result({ outcome: "temporary_error", retry_after_seconds: 90 })),
    );
    render(<CatalogSync shop={connected()} onShop={vi.fn()} />);
    fireEvent.click(button());
    expect(
      await screen.findByText(new RegExp(t.syncOutcome.temporary_error)),
    ).toBeInTheDocument();
  });
});

describe("CatalogSync: каталог загружается (Т24)", () => {
  it("catalog_loading — кнопка выключена, текст показывает сводка каталога", () => {
    render(<CatalogSync shop={{ ...connected(), catalog_loading: true }} onShop={vi.fn()} />);
    expect(screen.queryByText(t.ui.catalogLoading)).toBeNull();
    expect(button()).toBeDisabled();
  });

  it("не загружается, каталога нет — кнопка включена", () => {
    render(<CatalogSync shop={{ ...connected(), catalog_synced_at: null }} onShop={vi.fn()} />);
    expect(button()).toBeEnabled();
    expect(screen.queryByText(t.ui.catalogLoading)).toBeNull();
  });
});

describe("CatalogSync: обрыв связи (Т45)", () => {
  it("fetch отклонён — один getShop, onShop и текст «Загрузка могла продолжиться»", async () => {
    const onShop = vi.fn();
    fetchMock
      .mockRejectedValueOnce(new TypeError("fail"))
      .mockResolvedValueOnce(jsonResponse(200, connected()));
    render(<CatalogSync shop={connected()} onShop={onShop} />);
    fireEvent.click(button());
    expect(await screen.findByText(t.ui.syncInterrupted)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/shops/${connected().id}`);
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("GET");
    expect(onShop).toHaveBeenCalledWith(connected());
  });

  it("502 с HTML — то же перечитывание", async () => {
    fetchMock
      .mockResolvedValueOnce(htmlResponse(502))
      .mockResolvedValueOnce(jsonResponse(200, connected()));
    render(<CatalogSync shop={connected()} onShop={vi.fn()} />);
    fireEvent.click(button());
    expect(await screen.findByText(t.ui.syncInterrupted)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("getShop тоже отклонён — текст исходной ошибки", async () => {
    fetchMock.mockRejectedValue(new TypeError("fail"));
    render(<CatalogSync shop={connected()} onShop={vi.fn()} />);
    fireEvent.click(button());
    expect(await screen.findByText(t.errors.network_error)).toBeInTheDocument();
    expect(screen.queryByText(t.ui.syncInterrupted)).toBeNull();
  });
});
