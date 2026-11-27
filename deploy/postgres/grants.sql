-- Права роли приложения kaspisa_app (ADR 0023): только DML, таблицами не владеет.
-- Повторяемая часть: образ выполняет файл при init (в базе kaspisa), восстановление копии
-- выполняет его от владельца kaspisa после pg_restore. Суперпользователь не нужен.
GRANT CONNECT ON DATABASE kaspisa TO kaspisa_app;
GRANT USAGE ON SCHEMA public TO kaspisa_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO kaspisa_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kaspisa_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kaspisa IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kaspisa_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kaspisa IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO kaspisa_app;
