import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { emptyResponse, jsonResponse, sentBody, stubFetch } from "@/test/http";
import { BulkBar } from "./BulkBar";

const SHOP = "11111111-1111-4111-8111-111111111111";
const BULK = `/api/shops/${SHOP}/products`;
const CONFIRM = "/api/auth/confirm-password";

let fetchMock: ReturnType<typeof stubFetch>;
const onDone = vi.fn();
beforeEach(() => {
  fetchMock = stubFetch();
  onDone.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

const required = () => jsonResponse(403, { code: "password_required" });
const urls = () => fetchMock.mock.calls.map((c) => `${(c[1] as RequestInit).method} ${c[0]}`);
const typePassword = (value: string) =>
  fireEvent.change(screen.getByLabelText(t.ui.password), { target: { value } });
const confirm = () => fireEvent.click(screen.getByRole("button", { name: t.ui.passwordConfirm }));
const trackOn = () => {
  render(<BulkBar shopId={SHOP} selectedIds={[1, 2]} onDone={onDone} />);
  fireEvent.click(screen.getByRole("button", { name: t.ui.bulkTrackOn }));
};

describe("BulkBar: повторный пароль (Т31)", () => {
  it("без 403 форма не показывается", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { updated: 2 }));
    trackOn();
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledWith(2));
    expect(screen.queryByText(t.ui.passwordTitle)).toBeNull();
  });

  it("403 password_required — форма; верный пароль — правка повторена один раз", async () => {
    fetchMock
      .mockResolvedValueOnce(required())
      .mockResolvedValueOnce(emptyResponse(204))
      .mockResolvedValueOnce(jsonResponse(200, { updated: 2 }));
    trackOn();
    expect(await screen.findByText(t.ui.passwordTitle)).toBeInTheDocument();
    typePassword("секрет-пароль");
    confirm();
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledWith(2));
    expect(urls()).toEqual([`PATCH ${BULK}`, `POST ${CONFIRM}`, `PATCH ${BULK}`]);
    expect(sentBody(fetchMock, 1)).toEqual({ password: "секрет-пароль" });
    expect(sentBody(fetchMock, 2)).toEqual({ product_ids: [1, 2], changes: { tracked: true } });
    await vi.waitFor(() => expect(screen.queryByText(t.ui.passwordTitle)).toBeNull());
  });

  it("password_invalid — текст, форма остаётся, повтора нет", async () => {
    fetchMock
      .mockResolvedValueOnce(required())
      .mockResolvedValueOnce(jsonResponse(403, { code: "password_invalid" }));
    trackOn();
    await screen.findByText(t.ui.passwordTitle);
    typePassword("не тот");
    confirm();
    expect(await screen.findByText(t.errors.password_invalid)).toBeInTheDocument();
    expect(screen.getByText(t.ui.passwordTitle)).toBeInTheDocument();
    expect(urls()).toEqual([`PATCH ${BULK}`, `POST ${CONFIRM}`]);
    expect(onDone).not.toHaveBeenCalled();
  });

  it("«Отмена» — повтора нет", async () => {
    fetchMock.mockResolvedValueOnce(required());
    trackOn();
    await screen.findByText(t.ui.passwordTitle);
    fireEvent.click(screen.getByRole("button", { name: t.ui.passwordCancel }));
    expect(screen.queryByText(t.ui.passwordTitle)).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("повтор снова password_required — форма снова, без цикла", async () => {
    fetchMock
      .mockResolvedValueOnce(required())
      .mockResolvedValueOnce(emptyResponse(204))
      .mockResolvedValueOnce(required());
    trackOn();
    await screen.findByText(t.ui.passwordTitle);
    typePassword("секрет");
    confirm();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(await screen.findByText(t.ui.passwordTitle)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(onDone).not.toHaveBeenCalled();
  });

  it("429 — совет «Выйти на всех устройствах»", async () => {
    fetchMock
      .mockResolvedValueOnce(required())
      .mockResolvedValueOnce(jsonResponse(429, { code: "too_many_attempts", retry_after_seconds: 120 }));
    trackOn();
    await screen.findByText(t.ui.passwordTitle);
    typePassword("секрет");
    confirm();
    expect(await screen.findByText(t.ui.passwordLockedHint)).toBeInTheDocument();
  });
});
