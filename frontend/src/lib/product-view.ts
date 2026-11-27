// Показ полей товара: цены и позиция видны у любого товара, где значения есть (ADR 0033).
import type { Product } from "@/api/types";
import { codeText, fill, skipReasonText, t } from "@/i18n";
import { formatDateTime, formatPrice } from "./format";

const DASH = "—";

/** Цена из столбцов, которые API отдаёт как есть: нет значения — прочерк. */
export function priceText(value: number | null): string {
  return value !== null ? formatPrice(value) : DASH;
}

/** Число (конкуренты) с тем же правилом. */
export function countText(value: number | null): string {
  return value !== null ? String(value) : DASH;
}

/** Позиция: точное место или «N+», если своего магазина нет среди N прочитанных (ADR 0016). */
export function trackedPosition(product: Product): string {
  if (product.position !== null) return String(product.position);
  if (product.position_over !== null) return `${product.position_over}+`;
  return DASH;
}

/**
 * Состояние проверки отслеживаемого товара: нет границ, пропуск, последнее решение бота
 * или «ожидает проверки». Не отслеживается — null.
 */
export function checkNotice(product: Product): string | null {
  if (!product.tracked) return null;
  const unbounded = product.min_price === null || product.max_price === null;
  if (product.skip_reason !== null) return skipReasonText(product.skip_reason);
  // Без границ допустима только причина «границы не заданы»; остальные решения устарели.
  if (unbounded && product.decision_reason !== "bounds_missing") return t.ui.noBounds;
  if (product.decision_reason === null) return t.ui.awaitingCheck;
  const reason = codeText(t.reason, product.decision_reason);
  const target = product.decision_target_price;
  if (product.decision_write === null) {
    const text = fill(t.ui.decisionNoChange, { reason });
    return target === null ? text : text + fill(t.ui.decisionTarget, { price: formatPrice(target) });
  }
  return fill(t.ui.decisionWrite, {
    price: target === null ? DASH : formatPrice(target),
    reason,
    write: codeText(t.write, product.decision_write),
  });
}

/**
 * Текст «Проверки» для строки таблицы: у отслеживаемого товара без границ (и без пропуска)
 * вместо фразы — короткая подпись; полный текст — во всплывающей подсказке и в карточке товара.
 */
export function compactCheckNotice(product: Product): string | null {
  const unbounded = product.min_price === null || product.max_price === null;
  if (product.tracked && product.skip_reason === null && unbounded) return t.ui.noBoundsShort;
  return checkNotice(product);
}

export type CheckTone = "first" | "pending" | "nobounds" | "problem" | "neutral";

/**
 * Смысловой цвет ячейки «Проверка»; порядок правил — первое сработавшее.
 * Не отслеживается — null (ячейка пуста, как у checkNotice).
 */
export function checkTone(product: Product): CheckTone | null {
  if (!product.tracked) return null;
  if (product.skip_reason !== null) return "problem";
  if (product.min_price === null || product.max_price === null) return "nobounds";
  if (product.decision_reason === null) return "pending";
  if (product.decision_write === "refused") return "problem";
  if (product.position === 1) return "first";
  return "neutral";
}

/**
 * «Последняя проверка …»: время пропуска или последней проверки; нет времени — null.
 * У не отслеживаемого товара пропуск устарел — берётся только время проверки (бот или кнопка).
 */
export function lastCheckText(product: Product): string | null {
  const skipped = product.tracked && product.skip_reason !== null;
  const at = skipped ? (product.skipped_at ?? product.last_checked_at) : product.last_checked_at;
  return at === null ? null : fill(t.ui.lastCheck, { time: formatDateTime(at) });
}

/** Граница цены: не задана — прочерк. */
export function boundPrice(value: number | null): string {
  return value === null ? DASH : formatPrice(value);
}
