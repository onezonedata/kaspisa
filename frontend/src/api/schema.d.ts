export interface paths {
    "/api/auth/confirm-password": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Confirm Password
         * @description Повторный ввод пароля перед опасной правкой границ (ADR 0024).
         *
         *     Счётчик растёт до проверки и считается по пользователю: украденную cookie нельзя размножить
         *     на сессии. Верный пароль открывает окно на 10 минут для токена этой сессии.
         */
        post: operations["confirm_password"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Login */
        post: operations["login"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/logout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Logout
         * @description Выход: завершает только текущую сессию (ADR 0012).
         */
        post: operations["logout"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/logout-all": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Logout All
         * @description Выход на всех устройствах: завершает все сессии учётной записи (ADR 0012).
         */
        post: operations["logout_all"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/me": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Me */
        get: operations["me"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/password-reset": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Password Reset */
        post: operations["password_reset"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/register": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Register */
        post: operations["register"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Events */
        get: operations["list_events"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notifications/telegram": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Settings */
        get: operations["get_settings"];
        /** Save Settings */
        put: operations["save_settings"];
        post?: never;
        /** Delete Settings */
        delete: operations["delete_settings"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notifications/telegram/test": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Check Settings
         * @description Синхронная проверка сохранённых настроек: одно проверочное сообщение.
         */
        post: operations["check_settings"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Shops */
        get: operations["list_shops"];
        put?: never;
        /**
         * Add Shop
         * @description Один магазин на арендатора (ADR 0029): проверка под блокировкой строки арендатора.
         */
        post: operations["add_shop"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Shop */
        get: operations["get_shop"];
        put?: never;
        post?: never;
        /** Delete Shop */
        delete: operations["delete_shop"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/catalog-summary": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Catalog Summary
         * @description Число товаров и отслеживаемых: счёт в базе, без предела каталога.
         */
        get: operations["catalog_summary"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/code": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Connect Code */
        post: operations["connect_code"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/code/resend": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Connect Resend */
        post: operations["connect_resend"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/credentials": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Change Credentials */
        put: operations["change_credentials"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/halt/clear": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Clear Halt */
        post: operations["clear_halt"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Connect Login */
        post: operations["connect_login"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/login/restart": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Connect Restart */
        post: operations["connect_restart"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/price-changes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Price Changes */
        get: operations["list_price_changes"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/products": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Products */
        get: operations["list_products"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Update Products */
        patch: operations["update_products"];
        trace?: never;
    };
    "/api/shops/{shop_id}/products/sync": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Sync Products */
        post: operations["sync_products"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/products/{product_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Product */
        get: operations["get_product"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /**
         * Update Product
         * @description Границы меняются под блокировкой строки товара: новая действует со следующей записи.
         *
         *     Опасное снижение минимума (ADR 0024) требует подтверждённого пароля: проверка идёт по
         *     значениям заблокированной строки и до любых присваиваний.
         */
        patch: operations["update_product"];
        trace?: never;
    };
    "/api/shops/{shop_id}/products/{product_id}/price-read": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Read Prices
         * @description «Узнать цены» (ADR 0030, 0033): разовое чтение публичной карточки без кабинета и записи.
         *
         *     Пишет поля наблюдения товара (как обработчик) и сбрасывает решение бота по правилу правки
         *     товара; журнал, пропуск, отказ и настройки не трогает. Одно чтение на магазин (блокировка
         *     в Redis), повтор по товару - не чаще `PRICE_READ_REPEAT_SECONDS`; запрос идёт через общий
         *     ограничитель `kaspi.kz`. Таймер повтора остаётся, только если запрос мог уйти в Kaspi.
         *     Если город сверки магазина не задан, карточка читается в Астане (ADR 0031).
         */
        post: operations["read_prices"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/shops/{shop_id}/settings": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /** Update Settings */
        patch: operations["update_settings"];
        trace?: never;
    };
    "/api/version": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Version
         * @description Текущая версия и признак новой; нужна сессия.
         */
        get: operations["get_version"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Health
         * @description Проверка, что сервис запущен.
         */
        get: operations["health"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * BotMode
         * @description Режим бота магазина (ADR 0024): выключен, проверка без записи, работает.
         * @enum {string}
         */
        BotMode: "off" | "dry_run" | "live";
        /**
         * BulkChanges
         * @description Массово меняются только эти поля; границы цены - у каждого товара отдельно.
         */
        BulkChanges: {
            /**
             * Auto Decrease
             * @default false
             */
            auto_decrease?: boolean;
            /**
             * Auto Raise
             * @default false
             */
            auto_raise?: boolean;
            /**
             * Step
             * @default 1
             */
            step?: number;
            /**
             * Tracked
             * @default false
             */
            tracked?: boolean;
        };
        /** BulkProductSettingsIn */
        BulkProductSettingsIn: {
            changes: components["schemas"]["BulkChanges"];
            /** Product Ids */
            product_ids: number[];
        };
        /** BulkResult */
        BulkResult: {
            /** Updated */
            updated: number;
        };
        /**
         * CatalogSummaryOut
         * @description Число товаров магазина; оба `None`, если каталог больше предела.
         */
        CatalogSummaryOut: {
            /** Total */
            total: number | null;
            /** Tracked */
            tracked: number | null;
        };
        /** CodeIn */
        CodeIn: {
            /** Code */
            code: string;
        };
        /** ConnectResult */
        ConnectResult: {
            /**
             * Outcome
             * @enum {string}
             */
            outcome: "connected" | "code_required" | "code_sent" | "code_invalid" | "rejected" | "temporary_error" | "restart_required";
            /** Retry After Seconds */
            retry_after_seconds: number | null;
            shop: components["schemas"]["ShopOut"];
        };
        /** ErrorOut */
        ErrorOut: {
            /** Code */
            code: string;
            /**
             * Fields
             * @description Пути полей без значений (422)
             */
            fields?: string[] | null;
            /** Retry After Seconds */
            retry_after_seconds?: number | null;
        };
        /** LoginIn */
        LoginIn: {
            /** Email */
            email: string;
            /** Password */
            password: string;
        };
        /**
         * OwnerEventKind
         * @description Вид события журнала действий владельца (ADR 0024).
         * @enum {string}
         */
        OwnerEventKind: "login" | "password_confirm_failed" | "bot_mode_changed" | "bounds_changed";
        /**
         * OwnerEventOut
         * @description Событие журнала действий владельца: без паролей, токенов и cookie.
         */
        OwnerEventOut: {
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Id */
            id: number;
            /** Ip */
            ip: string;
            kind: components["schemas"]["OwnerEventKind"];
            /** New Value */
            new_value: string | null;
            /** Old Value */
            old_value: string | null;
            /** Shop Id */
            shop_id: string | null;
            /** Sku */
            sku: string | null;
        };
        /**
         * PasswordConfirmIn
         * @description Повторный ввод пароля сервиса перед опасной правкой (ADR 0024).
         */
        PasswordConfirmIn: {
            /** Password */
            password: string;
        };
        /** PasswordResetIn */
        PasswordResetIn: {
            /** New Password */
            new_password: string;
            /** Token */
            token: string;
        };
        /**
         * PendingWrite
         * @description Открытая запись цены: новая граница действует со следующей записи.
         */
        PendingWrite: {
            /**
             * Hold Until
             * Format: date-time
             */
            hold_until: string;
            /** Outside Bounds */
            outside_bounds: boolean;
            /** Prices */
            prices: {
                [key: string]: number;
            };
            /**
             * Sent At
             * Format: date-time
             */
            sent_at: string;
            /** Status */
            status: string;
        };
        /** PriceChangeOut */
        PriceChangeOut: {
            /** Capped At Max */
            capped_at_max: boolean;
            /** Cities Aligned */
            cities_aligned: boolean;
            /** Competitor Merchant Id */
            competitor_merchant_id: string | null;
            /** Competitor Name */
            competitor_name: string | null;
            /** Competitor Price */
            competitor_price: number | null;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Error */
            error: string | null;
            /** Id */
            id: number;
            /** New Price */
            new_price: number;
            /** Old Price */
            old_price: number;
            /** Product Id */
            product_id: number;
            /** Reason */
            reason: string;
            /** Result */
            result: string;
            /** Skipped Competitors */
            skipped_competitors: number;
            /** Sku */
            sku: string;
            /** Target Price */
            target_price: number | null;
            /** Title */
            title: string;
        };
        /** PriceChangePage */
        PriceChangePage: {
            /** Items */
            items: components["schemas"]["PriceChangeOut"][];
            /** Limit */
            limit: number;
            /** Offset */
            offset: number;
        };
        /** ProductOut */
        ProductOut: {
            /** Auto Decrease */
            auto_decrease: boolean;
            /** Auto Raise */
            auto_raise: boolean;
            /** Competitors Count */
            competitors_count: number | null;
            /** Current Price */
            current_price: number | null;
            /** Decision Reason */
            decision_reason: string | null;
            /** Decision Target Price */
            decision_target_price: number | null;
            /** Decision Write */
            decision_write: string | null;
            /** First Place Price */
            first_place_price: number | null;
            /** Id */
            id: number;
            /** Last Checked At */
            last_checked_at: string | null;
            /** Master Sku */
            master_sku: string;
            /** Max Price */
            max_price: number | null;
            /** Min Price */
            min_price: number | null;
            pending_write: components["schemas"]["PendingWrite"] | null;
            /** Position */
            position: number | null;
            /** Position Over */
            position_over: number | null;
            /** Refusal Reason */
            refusal_reason: string | null;
            /** Refusal Since */
            refusal_since: string | null;
            /** Retry At */
            retry_at: string | null;
            /**
             * Shop Id
             * Format: uuid
             */
            shop_id: string;
            /** Skip Reason */
            skip_reason: string | null;
            /** Skipped At */
            skipped_at: string | null;
            /** Sku */
            sku: string;
            /** Step */
            step: number;
            /** Title */
            title: string;
            /** Tracked */
            tracked: boolean;
        };
        /** ProductPage */
        ProductPage: {
            /** Items */
            items: components["schemas"]["ProductOut"][];
            /** Total */
            total: number;
        };
        /**
         * ProductSettingsIn
         * @description Частичная правка товара: поле, которого нет в теле, не меняется.
         */
        ProductSettingsIn: {
            /**
             * Auto Decrease
             * @default false
             */
            auto_decrease?: boolean;
            /**
             * Auto Raise
             * @default false
             */
            auto_raise?: boolean;
            /** Max Price */
            max_price?: number | null;
            /** Min Price */
            min_price?: number | null;
            /**
             * Step
             * @default 1
             */
            step?: number;
            /**
             * Tracked
             * @default false
             */
            tracked?: boolean;
        };
        /** RegisterIn */
        RegisterIn: {
            /** Email */
            email: string;
            /** Password */
            password: string;
        };
        /** ShopCredentialsIn */
        ShopCredentialsIn: {
            /** Login */
            login: string;
            /** Password */
            password: string;
        };
        /** ShopOut */
        ShopOut: {
            /** Auto Raise Enabled */
            auto_raise_enabled: boolean;
            bot_mode: components["schemas"]["BotMode"];
            /** Catalog Loading */
            catalog_loading: boolean;
            /** Catalog Synced At */
            catalog_synced_at: string | null;
            /** City Id */
            city_id: string | null;
            /**
             * Connection Changed At
             * Format: date-time
             */
            connection_changed_at: string;
            /** Connection Error */
            connection_error: string | null;
            /** Connection State */
            connection_state: string;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Default Step */
            default_step: number;
            /** Excluded Merchant Ids */
            excluded_merchant_ids: string[];
            /** Halt Reason */
            halt_reason: string | null;
            /** Halted At */
            halted_at: string | null;
            /**
             * Id
             * Format: uuid
             */
            id: string;
            /** Merchant Uid */
            merchant_uid: string | null;
            /** Resend Available In Seconds */
            resend_available_in_seconds: number | null;
            /** Restart Required */
            restart_required: boolean;
            /** Server Dry Run */
            server_dry_run: boolean | null;
            /** Strategy */
            strategy: string;
        };
        /**
         * ShopSettingsIn
         * @description Частичная правка: поле, которого нет в теле, не меняется (по `model_fields_set`).
         */
        ShopSettingsIn: {
            /**
             * Auto Raise Enabled
             * @default true
             */
            auto_raise_enabled?: boolean;
            /** @default off */
            bot_mode?: components["schemas"]["BotMode"];
            /** City Id */
            city_id?: string | null;
            /**
             * Default Step
             * @default 1
             */
            default_step?: number;
            /** Excluded Merchant Ids */
            excluded_merchant_ids?: string[];
            /** @default be_first */
            strategy?: components["schemas"]["Strategy"];
        };
        /**
         * Strategy
         * @description Стратегия магазина.
         * @enum {string}
         */
        Strategy: "be_first" | "match_first" | "hold_second";
        /** SyncResult */
        SyncResult: {
            /** Added */
            added: number;
            /** Missing */
            missing: number;
            /**
             * Outcome
             * @enum {string}
             */
            outcome: "synced" | "rejected" | "temporary_error";
            /** Retry After Seconds */
            retry_after_seconds: number | null;
            shop: components["schemas"]["ShopOut"];
            /** Updated */
            updated: number;
        };
        /** TelegramSettingsIn */
        TelegramSettingsIn: {
            /** Chat Id */
            chat_id: string;
            /** Token */
            token?: string | null;
        };
        /** TelegramSettingsOut */
        TelegramSettingsOut: {
            /** Chat Id */
            chat_id: string | null;
            /** Token Set */
            token_set: boolean;
        };
        /** UserOut */
        UserOut: {
            /** Email */
            email: string;
            /**
             * Id
             * Format: uuid
             */
            id: string;
        };
        /** VersionOut */
        VersionOut: {
            /** Current */
            current: string;
            /** Latest */
            latest: string | null;
            /** Release Url */
            release_url: string | null;
            /** Update Available */
            update_available: boolean;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    confirm_password: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PasswordConfirmIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    login: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LoginIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UserOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    logout: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    logout_all: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    me: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UserOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    password_reset: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PasswordResetIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    register: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RegisterIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UserOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_events: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OwnerEventOut"][];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_settings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TelegramSettingsOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    save_settings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TelegramSettingsIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TelegramSettingsOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_settings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    check_settings: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_shops: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ShopOut"][];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    add_shop: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ShopCredentialsIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ShopOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_shop: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ShopOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_shop: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    catalog_summary: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CatalogSummaryOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    connect_code: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CodeIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConnectResult"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    connect_resend: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConnectResult"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    change_credentials: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ShopCredentialsIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ShopOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    clear_halt: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ShopOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    connect_login: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConnectResult"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    connect_restart: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConnectResult"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_price_changes: {
        parameters: {
            query?: {
                product_id?: number | null;
                limit?: number;
                offset?: number;
            };
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PriceChangePage"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_products: {
        parameters: {
            query?: {
                q?: string | null;
                tracked?: boolean | null;
                has_bounds?: boolean | null;
                has_refusal?: boolean | null;
                limit?: number;
                offset?: number;
            };
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ProductPage"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    update_products: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BulkProductSettingsIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BulkResult"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    sync_products: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SyncResult"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_product: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
                product_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ProductOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    update_product: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
                product_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ProductSettingsIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ProductOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    read_prices: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
                product_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ProductOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    update_settings: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                shop_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ShopSettingsIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ShopOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_version: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VersionOut"];
                };
            };
            /** @description Ошибка */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    health: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: string;
                    };
                };
            };
        };
    };
}
