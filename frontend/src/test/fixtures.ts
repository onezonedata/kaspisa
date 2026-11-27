// Фабрики объектов ответа API: тип берётся из схемы, лишнее или пропущенное поле ломает tsc.
import type { PendingWrite, PriceChange, Product, Shop } from "@/api/types";

export function makeShop(o: Partial<Shop> = {}): Shop {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    merchant_uid: null,
    connection_state: "not_connected",
    connection_error: null,
    connection_changed_at: "2026-11-15T10:00:00Z",
    created_at: "2026-11-15T10:00:00Z",
    resend_available_in_seconds: null,
    restart_required: false,
    halt_reason: null,
    halted_at: null,
    bot_mode: "off",
    server_dry_run: null,
    strategy: "be_first",
    city_id: null,
    default_step: 1,
    auto_raise_enabled: false,
    excluded_merchant_ids: [],
    catalog_synced_at: null,
    catalog_loading: false,
    ...o,
  };
}

export function makePendingWrite(o: Partial<PendingWrite> = {}): PendingWrite {
  return {
    status: "receipt",
    prices: { "750000000": 1199 },
    sent_at: "2026-11-15T10:00:00Z",
    hold_until: "2026-11-15T10:10:00Z",
    outside_bounds: false,
    ...o,
  };
}

export function makeProduct(o: Partial<Product> = {}): Product {
  return {
    id: 1,
    shop_id: "11111111-1111-4111-8111-111111111111",
    sku: "SKU-1",
    master_sku: "SKU-1",
    title: "Кабель USB-C",
    tracked: false,
    min_price: null,
    max_price: null,
    step: 1,
    auto_decrease: true,
    auto_raise: false,
    current_price: null,
    position: null,
    position_over: null,
    competitors_count: null,
    first_place_price: null,
    refusal_reason: null,
    refusal_since: null,
    retry_at: null,
    last_checked_at: null,
    skip_reason: null,
    skipped_at: null,
    decision_reason: null,
    decision_target_price: null,
    decision_write: null,
    pending_write: null,
    ...o,
  };
}

export function makePriceChange(o: Partial<PriceChange> = {}): PriceChange {
  return {
    id: 1,
    product_id: 1,
    sku: "SKU-1",
    title: "Кабель USB-C",
    old_price: 1500,
    new_price: 1199,
    reason: "undercut_first",
    target_price: 1199,
    competitor_merchant_id: null,
    competitor_name: null,
    competitor_price: null,
    capped_at_max: false,
    skipped_competitors: 0,
    cities_aligned: false,
    result: "dry_run",
    error: null,
    created_at: "2026-11-15T10:00:00Z",
    ...o,
  };
}
