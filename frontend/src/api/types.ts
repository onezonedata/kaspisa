// Короткие псевдонимы типов из сгенерированной схемы OpenAPI.
import type { components } from "./schema";

type S = components["schemas"];

export type Shop = S["ShopOut"];
export type Product = S["ProductOut"];
export type PendingWrite = S["PendingWrite"];
export type PriceChange = S["PriceChangeOut"];
export type ConnectResult = S["ConnectResult"];
export type CatalogSummaryOut = S["CatalogSummaryOut"];
export type SyncResult = S["SyncResult"];
export type ShopSettingsIn = S["ShopSettingsIn"];
export type ProductSettingsIn = S["ProductSettingsIn"];
export type BulkChanges = S["BulkChanges"];
export type Strategy = S["Strategy"];
export type BotMode = S["BotMode"];
export type OwnerEvent = S["OwnerEventOut"];
export type OwnerEventKind = S["OwnerEventKind"];
export type ErrorOut = S["ErrorOut"];
export type User = S["UserOut"];
export type VersionOut = S["VersionOut"];
export type TelegramSettings = S["TelegramSettingsOut"];
export type TelegramSettingsIn = S["TelegramSettingsIn"];
export type ProductPage = S["ProductPage"];
export type PriceChangePage = S["PriceChangePage"];
export type UserAdmin = S["UserAdminOut"];
export type UserAdded = S["UserAddedOut"];
