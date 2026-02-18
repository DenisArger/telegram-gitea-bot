# Telegram Webhook Bot for Gitea Integration

## Описание

Приложение представляет собой вебхук-сервер, который интегрирует события из системы управления репозиториями **Gitea** с **Telegram**. Приложение обрабатывает уведомления о pull request', issue, отправляя соответствующие сообщения в указанный Telegram-канал.

Основные функции:
- Обработка событий Gitea.
- Отправка уведомлений в Telegram с использованием кнопок для быстрого перехода к pull request.
- Сопоставление пользователей Gitea с их Telegram-аккаунтами для персонализированных уведомлений.

## Требования

Для запуска приложения необходимы следующие компоненты:
- Python 3.9+
- Библиотеки Python, указанные в `requirements.txt`:
  - Flask
  - python-telegram-bot
  - uvicorn
  - asgiref

## Переменные окружения

Проект использует файл `.env`. Создайте его на основе `.env.example` и укажите свои значения:

- `TELEGRAM_BOT_TOKEN` — токен бота Telegram.
- `TELEGRAM_TARGET_CHAT_ID` — ID чата/канала для уведомлений (например, `-100...`).
- `TELEGRAM_MESSAGE_THREAD_ID` — ID темы в форуме Telegram.

## Деплой на Vercel

1. Установите переменные окружения в настройках проекта на Vercel:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_TARGET_CHAT_ID`
   - `TELEGRAM_MESSAGE_THREAD_ID`
2. Убедитесь, что есть `vercel.json` и `api/index.py` (уже добавлены).
3. В Gitea обновите webhook URL на ваш домен Vercel.

## Настройка Gitea

1. Создайте webhook в Gitea.
2. Укажите URL вашего сервера (например, `https://your-domain.com/`).
3. Выберите `application/json`.
4. Включите нужные события (`pull_request`, `issue_comment` и другие по вашему процессу).

## Локальный запуск

```bash
git clone https://github.com/DenisArger/telegram-gitea-bot.git
cd telegram-gitea-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python TelegramWebhookBot.py
```

## Примечание по пользователям

Для добавления новых соответствий Gitea ↔ Telegram обновите файл `users.json`.
