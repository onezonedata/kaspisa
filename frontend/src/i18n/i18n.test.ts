import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import {
  codeText,
  errorText,
  fill,
  formatDuration,
  journalErrorText,
  refusalText,
  skipReasonText,
  t,
} from "./index";

describe("errorText (Т7)", () => {
  it("известный код — текст словаря", () => {
    expect(errorText(new ApiError(401, "invalid_credentials"))).toBe(t.errors.invalid_credentials);
  });

  it("неизвестный код — запасной текст с кодом", () => {
    const text = errorText(new ApiError(500, "weird_code"));
    expect(text).toContain("weird_code");
    expect(text).toContain(t.common.unknownCode);
  });

  it("retryAfterSeconds добавляет время", () => {
    const text = errorText(new ApiError(429, "too_many_attempts", null, 90));
    expect(text).toContain("1 мин 30 с");
  });

  it("422 перечисляет поля: известные подписью, неизвестные путём", () => {
    const text = errorText(
      new ApiError(422, "validation_error", ["body.city_id", "body.zzz"], null),
    );
    expect(text).toContain(t.fields["body.city_id"]);
    expect(text).toContain("body.zzz");
  });
});

describe("тексты отказов и журнала (Т8)", () => {
  it("permanent: добавляет пометку", () => {
    expect(refusalText("permanent:stock_counted")).toBe(
      `${t.refusal.stock_counted} ${t.refusal.permanentNote}`,
    );
  });

  it("без пометки — только текст", () => {
    expect(refusalText("stock_counted")).toBe(t.refusal.stock_counted);
  });

  it("журнал: свой словарь, затем отказы", () => {
    expect(journalErrorText("not_confirmed")).toBe(t.journalError.not_confirmed);
    expect(journalErrorText("preorder")).toBe(t.refusal.preorder);
  });

  it("журнал: сброс остатка с точками и составное примечание", () => {
    const one = journalErrorText("stock_reset:S1");
    expect(one).toContain("S1");
    expect(one).toContain("исключён из отслеживания");
    expect(one).not.toContain(t.common.unknownCode);
    expect(journalErrorText("stock_reset:S1,S2")).toContain("S1, S2");
    const both = journalErrorText("not_confirmed; stock_reset:S1");
    expect(both).toBe(`${t.journalError.not_confirmed}; ${one}`);
  });

  it("журнал: список точек выводится целиком, без лишних символов и пробелов", () => {
    const text = (points: string) => fill(t.journalError.stock_reset, { points });
    expect(journalErrorText("stock_reset:S1")).toBe(text("S1"));
    expect(journalErrorText("stock_reset:S1,S2")).toBe(text("S1, S2"));
    expect(journalErrorText("stock_reset: S1 , S2 ")).toBe(text("S1, S2"));
  });

  it("codeText: незнакомый код не падает", () => {
    expect(codeText(t.reason, "new_rule")).toContain("new_rule");
  });
});

describe("словарь этапа 13 (Т61)", () => {
  it.each([
    "undercut_first", "matched_first", "held_second", "dropped_to_min",
    "no_competitors_raised_to_max", "own_offer_missing", "out_of_bounds",
    "competitors_unseen", "no_competitors_raise_off", "at_min_price", "already_at_target",
    "raise_disabled", "decrease_disabled",
    // Старые коды остаются для прежних строк журнала.
    "all_competitors_below_min", "returned_to_min", "returned_to_max", "below_min_do_nothing",
  ])("есть текст причины %s", (code) => {
    expect(Object.hasOwn(t.reason, code)).toBe(true);
    expect(t.reason[code as keyof typeof t.reason]).not.toBe("");
  });

  it.each([
    "sent", "dry_run", "refused", "unknown", "not_sent", "bounds", "quota", "hold",
    "price_changed", "bot_disabled",
  ])("есть текст итога записи %s", (code) => {
    expect(codeText(t.write, code)).not.toContain(t.common.unknownCode);
  });

  it("есть результат no_change и ключи ui; belowMin удалён", () => {
    expect(t.result.no_change).toBe("цена не изменилась");
    for (const key of [
      "decisionNoChange", "decisionTarget", "decisionWrite", "awaitingCheck", "priceUnchanged",
    ] as const) {
      expect(t.ui[key]).not.toBe("");
    }
    expect(t).not.toHaveProperty("belowMin");
    expect(t.ui).not.toHaveProperty("belowMinLabel");
  });
});

