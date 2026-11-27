// Словарь интерфейса: тип Messages, выбор языка и функции текста кодов.
import type { ApiError } from "@/api/client";
import type { BotMode, ConnectResult, OwnerEventKind, Strategy, SyncResult } from "@/api/types";
import type { ParseResult } from "@/lib/parse";
import { ru } from "./ru";

type Keys<K extends string> = Record<K, string>;

type ErrorCode =
  | "invalid_token" | "unauthenticated" | "invalid_credentials" | "csrf_required"
  | "not_found" |"registration_closed" | "operation_not_allowed" | "login_in_progress"
  | "state_changed" | "restart_required" | "credentials_unreadable" | "price_write_open"
  | "sync_in_progress" | "catalog_too_large" | "validation_error" | "min_above_max"
  | "password_required" | "password_invalid" | "shop_limit_reached"
  | "too_many_attempts" | "resend_too_early" | "internal_error" | "service_unavailable"
  | "server_busy" | "network_error" | "unexpected_error"
  | "telegram_not_configured" | "telegram_token_required" | "telegram_token_invalid"
  | "telegram_chat_invalid" | "telegram_unavailable" | "telegram_token_unreadable"
  | "price_read_too_early" | "kaspi_busy" | "kaspi_unavailable"
  | "price_read_in_progress" | "admin_required" | "email_taken" | "cannot_delete_self";

type FieldPath =
  | "body.email" | "body.password" | "body.new_password" | "body.token" | "body.login"
  | "body.code" | "body.city_id" | "body.min_price" | "body.max_price" | "body.step"
  | "body.default_step" | "body.excluded_merchant_ids" | "body.chat_id";

type ConnectionState = "not_connected" | "awaiting_code" | "connected" | "reauth_required";

type ConnectionErrorCode =
  | "multiple_merchants" | "no_merchants" | "merchant_mismatch" | "shop_already_connected"
  | "secret_unreadable" | "session_expired" | "CREDENTIALS_INVALID" | "INSUFFICIENT_PERMISSIONS"
  | "NO_MERCHANTS" | "MERCHANT_UID_INVALID" | "SECURITY_CODE_INVALID" | "MFA_CODE_ATTEMPT_LIMIT"
  | "INACTIVE_CODE_ERROR" | "FAILED" | "STAFF_MODE";

type RefusalCode =
  | "stock_counted" | "unverified_store_state" | "price_mismatch" | "preorder" | "archived"
  | "no_stores" | "stores_changed" | "stale_listing" | "model_missing" | "snapshot_mismatch"
  | "invalid_prices";

type SkipReasonCode =
  | "listing_missing" | "own_price_unknown" | "engine_rejected" | "response_format" | "kaspi_error";

type ReasonCode =
  | "undercut_first" | "matched_first" | "held_second" | "dropped_to_min"
  | "no_competitors_raised_to_max" | "all_competitors_below_min" | "returned_to_min"
  | "returned_to_max" | "own_offer_missing" | "no_competitors_raise_off" | "already_at_target"
  | "raise_disabled" | "decrease_disabled" | "below_min_do_nothing" | "competitors_unseen"
  | "out_of_bounds" | "at_min_price" | "bounds_missing";

/** Итог записи цены для решения «менять» (`decision_write`). */
type WriteCode =
  | "sent" | "dry_run" | "refused" | "unknown" | "not_sent" | "bounds" | "same_price"
  | "listing_missing" | "own_price_unknown" | "price_changed" | "limiter_busy" | "lease_lost"
  | "not_tracked" | "not_connected" | "halted" | "bot_disabled" | "not_live" | "quota" | "hold";

