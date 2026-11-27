// Форматирование цен, дат и длительностей для показа.
const priceFormat = new Intl.NumberFormat("ru-RU");

/** Цена в целых тенге; не целое число цены не показывает. */
export function formatPrice(value: number): string {
  if (!Number.isInteger(value)) return "—";
  return `${priceFormat.format(value)} ₸`;
}

const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" });

/** Дата и время в часовом поясе браузера. */
export function formatDateTime(iso: string): string {
  return dateFormat.format(new Date(iso));
}
