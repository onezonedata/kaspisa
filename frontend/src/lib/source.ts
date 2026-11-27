// Ссылка на исходный код установки (AGPL-3.0, §13; docs/deploy.md).
// Кто изменил код и даёт им пользоваться другим, меняет адрес на свой.
// Если в его репозитории нет метки v<версия>, меняется и SOURCE_HREF.
import pkg from "../../package.json";

export const SOURCE_REPO_URL = "https://github.com/onezonedata/kaspisa";
export const APP_VERSION: string = pkg.version;
export const SOURCE_HREF = `${SOURCE_REPO_URL}/tree/v${APP_VERSION}`;
