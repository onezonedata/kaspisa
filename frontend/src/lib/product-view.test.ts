import { describe, expect, it } from "vitest";

import { fill, t } from "@/i18n";
import { makeProduct } from "@/test/fixtures";
import { formatDateTime, formatPrice } from "./format";
import {
  checkNotice,
  checkTone,
  compactCheckNotice,
  countText,
  lastCheckText,
  priceText,
  trackedPosition,
} from "./product-view";

describe("checkTone (Т21)", () => {
  const base = {
    tracked: true,
    min_price: 100,
    max_price: 200,
    skip_reason: null,
    decision_reason: "undercut_first",
    decision_write: null,
    position: 2,
  } as const;

  it.each([
    ["не отслеживается", { tracked: false }, null],
    ["пропуск проверки", { skip_reason: "kaspi_error" }, "problem"],
    ["пропуск важнее отсутствия границ", { skip_reason: "kaspi_error", min_price: null }, "problem"],
    ["нет нижней границы", { min_price: null }, "nobounds"],
    ["нет верхней границы, решение bounds_missing", { max_price: null, decision_reason: "bounds_missing" }, "nobounds"],
    ["решения ещё нет", { decision_reason: null }, "pending"],
    ["запись отклонена", { decision_write: "refused" }, "problem"],
    ["отказ важнее первого места", { decision_write: "refused", position: 1 }, "problem"],
    ["первое место", { position: 1 }, "first"],
    ["позиция неизвестна, больше 20", { position: null, position_over: 20 }, "neutral"],
    ["второе место", { position: 2 }, "neutral"],
    ["запись выполнена на первом месте", { decision_write: "written", position: 1 }, "first"],
    ["нет верхней границы при другом решении", { max_price: null }, "nobounds"],
  ] as const)("%s", (_name, over, expected) => {
    expect(checkTone(makeProduct({ ...base, ...over }))).toBe(expected);
  });
});

describe("checkNotice (Т19)", () => {
  it("не отслеживается — null при любых полях", () => {
    const product = makeProduct({
      tracked: false,
      min_price: null,
      skip_reason: "own_price_unknown",
      skipped_at: "2026-11-15T10:00:00Z",
    });
    expect(checkNotice(product)).toBeNull();
  });

  it("в таблице длинная подсказка noBounds заменяется короткой, остальное — как в карточке", () => {
    const unbounded = makeProduct({ tracked: true, min_price: null, max_price: 2000 });
    expect(compactCheckNotice(unbounded)).toBe(t.ui.noBoundsShort);
    const decided = makeProduct({ tracked: true, min_price: null, decision_reason: "bounds_missing" });
    expect(compactCheckNotice(decided)).toBe(t.ui.noBoundsShort);
    const skipped = makeProduct({
      tracked: true,
      min_price: null,
      skip_reason: "kaspi_error",
      skipped_at: "2026-11-15T10:00:00Z",
    });
    expect(compactCheckNotice(skipped)).toBe(checkNotice(skipped));
    const awaiting = makeProduct({ tracked: true, min_price: 1000, max_price: 2000 });
    expect(compactCheckNotice(awaiting)).toBe(checkNotice(awaiting));
    expect(compactCheckNotice(makeProduct({ tracked: false }))).toBeNull();
    expect(t.ui.noBoundsShort.length).toBeLessThan(t.ui.noBounds.length);
  });

  it("отслеживается без нижней или верхней границы — noBounds", () => {
    expect(checkNotice(makeProduct({ tracked: true, min_price: null, max_price: 2000 }))).toBe(
      t.ui.noBounds,
    );
    expect(checkNotice(makeProduct({ tracked: true, min_price: 1000, max_price: null }))).toBe(
      t.ui.noBounds,
    );
  });

  it("границы есть и есть причина — текст словаря", () => {
    const product = makeProduct({
      tracked: true,
      min_price: 1000,
      max_price: 2000,
      skip_reason: "own_price_unknown",
      skipped_at: "2026-11-15T10:00:00Z",
    });
    expect(checkNotice(product)).toBe(t.skipReason.own_price_unknown);
  });

  it("неизвестный код — запасной текст с кодом", () => {
    const product = makeProduct({
      tracked: true,
      min_price: 1000,
      max_price: 2000,
      skip_reason: "zzz",
      skipped_at: "2026-11-15T10:00:00Z",
    });
    expect(checkNotice(product)).toBe(fill(t.skipReason.codeText, { code: "zzz" }));
    expect(checkNotice(product)).toContain("zzz");
  });

  it("границы есть, решения и пропуска нет — «ожидает проверки»", () => {
    expect(checkNotice(makeProduct({ tracked: true, min_price: 1000, max_price: 2000 }))).toBe(
      t.ui.awaitingCheck,
    );
  });

  it("нет границ: bounds_missing — текст решения; пропуск — текст пропуска; иная причина — noBounds (Т31)", () => {
    const missing = makeProduct({ tracked: true, decision_reason: "bounds_missing" });
    expect(checkNotice(missing)).toBe("Цена не меняется: границы цены не заданы");
    const skipped = makeProduct({
      tracked: true,
      skip_reason: "listing_missing",
      skipped_at: "2026-11-15T10:00:00Z",
    });
    expect(checkNotice(skipped)).toBe(t.skipReason.listing_missing);
    const stale = makeProduct({ tracked: true, decision_reason: "out_of_bounds" });
    expect(checkNotice(stale)).toBe(t.ui.noBounds);
  });
});

