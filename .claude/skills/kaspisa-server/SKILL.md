---
name: kaspisa-server
description: Подготовка сервера Ubuntu или Debian для kaspisa (Docker, ufw), вход по ключу, смена порта SSH, автообновления, fail2ban и уведомление о входе по SSH. Все шаги выполняет владелец от root, агент готовит команды. Используй, когда владелец просит подготовить или защитить сервер.
---

# Подготовка и защита сервера

Правила А1–А13 — в `AGENTS.md` корня репозитория. Здесь порядок шагов и точки остановки.

Все шаги выполняются от root. Агент под пользователем `kaspisa` их не выполняет (А9): он проверяет порядок, отдаёт владельцу команды текстом и ждёт подтверждения. Если `id -u` равен 0, агент останавливается и ничего не выполняет.

Перед шагами 2 и 3 владелец держит открытой вторую сессию SSH. Шаг 1 включает ufw, поэтому вторая сессия открывается и до него. Перед шагом 1 и перед каждым шагом, меняющим sshd, порт или ufw, агент спрашивает владельца, открыта ли консоль хостера и есть ли в ней рабочий вход (на образах без пароля консоль бесполезна), и ждёт «да». Агент просит владельца проверять вход после каждого шага, который меняет доступ.

## Шаг 1. Подготовка сервера

Команда от root (флаги по режиму, `--public` для `public`, `--tailscale` при необходимости):

```
bash deploy/prepare-server.sh
```

Затем `usermod -aG docker <пользователь>` и новый вход. Подробности — `docs/deploy.md`, раздел 1. Скрипт сам берёт порты SSH из работающего sshd; если порт не определён, ufw не включается.

## Шаг 2. SSH только по ключу

1. Владелец подтверждает, что вход по ключу работает во второй сессии.
2. Проверь, нет ли перекрывающих настроек:

   ```
   grep -nE '^[[:space:]]*(Match|AllowUsers|AllowGroups|DenyUsers)' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf
   ```

   Если есть совпадения, остановись и покажи их владельцу.
3. Владелец создаёт `/etc/ssh/sshd_config.d/00-kaspisa.conf`:

   ```
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitRootLogin prohibit-password
   ```

   Номер `00` опережает `50-cloud-init.conf`: действует первое найденное значение.
4. `sshd -t` без ошибок.
5. `sshd -T -C user=<пользователь>,host=<имя>,addr=<адрес владельца>`: в выводе `passwordauthentication no` и `kbdinteractiveauthentication no`. Это и есть проверка отказа по паролю. Живую попытку с паролем не делаем.
6. Только после шагов 4 и 5: перезапуск службы (`systemctl restart ssh`; на Ubuntu 24.04 служба называется `ssh`).
7. Владелец входит новым окном по ключу. Вторую сессию он закрывает только после этого.

## Шаг 3. Смена порта SSH

Новый порт называет владелец: ниже 1024, не занятый (`ss -ltn`), не 80 и не 443: порт выше 1024 может занять любой процесс, пока sshd перезапускается. Порядок обязателен, чтобы не потерять доступ:

1. `ufw allow <новый>/tcp`. Если у хостера есть свой файрвол, открыть порт и там (см. п. 6).
2. Если стоит fail2ban: в `/etc/fail2ban/jail.d/kaspisa.local` указать `port = <старый>,<новый>`, затем `fail2ban-client reload`.
3. sshd слушает оба порта. В `00-kaspisa.conf` — `Port <старый>` и `Port <новый>`. Затем `sshd -t` и `systemctl daemon-reload`. На Ubuntu 24.04 (openssh 9.6, `ssh.socket` включён) порт задаёт сокет, а генератор `sshd-socket-generator` сам переносит строки `Port` в сокет при `daemon-reload`: проверь, что `/run/systemd/generator/ssh.socket.d/addresses.conf` содержит оба порта. Ручной override (`systemctl edit ssh.socket`) не нужен.
4. Перезапуск сокета: `systemctl restart ssh.socket`.
5. `ss -ltn` показывает оба порта; `ufw status` показывает новый порт разрешённым.
6. Перед этим спроси владельца о сетевом фильтре провайдера: открыт ли новый порт там. На стенде фильтр был, хотя владелец ответил «нет». Владелец входит по новому порту вторым окном и подтверждает вход. Без подтверждения шаги 7–9 не выполняются. Если вход не проходит, а `ss` показывает порт, проверь счётчик правила: `nft list ruleset | grep 'dport <новый>'`. Ноль пакетов значит, что фильтр стоит до сервера.
7. Убрать старый порт из конфигурации sshd или сокета, `sshd -t`, перезапуск, `ss -ltn`, снова вход по новому порту.
8. Если стоит fail2ban: `port = <новый>` в `jail.d/kaspisa.local`, `fail2ban-client reload`.
9. `ufw delete allow <старый>/tcp`. Повторный запуск `prepare-server.sh` старое правило ufw не удаляет.