/** Тексты экранов части 1; следующие части добавляют свои ключи сюда и в ru.ts. */
export interface UiMessages {
  appName: string;
  loading: string;
  notFoundTitle: string;
  notFoundBack: string;
  email: string;
  password: string;
  newPassword: string;
  resetToken: string;
  loginTitle: string;
  signIn: string;
  signingIn: string;
  registerTitle: string;
  registerAction: string;
  registering: string;
  resetTitle: string;
  resetAction: string;
  resetting: string;
  passwordReset: string;
  passwordTooShort: string;
  toRegister: string;
  toLogin: string;
  registrationClosed: string;
  sourceCode: string;
  versionLabel: string;
  forgotPassword: string;
  haveResetCode: string;
  logout: string;
  logoutAll: string;
  refresh: string;
  refreshing: string;
  retry: string;
  shopsTitle: string;
  required: string;
  invalidInt: string;
  range: string;
  shopsEmpty: string;
  addShopTitle: string;
  kaspiLogin: string;
  kaspiPassword: string;
  addShop: string;
  addingShop: string;
  shopNoConnection: string;
  shopTitle: string;
  botHalted: string;
  haltedPrefix: string;
  haltedSince: string;
  clearHalt: string;
  clearingHalt: string;
  haltLoginHint: string;
  connect: string;
  connecting: string;
  code: string;
  sendCode: string;
  resendCode: string;
  resendIn: string;
  restartLogin: string;
  restartIn: string;
  awaitingCodeHint: string;
  restartHint: string;
  codeFormat: string;
  connectionInterrupted: string;
  settingsTitle: string;
  strategyLabel: string;
  cityLabel: string;
  cityHint: string;
  cityDefaulted: string;
  cityInvalid: string;
  cityRequired: string;
  excludedLabel: string;
  excludedTooMany: string;
  defaultStepLabel: string;
  autoRaiseLabel: string;
  botModeLabel: string;
  botModeStatus: string;
  liveConfirm: string;
  liveConfirmServerBlocked: string;
  leftLiveNotice: string;
  bannerServerDryRun: string;
  bannerWorkerSilent: string;
  passwordTitle: string;
  passwordHint: string;
  passwordConfirm: string;
  passwordConfirming: string;
  passwordCancel: string;
  passwordLockedHint: string;
  eventsTitle: string;
  eventsLink: string;
  eventsEmpty: string;
  eventsToShops: string;
  colEventTime: string;
  colEventKind: string;
  colEventDetail: string;
  colEventIp: string;
  colEventUser: string;
  eventModeChange: string;
  eventBoundsChange: string;
  save: string;
  saving: string;
  saved: string;
  changeCredentials: string;
  credentialsHint: string;
  credentialsInvalid: string;
  credentialsSave: string;
  credentialsSaving: string;
  credentialsSaved: string;
  deleteShop: string;
  deleting: string;
  confirmDeleteShop: string;
  productsLink: string;
  navLabel: string;
  navPriceJournal: string;
  navShopSettings: string;
  menuOpen: string;
  menuClose: string;
  sidebarCollapse: string;
  themeDark: string;
  productsCount: string;
  syncCatalog: string;
  syncing: string;
  syncDone: string;
  syncHint: string;
  syncInterrupted: string;
  productsTitle: string;
  productTitle: string;
  toShop: string;
  toProducts: string;
  toJournal: string;
  searchLabel: string;
  searchAction: string;
  filterTracked: string;
  filterHasBounds: string;
  filterHasRefusal: string;
  filterAll: string;
  filterYes: string;
  filterNo: string;
  yes: string;
  no: string;
  colSelect: string;
  colTrack: string;
  colSku: string;
  colTitle: string;
  colMin: string;
  colMax: string;
  colStep: string;
  colDecrease: string;
  colRaise: string;
  colPrice: string;
  colPosition: string;
  colCompetitors: string;
  colFirstPrice: string;
  colRefusal: string;
  // «Узнать цены» (этап 21)
  priceRead: string;
  priceReading: string;
  colPriceRead: string;
  priceReadTitle: string;
  selectAll: string;
  selectRow: string;
  trackToggle: string;
  pageRange: string;
  pagePrev: string;
  pageNext: string;
  noProducts: string;
  catalogEmpty: string;
  bulkSelected: string;
  bulkTrackOn: string;
  bulkTrackOff: string;
  bulkDecreaseOn: string;
  bulkDecreaseOff: string;
  bulkRaiseOn: string;
  bulkRaiseOff: string;
  bulkStepLabel: string;
  bulkStepApply: string;
  bulkDone: string;
  bulkNotFound: string;
  productSettingsTitle: string;
  trackedLabel: string;
  autoDecreaseLabel: string;
  minPriceLabel: string;
  maxPriceLabel: string;
  stepLabel: string;
  boundsHint: string;
  productState: string;
  pendingTitle: string;
  pendingSent: string;
  pendingStatus: string;
  pendingSentAt: string;
  pendingHoldUntil: string;
  pendingOutsideBounds: string;
  journalTitle: string;
  journalEmpty: string;
  colWhen: string;
  colProduct: string;
  colChange: string;
  colReason: string;
  colResult: string;
  colError: string;
  competitorDetail: string;
  targetDetail: string;
  cappedAtMax: string;
  skippedCompetitors: string;
  citiesAligned: string;
  trackedOn: string;
  trackedOff: string;
  orderHint: string;
  colCheck: string;
  noBounds: string;
  noBoundsShort: string;
  lastCheck: string;
  decisionNoChange: string;
  decisionTarget: string;
  decisionWrite: string;
  awaitingCheck: string;
  priceUnchanged: string;
  catalogLoading: string;
  catalogNotLoaded: string;
  catalogSummary: string;
  trackedCount: string;
  catalogTooLarge: string;
  connectHint: string;
  addShopHint: string;
  // Плашка обновления (этап 9)
  updateAvailable: string;
  updateHow: string;
  // Уведомления в Telegram (этап 14)
  notificationsLink: string;
  notificationsTitle: string;
  tgStep1: string;
  tgStep2: string;
  tgStep3: string;
  tgStatusOff: string;
  tgStatusOn: string;
  tgTokenLabel: string;
  tgTokenKeepHint: string;
  tgChatLabel: string;
  tgOldChatHint: string;
  tgTest: string;
  tgTesting: string;
  tgTestSent: string;
  tgDisable: string;
  tgDisabling: string;
  tgAdminOnly: string;
  usersLink: string;
  usersTitle: string;
  userAdminBadge: string;
  colUserEmail: string;
  colUserRole: string;
  colUserCreated: string;
  userDelete: string;
  userDeleting: string;
  userConfirmDelete: string;
  userAdd: string;
  userAdding: string;
  userAddTitle: string;
  userLinkTitle: string;
  userLinkHint: string;
  userLinkCopy: string;
  userLinkCopied: string;
  userLinkDone: string;
}

