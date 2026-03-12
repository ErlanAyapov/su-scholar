# WebSocket Deploy (Django Channels)

Если локально WebSocket работает, а на сервере нет, обычно причина в том, что прод-сервер запущен только как WSGI (`gunicorn project.wsgi:application`).

Для Channels нужен ASGI-сервер. Рекомендуемая схема:

- `gunicorn` обслуживает HTTP (`127.0.0.1:8000`)
- `daphne` обслуживает WebSocket (`127.0.0.1:8001`)
- `nginx` проксирует `/ws/` на `daphne`, остальное на `gunicorn`

## 1) Установить зависимости

```bash
source /home/yerlan/project/venv/bin/activate
cd /home/yerlan/project/su_science
pip install -r requirements.txt
```

## 2) Подключить systemd сервис daphne

Скопируйте шаблон:

`/home/yerlan/project/su_science/.dist/systemd/daphne.service.example`

в:

`/etc/systemd/system/daphne.service`

и выполните:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now daphne
sudo systemctl status daphne
```

## 3) Настроить nginx для WebSocket

Добавьте в server-блок nginx:

```nginx
location /ws/ {
    proxy_pass http://127.0.0.1:8001;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
    proxy_buffering off;
}
```

А в `http`-контекст nginx добавьте `map`:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
```

Проверка и перезапуск:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 4) Проверки

```bash
sudo journalctl -u daphne -f
sudo journalctl -u nginx -f
```

В браузере на странице:

`/admin/account/user/bulk-operations/`

статус должен стать `WebSocket: online`.

