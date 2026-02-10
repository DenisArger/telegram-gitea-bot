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

  ## Настройка Gitea

1. Создайте webhook в Gitea:
   - Перейдите в настройки репозитория в Gitea.
   - Добавьте новый webhook с URL вашего сервера (например, `http://your-domain.com/`).
   - Выберите тип контента `application/json`.
   - Укажите события, которые должны вызывать webhook (например, pull requests, issues).

   ## Установка

   - git clone http://172.18.56.92:3000/NikitaHalukh/telegram_bot
   - перейти в котолог проекта
   - python TelegramWebhookBot.py или python3 TelegramWebhookBot.py
   
## На момент написания Readme файла не исправен
    Необходимо настроить мост между ботом и сервисом Gitea
    При необходимости добавления новых пользователей, достаточно дополнить массив пользователей rr_of_users