describe("checkNotice: решение бота (Т57)", () => {
  const tracked = { tracked: true, min_price: 600, max_price: 800 };

  it("цена вне границ — «Цена не меняется» с причиной, без цели", () => {
    const product = makeProduct({ ...tracked, decision_reason: "out_of_bounds" });
    expect(checkNotice(product)).toBe(
      "Цена не меняется: текущая цена вне границ — бот её не меняет",
    );
  });

  it("своего магазина нет в карточке (Н6)", () => {
    const product = makeProduct({ ...tracked, decision_reason: "own_offer_missing" });
    expect(checkNotice(product)).toBe(
      fill(t.ui.decisionNoChange, { reason: t.reason.own_offer_missing }),
    );
    expect(checkNotice(product)).toContain("своего магазина нет в карточке города сверки");
  });

  it("«не менять» с целью добавляет цель", () => {
    const product = makeProduct({
      ...tracked,
      decision_reason: "decrease_disabled",
      decision_target_price: 600,
    });
    expect(checkNotice(product)).toBe(
      `Цена не меняется: цель ниже текущей, автоснижение выключено, цель ${formatPrice(600)}`,
    );
  });

  it.each([
    ["dry_run", "не отправлено: режим без записи"],
    ["not_live", "не отправлено: режим сменился на проверку без записи"],
    ["quota", "не отправлено: исчерпан лимит записей магазина"],
    ["sent", "отправлено в Kaspi"],
  ])("решение «менять», итог %s", (write, text) => {
    const product = makeProduct({
      ...tracked,
      decision_reason: "undercut_first",
      decision_target_price: 699,
      decision_write: write,
    });
    expect(checkNotice(product)).toBe(
      `Бот решил поставить ${formatPrice(699)}: ниже первого места; ${text}`,
    );
    expect(checkNotice(product)).not.toContain("Цена изменена");
  });

  it("неизвестные коды причины и записи — запасной текст", () => {
    const product = makeProduct({
      ...tracked,
      decision_reason: "new_rule",
      decision_target_price: 650,
      decision_write: "new_write",
    });
    const text = checkNotice(product) as string;
    expect(text).toContain(`${t.common.unknownCode} (new_rule)`);
    expect(text).toContain(`${t.common.unknownCode} (new_write)`);
  });

  it("пропуск важнее решения, отсутствие границ важнее пропуска", () => {
    const skipped = makeProduct({
      ...tracked,
      skip_reason: "response_format",
      decision_reason: "out_of_bounds",
    });
    expect(checkNotice(skipped)).toBe(t.skipReason.response_format);
    const noBounds = makeProduct({ tracked: true, decision_reason: "out_of_bounds" });
    expect(checkNotice(noBounds)).toBe(t.ui.noBounds);
  });
});

describe("lastCheckText (Т57)", () => {
  it("есть время проверки — «последняя проверка …»; нет — null", () => {
    const at = "2026-11-15T10:00:00Z";
    const bounds = { tracked: true, min_price: 600, max_price: 800 };
    expect(lastCheckText(makeProduct({ ...bounds, last_checked_at: at }))).toBe(
      fill(t.ui.lastCheck, { time: formatDateTime(at) }),
    );
    expect(lastCheckText(makeProduct(bounds))).toBeNull();
  });

  it("при пропуске берётся время пропуска", () => {
    const product = makeProduct({
      tracked: true,
      min_price: 600,
      max_price: 800,
      skip_reason: "kaspi_error",
      skipped_at: "2026-11-15T10:00:00Z",
      last_checked_at: "2026-11-14T10:00:00Z",
    });
    expect(lastCheckText(product)).toBe(
      fill(t.ui.lastCheck, { time: formatDateTime("2026-11-15T10:00:00Z") }),
    );
  });

  it("lastCheckText без границ показывает время (Т32)", () => {
    const at = "2026-11-15T10:00:00Z";
    expect(lastCheckText(makeProduct({ tracked: true, last_checked_at: at }))).toBe(
      fill(t.ui.lastCheck, { time: formatDateTime(at) }),
    );
  });
});

describe("не отслеживаемый товар: цифры и время проверки (Т85)", () => {
  it("значения и время показываются, если есть", () => {
    const at = "2026-11-22T10:00:00Z";
    const product = makeProduct({
      tracked: false,
      current_price: 2998,
      position: null,
      position_over: 11,
      competitors_count: 6,
      last_checked_at: at,
    });
    expect(priceText(product.current_price)).toBe(formatPrice(2998));
    expect(trackedPosition(product)).toBe("11+");
    expect(countText(product.competitors_count)).toBe("6");
    expect(lastCheckText(product)).toBe(fill(t.ui.lastCheck, { time: formatDateTime(at) }));
    expect(checkNotice(product)).toBeNull();
  });

  it("при всех null — прочерки и нет времени", () => {
    const product = makeProduct({ tracked: false });
    expect(priceText(product.current_price)).toBe("—");
    expect(trackedPosition(product)).toBe("—");
    expect(countText(product.competitors_count)).toBe("—");
    expect(lastCheckText(product)).toBeNull();
  });
});