describe("словарь этапа 8 (Т33)", () => {
  it("есть тексты not_live, password_required, password_invalid", () => {
    expect(codeText(t.write, "not_live")).not.toContain(t.common.unknownCode);
    expect(t.errors.password_required).not.toBe("");
    expect(t.errors.password_invalid).toBe("Неверный пароль");
    expect(errorText(new ApiError(403, "password_required"))).toBe(t.errors.password_required);
  });

  it("три режима бота названы по ADR 0024", () => {
    expect(t.botMode).toEqual({
      off: "Выключен",
      dry_run: "Проверка без записи",
      live: "Работает",
    });
  });

  it.each(["login", "password_confirm_failed", "bot_mode_changed", "bounds_changed"])(
    "есть текст события %s",
    (kind) => {
      expect(codeText(t.ownerEvent, kind)).not.toContain(t.common.unknownCode);
    },
  );

  it("есть ключи ui подтверждения, баннеров и журнала действий", () => {
    for (const key of [
      "liveConfirm", "liveConfirmServerBlocked", "leftLiveNotice", "bannerServerDryRun",
      "bannerWorkerSilent", "passwordTitle", "passwordConfirm", "passwordCancel",
      "passwordLockedHint", "eventsTitle", "eventsLink", "eventsEmpty",
    ] as const) {
      expect(t.ui[key]).not.toBe("");
    }
    expect(t.ui.bannerServerDryRun).toBe("Запись цен запрещена на сервере: бот только считает");
    expect(t.ui.bannerWorkerSilent).toBe("Обработчик не отвечает: бот сейчас не работает");
    expect(t.ui.passwordLockedHint).toContain(t.ui.logoutAll);
  });
});

describe("словарь этапа 11 (Т20)", () => {
  it.each([
    "listing_missing",
    "own_price_unknown",
    "engine_rejected",
    "response_format",
    "kaspi_error",
  ] as const)("есть текст причины %s", (code) => {
    expect(t.skipReason[code]).not.toBe("");
  });

  it("запасной текст причины подставляет код", () => {
    expect(skipReasonText("zzz")).toBe("Проверка пропущена (zzz)");
    expect(skipReasonText("kaspi_error")).toBe(t.skipReason.kaspi_error);
  });

  it("есть новые ключи ui", () => {
    const keys = [
      "trackedOn",
      "trackedOff",
      "orderHint",
      "colCheck",
      "noBounds",
      "lastCheck",
      "catalogLoading",
      "catalogNotLoaded",
      "catalogSummary",
      "trackedCount",
      "catalogTooLarge",
      "connectHint",
      "addShopHint",
    ] as const;
    for (const key of keys) expect(t.ui[key]).not.toBe("");
    expect(t.ui.colSelect).toBe("Выбор");
    expect(t.connectOutcome.connected).toContain("Каталог загрузится автоматически");
  });
});

describe("formatDuration", () => {
  it.each([
    [5, "5 с"],
    [60, "1 мин"],
    [90, "1 мин 30 с"],
    [125, "2 мин 5 с"],
    [900, "15 мин"],
  ])("%i с -> %s", (seconds, text) => {
    expect(formatDuration(seconds)).toBe(text);
  });
});

describe("тексты закрытой регистрации и подвала (Т16)", () => {
  it("есть тексты закрытой регистрации и подвала", () => {
    for (const text of [t.errors.registration_closed, t.ui.registrationClosed, t.ui.versionLabel]) {
      expect(typeof text).toBe("string");
      expect(text.length).toBeGreaterThan(0);
    }
    expect(t.ui.sourceCode).toBe("Исходный код");
    expect(errorText(new ApiError(403, "registration_closed"))).toBe(t.errors.registration_closed);
  });
});

describe("errorText с неполной ошибкой (не ApiError из ответа)", () => {
  it("без retryAfterSeconds и fields не падает и не пишет NaN", () => {
    const broken = new SyntaxError("Unexpected token <") as unknown as ApiError;
    expect(() => errorText(broken)).not.toThrow();
    expect(errorText(broken)).not.toContain("NaN");
  });
});

describe("словарь этапа 21 (Т40)", () => {
  it("есть причина bounds_missing и тексты границ", () => {
    expect(t.reason.bounds_missing).toBe("границы цены не заданы");
    expect(t.ui.noBounds).toBe(
      "Границы цены не заданы — бот цену не меняет. Цены видны после проверки ботом, если он включён, или по кнопке «Узнать цены»",
    );
    expect(t.ui.boundsHint).toBe(
      "Задайте обе границы — без них бот цену не меняет. Цены видны после проверки ботом, если он включён, или по кнопке «Узнать цены»",
    );
    expect("city_required" in t.errors).toBe(false);
  });

  it("справочника городов и пометки города нет", () => {
    expect("cities" in t).toBe(false);
    expect("priceReadOtherCity" in t.ui).toBe(false);
  });

  it("есть ключи ui «Узнать цены»; ключей итога, таблицы и пометки города нет (Т87)", () => {
    for (const key of ["priceRead", "priceReading", "colPriceRead", "priceReadTitle"] as const) {
      expect(t.ui[key]).not.toBe("");
    }
    for (const key of [
      "priceReadAt", "priceReadOwn", "priceReadOwnOver", "priceReadOwnMissing", "priceReadPosition",
      "priceReadFirst", "priceReadCity", "priceReadOtherCity", "priceReadCompetitors",
      "priceReadSeller", "priceReadPrice",
    ]) {
      expect(key in t.ui).toBe(false);
    }
  });

  it.each([
    "price_read_too_early", "kaspi_busy", "kaspi_unavailable", "price_read_in_progress",
  ] as const)("есть текст ошибки %s", (code) => {
    expect(t.errors[code]).not.toBe("");
    expect(errorText(new ApiError(429, code))).toBe(t.errors[code]);
  });
});
