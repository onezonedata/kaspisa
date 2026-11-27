-- Роли базы (ADR 0023). Образ выполняет файл один раз, на пустом томе, от суперпользователя
-- в базе kaspisa (её создал образ по POSTGRES_DB); следом он выполняет grants.sql.
-- Это .sql, а не .sh: образ выполняет .sh только с битом исполнения, а на хостах он не гарантирован.
--
-- Первые команды сессии отключают запись текста запроса в журнал сервера: если CREATE ROLE упадёт,
-- пароль в журнал не попадёт. Пароли читаются из окружения командой \getenv, а не из аргументов psql.
SET log_min_error_statement = panic;
SET log_statement = 'none';
\getenv owner_password KASPISA_DB_OWNER_PASSWORD
\getenv app_password KASPISA_DB_APP_PASSWORD

CREATE ROLE kaspisa LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'owner_password';
CREATE ROLE kaspisa_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
ALTER DATABASE kaspisa OWNER TO kaspisa;
REVOKE ALL ON DATABASE kaspisa FROM PUBLIC;