export interface Messages {
  common: {
    unknownCode: string;
    fieldsPrefix: string;
    retryPrefix: string;
    unitMinutes: string;
    unitSeconds: string;
  };
  meta: { description: string };
  errors: Keys<ErrorCode>;
  fields: Keys<FieldPath>;
  connectionState: Keys<ConnectionState>;
  connectionError: Keys<ConnectionErrorCode>;
  connectOutcome: Record<ConnectResult["outcome"], string>;
  syncOutcome: Record<SyncResult["outcome"], string>;
  haltReason: Keys<"login_errors" | "write_errors" | "store_anomalies">;
  refusal: Keys<RefusalCode> & { permanentNote: string };
  journalError: Keys<
    "listing_missing" | "expired_unchecked" | "not_confirmed" | "session_expired" | "stock_reset"
  >;
  result: Keys<"applied" | "dry_run" | "failed" | "unconfirmed" | "no_change">;
  write: Keys<WriteCode>;
  botMode: Record<BotMode, string>;
  ownerEvent: Record<OwnerEventKind, string>;
  reason: Keys<ReasonCode>;
  pendingStatus: Keys<"sending" | "receipt" | "unknown" | "confirmed" | "expired">;
  strategy: Record<Strategy, string>;
  skipReason: Keys<SkipReasonCode | "codeText">;
  ui: UiMessages;
}

/** Выбор языка — одна строка; казахский добавится файлом kk.ts с типом Messages. */
export const t: Messages = ru;

/** Текст известного кода;незнакомый код — запасной текст с самим кодом. */
export function codeText(group: Record<string, string>, code: string): string {
  return Object.hasOwn(group, code) ? group[code] : `${t.common.unknownCode} (${code})`;
}

/** Текст причины пропуска проверки; незнакомый код — запасной текст с кодом. */
export function skipReasonText(code: string): string {
  if (code !== "codeText" && Object.hasOwn(t.skipReason, code)) {
    return t.skipReason[code as SkipReasonCode];
  }
  return t.skipReason.codeText.replace("{code}", code);
}

/** Секунды как «M мин S с» (единицы — из словаря). */
export function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  const { unitMinutes: min, unitSeconds: sec } = t.common;
  if (minutes === 0) return `${rest} ${sec}`;
  return rest === 0 ? `${minutes} ${min}` : `${minutes} ${min} ${rest} ${sec}`;
}

/** Текст ошибки разбора числа из поля ввода. */
export function parseErrorText(result: Extract<ParseResult, { ok: false }>): string {
  return { required: t.ui.required, invalid: t.ui.invalidInt, range: t.ui.range }[result.error];
}

/** Текст ошибки API: код, время ожидания и поля 422. */
export function errorText(e: ApiError): string {
  let text = codeText(t.errors, e.code);
  if (e.retryAfterSeconds !== null && e.retryAfterSeconds !== undefined) {
    text += `. ${t.common.retryPrefix} ${formatDuration(e.retryAfterSeconds)}`;
  }
  if (e.fields !== null && e.fields !== undefined && e.fields.length > 0) {
    const names = e.fields.map((f) => (Object.hasOwn(t.fields, f) ? t.fields[f as FieldPath] : f));
    text += `. ${t.common.fieldsPrefix}: ${names.join(", ")}`;
  }
  return text;
}

/** «permanent:X» — текст отказа X и пометка о повторе; иначе текст отказа. */
export function refusalText(code: string): string {
  const permanent = "permanent:";
  if (code.startsWith(permanent)) {
    return `${codeText(t.refusal, code.slice(permanent.length))} ${t.refusal.permanentNote}`;
  }
  return codeText(t.refusal, code);
}

/** Одно примечание журнала: «stock_reset:S1,S2», свой словарь, затем коды отказов. */
function journalNoteText(code: string): string {
  const reset = "stock_reset:";
  if (code.startsWith(reset)) {
    const points = code.slice(reset.length).split(",").map((p) => p.trim()).join(", ");
    return fill(t.journalError.stock_reset, { points });
  }
  return Object.hasOwn(t.journalError, code) ? t.journalError[code as keyof Messages["journalError"]] : refusalText(code);
}

/** Ошибка строки журнала; составное примечание «a; b» переводится по частям. */
export function journalErrorText(code: string): string {
  return code.split("; ").map(journalNoteText).join("; ");
}

/** Подставляет значения в шаблон вида «через {n} с». */
export function fill(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (m, name: string) =>
    Object.hasOwn(values, name) ? String(values[name]) : m,
  );
}