## Шаг 4. Автообновления ОС

```
apt-get install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades
```

Автоматическую перезагрузку не включать: владелец перезагружает сервер сам. Проверить два файла: в `/etc/apt/apt.conf.d/20auto-upgrades` есть строка `APT::Periodic::Unattended-Upgrade "1";`, в `/etc/apt/apt.conf.d/50unattended-upgrades` строка `Unattended-Upgrade::Automatic-Reboot` равна `"false"` или закомментирована. На Ubuntu по умолчанию ставятся только обновления безопасности. На Debian 12 стандартный файл включает ещё источник `label=Debian` — обновления точечных выпусков; их оставляем. Сервисы kaspisa поднимаются сами после перезагрузки (`restart: unless-stopped`).

## Шаг 5. fail2ban

```
apt-get install -y fail2ban
```

Файл `/etc/fail2ban/jail.d/kaspisa.local`:

```
[sshd]
enabled = true
port = <фактические порты SSH>
backend = systemd
```

Порты — строки `port` в выводе `sshd -T`. Остальные параметры — по умолчанию пакета. Проверка: `fail2ban-client status sshd`. На Ubuntu 24.04 пакет уже содержит `/etc/fail2ban/jail.d/defaults-debian.conf` (`backend = systemd`, jail `sshd` включён); свой файл всё равно нужен для порта. Проверка, что jail видит журнал: после неудачного входа (несуществующий пользователь) счётчик `Total failed` в `fail2ban-client status sshd` растёт.

## Шаг 6. Уведомление о входе по SSH

Канал — Telegram. Бота создаёт владелец сам. Токен и идентификатор чата владелец записывает в `/etc/kaspisa-ssh-notify.curl` (root, права 600) — файл создаётся сразу с этими правами командой `install -m 600 -o root -g root /dev/null /etc/kaspisa-ssh-notify.curl`, затем заполняется редактором в своём терминале, не в чат (А5):

```
url = "https://api.telegram.org/bot***/sendMessage"
data = "chat_id=***"
```

Скрипт `/usr/local/sbin/kaspisa-ssh-notify` (владелец root, права 755: `install -m 755 -o root -g root /dev/null /usr/local/sbin/kaspisa-ssh-notify`, затем редактор) — текст в `docs/install.md`, путь 2. В `/etc/pam.d/sshd` добавляется строка:

```
session optional pam_exec.so /usr/local/sbin/kaspisa-ssh-notify
```

`optional` означает, что сбой скрипта не мешает входу. Сообщение приходит на каждый сеанс, включая `scp`, туннель и сеансы агента: агент не опрашивает сервер частыми SSH-командами. Строка `data` начинается с `chat_id=` (иначе Telegram отвечает `chat_id is empty`); номер группы начинается с минуса. Проверка без вывода токена, владелец запускает сам: `curl -sS --max-time 5 -K /etc/kaspisa-ssh-notify.curl --data-urlencode "text=проверка" | grep -o '"ok":[a-z]*'` — вывод должен быть `"ok":true`. После настройки владелец входит новым окном и проверяет, что сообщение пришло.

## Что не проверено

Шаги 2–6 и строка одной командой (`docs/install.md`) проверены на чистом Ubuntu 24.04. Ubuntu 22.04 и Debian 12 не проверены: порядок шага 3 там (без `ssh.socket`) на стенде не проверялся.
