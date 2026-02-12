# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from typing import Dict, Optional
from dotenv import load_dotenv
import asyncio
import json
import os
import random
import time
import traceback

class TelegramWebhookBot:
    def __init__(self):
        load_dotenv()

        # Конфигурация
        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.TARGET_CHAT_ID = os.getenv("TELEGRAM_TARGET_CHAT_ID")
        message_thread_id = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")

        if not self.TOKEN or not self.TARGET_CHAT_ID or not message_thread_id:
            raise ValueError(
                "Missing required environment variables: "
                "TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET_CHAT_ID, TELEGRAM_MESSAGE_THREAD_ID"
            )

        self.MESSAGE_THREAD_ID = int(message_thread_id)

        # Массив пользователей (сопоставление имен из Gitea с Telegram)
        self.arr_of_users = self.load_users()
        # Throttle for review comment notifications: key -> next_allowed_epoch
        self.review_comment_throttle: Dict[str, float] = {}
        self.review_comment_interval_seconds = 30 * 60

        # Инициализация Flask и Telegram Bot
        self.app = Flask(__name__)
        self.bot = Bot(token=self.TOKEN)

        # Регистрация маршрута для webhook
        self.app.route("/", methods=["POST"])(self.webhook)
        # Health-check for platform probes
        self.app.route("/", methods=["GET"])(self.health_check)
        # Weekly reminder endpoint (use with cron)
        self.app.route("/weekly-reminder", methods=["GET"])(self.weekly_reminder)

    def health_check(self):
        return "ok", 200

    def load_users(self):
        users_path = os.path.join(os.path.dirname(__file__), "users.json")
        try:
            with open(users_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("users.json должен содержать массив пользователей")
            return data
        except Exception as e:
            raise ValueError(f"Не удалось загрузить users.json: {e}")

    def weekly_reminder(self):
        try:
            message = self.build_weekly_reminder_message()
            asyncio.run(self.send_plain_message(message))
            return jsonify({"status": "success"}), 200
        except Exception as e:
            print(f"Произошла ошибка при отправке напоминания: {e}")
            traceback.print_exc()
            return jsonify({"status": "error", "message": str(e)}), 500

    def run(self, host: str = "127.0.0.1", port: int = 3333):
        """Запуск сервера."""
        self.app.run(host=host, port=port)  # Явно указываем локальный адрес (127.0.0.1)

    def webhook(self):
        """Обработчик входящих webhook-данных."""
        try:
            data = request.json
            print("Incoming webhook data:", data)

            # Проверка входящих данных
            if not data or not isinstance(data, dict):
                print("Некорректные входящие данные:", data)
                return jsonify({"status": "error", "message": "Bad Request"}), 400

            rep_user_name = data.get("sender", {}).get("login")
            if not rep_user_name:
                print("Отсутствует имя пользователя:", data)
                return jsonify({"status": "error", "message": "Missing sender login"}), 400

            main_user = next((user for user in self.arr_of_users if user["repName"] == rep_user_name), None)
            if not main_user:
                print(f"Пользователь не найден: {rep_user_name}")
                return jsonify({"status": "error", "message": "User not found"}), 400

            action = data.get("action")
            repo_name = data.get("repository", {}).get("name")
            branch = None
            rep_link = None

            # Определение ссылки на pull request или issue
            if data.get("pull_request"):
                rep_link = data["pull_request"].get("html_url")
                branch = data.get("pull_request", {}).get("head", {}).get("ref")
                if not branch:
                    # Some Gitea events provide a reduced pull_request object without head/ref
                    branch = data.get("pull_request", {}).get("title") or data.get("issue", {}).get("title")
            elif data.get("issue") and data.get("comment"):
                if data["issue"].get("pull_request"):
                    rep_link = data["issue"]["pull_request"].get("html_url")
                else:
                    rep_link = data["issue"].get("url")
                branch = data["issue"].get("title")

            if not rep_link or not branch:
                print("Неизвестная структура данных:", data)
                return jsonify({"status": "error", "message": "Unknown data structure"}), 400

            asyncio.run(
                self.process_event(action, data, main_user, repo_name, branch, rep_link)
            )

            return jsonify({"status": "success"}), 200

        except Exception as e:
            print(f"Произошла ошибка: {e}")
            traceback.print_exc()
            return jsonify({"status": "error", "message": str(e)}), 500

    async def send_telegram_message(self, message: str, rep_link: str):
        """Отправка сообщения в Telegram."""
        if not message or not rep_link:
            print("Недостаточно данных для отправки сообщения:", {"message": message, "rep_link": rep_link})
            return

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Перейти к pull_request", url=rep_link)]
        ])

        try:
            await self.bot.send_message(
                chat_id=self.TARGET_CHAT_ID,
                text=message,
                reply_markup=keyboard,
                message_thread_id=self.MESSAGE_THREAD_ID
            )
            print("Сообщение отправлено успешно:", {"message": message, "rep_link": rep_link})
        except Exception as e:
            print("Ошибка отправки сообщения:", e)

    async def send_plain_message(self, message: str):
        """Отправка сообщения в Telegram без кнопок."""
        if not message:
            print("Пустое сообщение для отправки.")
            return

        try:
            await self.bot.send_message(
                chat_id=self.TARGET_CHAT_ID,
                text=message,
                message_thread_id=self.MESSAGE_THREAD_ID
            )
            print("Сообщение отправлено успешно:", {"message": message})
        except Exception as e:
            print("Ошибка отправки сообщения:", e)

    def build_weekly_reminder_message(self) -> str:
        templates = [
            "🔔 Пятничный чек‑ин: не забудьте заполнить отчет за прошедшую неделю.",
            "🗓️ Финал недели: пора заполнить отчеты. Заполните, пожалуйста, форму.",
            "✅ Последний штрих пятницы — отчет о работе за неделю. Заполните сегодня.",
            "📌 Напоминание: отчет за прошедшую неделю ждёт вашего участия.",
            "✍️ Пятничная рутина: внесите результаты недели в отчет.",
            "🚀 Чтобы уйти на выходные спокойно — заполните недельный отчет.",
            "🧾 Отчетная пятница: обновите данные о проделанной работе.",
            "📣 Коллеги, внимание: отчет за неделю нужно заполнить сегодня.",
            "⏳ До завершения недели осталось чуть-чуть — заполните отчет, пожалуйста.",
            "🔍 Итоги недели: пришло время заполнить отчет.",
        ]
        mentions = " ".join([f"@{user['tgName']}" for user in self.arr_of_users if user.get("tgName")])
        return (
            f"{random.choice(templates)}\n"
            f"{mentions}"
        )

    async def process_event(self, action: str, data: Dict, main_user: Dict, repo_name: str, branch: str, rep_link: str):
        # Обработка событий
        if action == "review_requested":
            await self.handle_review_requested_event(data, main_user, repo_name, branch, rep_link)
        elif action == "review_request_removed":
            await self.handle_review_request_removed_event(data, main_user, repo_name, branch, rep_link)
        elif action in ["closed", "reopened", "created", "deleted", "synchronized"]:
            await self.handle_generic_event(action, data, main_user, repo_name, branch, rep_link)
        elif action == "reviewed":
            await self.handle_reviewed_event(data, main_user, repo_name, branch, rep_link)
        elif action == "assigned":
            print("Назначение рецензентов (assigned) игнорируется по настройке уведомлений.")
        elif action == "unassigned":
            print("Снятие рецензентов (unassigned) игнорируется по настройке уведомлений.")
        elif action == "opened":
            print("Открытие PR игнорируется по настройке уведомлений.")
        elif action == "edited":
            print("Редактирование комментария (edited) игнорируется по настройке уведомлений.")
        elif action == "updated":
            print("Обновление комментария (updated) игнорируется по настройке уведомлений.")
        else:
            print(f"Неизвестное действие: {action}")

    async def handle_assigned_event(self, data: Dict, main_user: Dict, repo_name: str, branch: str, rep_link: str):
        """Обработка назначения рецензентов."""
        assignees = data.get("pull_request", {}).get("assignees", [])
        print("Incoming assignees data:", assignees)  # Логируем данные о назначенных рецензентах

        if not assignees:
            print("Список рецензентов пуст:", data)
            return

        new_reviewers = [assignee for assignee in assignees if assignee["login"] != main_user["repName"]]
        if not new_reviewers:
            print("Нет новых рецензентов для обработки:", data)
            return

        for reviewer in new_reviewers:
            reviewer_tg_name = next(
                (user["tgName"] for user in self.arr_of_users if user["repName"] == reviewer["login"]), None)
            if reviewer_tg_name:
                message = (
                    f"🔔 @{reviewer_tg_name}\n"
                    f"🫡 Проверь, пожалуйста, запрос на слияние #{data['pull_request']['number']}\n"
                    f"Ветка: {branch}"
                )
                await self.send_telegram_message(message, rep_link)
            else:
                print(f"Рецензент не найден: {reviewer['login']}")

    async def handle_unassigned_event(self, data: Dict, main_user: Dict, repo_name: str, branch: str, rep_link: str):
        """Обработка удаления рецензентов."""
        removed_reviewers = data.get("pull_request", {}).get("removed_reviewers", [])
        if not removed_reviewers:
            print("Список удаленных рецензентов пуст:", data)
            return

        for reviewer in removed_reviewers:
            reviewer_login = reviewer.get("login")
            reviewer_tg_name = next(
                (user["tgName"] for user in self.arr_of_users if user["repName"] == reviewer_login), None)
            if reviewer_tg_name:
                message = (
                    f"🔔 @{main_user['tgName']}\n"
                    f"❌ Удалил рецензента @{reviewer_tg_name} из запроса на слияние #{data['pull_request']['number']}\n"
                    f"Ветка: {branch}"
                )
                await self.send_telegram_message(message, rep_link)
            else:
                print(f"Удаленный рецензент не найден: {reviewer_login}")

    async def handle_review_requested_event(self, data: Dict, main_user: Dict, repo_name: str, branch: str, rep_link: str):
        """Обработка запроса на проверку."""
        single_reviewer = data.get("requested_reviewer")
        if single_reviewer:
            requested_reviewers = [single_reviewer]
        else:
            requested_reviewers = data.get("pull_request", {}).get("requested_reviewers", [])
            if not requested_reviewers:
                print("Список запрошенных рецензентов пуст:", data)
                return

        seen_logins = set()
        unique_reviewers = []
        for reviewer in requested_reviewers:
            login = reviewer.get("login")
            if not login or login in seen_logins:
                continue
            seen_logins.add(login)
            unique_reviewers.append(reviewer)

        for reviewer in unique_reviewers:
            reviewer_tg_name = next(
                (user["tgName"] for user in self.arr_of_users if user["repName"] == reviewer["login"]), None)
            if reviewer_tg_name:
                message = (
                    f"🔔 @{reviewer_tg_name}\n"
                    f"👀 Проверь, пожалуйста, запрос на слияние #{data['pull_request']['number']}\n"
                    f"Ветка: {branch}"
                )
                await self.send_telegram_message(message, rep_link)
            else:
                print(f"Рецензент не найден: {reviewer['login']}")

    async def handle_review_request_removed_event(self, data: Dict, main_user: Dict, repo_name: str, branch: str, rep_link: str):
        """Обработка снятия запроса на ревью."""
        reviewer = data.get("requested_reviewer")
        if not reviewer:
            print("Нет removed reviewer в событии:", data)
            return

        reviewer_tg_name = next(
            (user["tgName"] for user in self.arr_of_users if user["repName"] == reviewer.get("login")), None)
        if reviewer_tg_name:
            message = (
                f"🔔 @{reviewer_tg_name}\n"
                f"❌ Запрос на ревью отозван для PR #{data['pull_request']['number']}\n"
                f"Ветка: {branch}"
            )
            await self.send_telegram_message(message, rep_link)
        else:
            print(f"Рецензент не найден: {reviewer.get('login')}")

    async def handle_generic_event(self, action: str, data: Dict, main_user: Dict, repo_name: str, branch: str,
                                   rep_link: str):
        """Обработка общих событий (opened, closed, reopened, created, edited, deleted, synchronized)."""
        pull_request_creator_name = data.get("pull_request", {}).get("user", {}).get("login")
        pull_request_creator_tg_name = next(
            (user["tgName"] for user in self.arr_of_users if user["repName"] == pull_request_creator_name),
            "Неизвестный"
        )

        if action == "closed":
            is_merged = data.get("pull_request", {}).get("merged", False)
            if is_merged:
                message = (
                    f"🔔{main_user['repName']}\n"
                    f"✅ Слил запрос на слияние #{data['pull_request']['number']}\n"
                    f"Ветка: {branch}\n"
                )
            else:
                message = (
                    f"🔔{main_user['repName']}\n"
                    f"❌ Закрыл(отменил) запрос на слияние #{data['pull_request']['number']}\n"
                    f"Ветка: {branch}\n"
                )
        elif action == "reopened":
            message = (
                f"🔔{main_user['repName']}\n"
                f"🔄 Переоткрыл запрос на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}\n"
            )
        elif action == "created":
            message = (
                f"🔔{main_user['repName']}\n"
                f"📝 Прокомментировал запрос на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}\n"
                f"🔔Автор ветки: @{pull_request_creator_tg_name}"
            )
        elif action == "synchronized":
            message = (
                f"🔔{main_user['repName']}\n"
                f"🔄 Обновил ветку PR #{data['pull_request']['number']} новыми коммитами\n"
                f"Ветка: {branch}\n"
                f"🔔Автор ветки: @{pull_request_creator_tg_name}"
            )
        else:
            print(f"Неизвестное действие: {action}")
            return

        await self.send_telegram_message(message, rep_link)

    async def handle_reviewed_event(self, data: Dict, main_user: Dict, repo_name: str, branch: str, rep_link: str):
        """Обработка отзыва."""
        review = data.get("review", {})
        raw_type = review.get("type") or review.get("state")
        if not raw_type:
            print("Отзыв не содержит типа (type/state):", review)
            message = (
                f"🔔 @{main_user['tgName']}\n"
                f"⚠️ Ошибка: Отзыв не содержит типа (type/state) для запроса на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}"
            )
            await self.send_telegram_message(message, rep_link)
            return
        review_type = str(raw_type).lower()
        if review_type in {"pull_request_review_comment"}:
            review_type = "pull_request_review_comment"
        elif review_type in {"pull_request_review_commented", "comment", "commented"}:
            review_type = "pull_request_review_commented"
        elif review_type in {"pull_request_review_approved", "approved", "approve"}:
            review_type = "pull_request_review_approved"
        elif review_type in {
            "pull_request_review_rejected",
            "pull_request_review_request_changes",
            "request_changes",
            "changes_requested",
            "rejected",
        }:
            review_type = "pull_request_review_rejected"
        elif review_type in {"pull_request_comment"}:
            review_type = "pull_request_comment"

        pull_request_creator_name = data.get("pull_request", {}).get("user", {}).get("login")
        pull_request_creator_tg_name = next(
            (user["tgName"] for user in self.arr_of_users if user["repName"] == pull_request_creator_name),
            "Неизвестный"
        )

        if review_type == "pull_request_review_approved":
            message = (
                f"🔔{main_user['repName']}\n"
                f"✅ Запрос на слияние одобрен #{data['pull_request']['number']}\n"
                f"Ветка: {branch}\n"
                f"🔔 Автор ветки: @{pull_request_creator_tg_name}"
            )
        elif review_type == "pull_request_review_commented":
            message = (
                f"🔔{main_user['repName']}\n"
                f"📝 Прокомментировал запрос на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}\n"
                f"🔔Автор ветки: @{pull_request_creator_tg_name}"
            )
        elif review_type == "pull_request_review_rejected":
            review_comment = review.get("content", "Без комментария")
            message = (
                f"🔔{main_user['repName']}\n"
                f"❌ Отклонил запрос на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}\n"
                f"Комментарий: '{review_comment}'\n"
                f"🔔Автор ветки: @{pull_request_creator_tg_name}"
            )
        elif review_type == "pull_request_comment":
            review_comment = review.get("content", "Без комментария")
            message = (
                f"🔔{main_user['repName']}\n"
                f"📝 Добавил комментарий к запросу на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}\n"
                f"Комментарий: '{review_comment}'\n"
                f"🔔Автор ветки: @{pull_request_creator_tg_name}"
            )
        elif review_type == "pull_request_review_comment":
            author_login = data.get("sender", {}).get("login") or main_user.get("repName")
            pr_number = data.get("pull_request", {}).get("number")
            throttle_key = f"{author_login}:{pr_number}"
            now = time.time()
            next_allowed = self.review_comment_throttle.get(throttle_key, 0)
            if now < next_allowed:
                print(f"Пропуск уведомления по лимиту: {throttle_key}")
                return
            self.review_comment_throttle[throttle_key] = now + self.review_comment_interval_seconds
            message = (
                f"🔔{main_user['repName']}\n"
                f"📝 Есть новые комментарии к PR #{pr_number}\n"
                f"Ветка: {branch}\n"
                f"🔔Автор ветки: @{pull_request_creator_tg_name}"
            )
        else:
            print(f"Неизвестный тип отзыва: {review_type}")
            message = (
                f"🔔{main_user['repName']}\n"
                f"⚠️ Получен неизвестный тип отзыва ({review_type}) для запроса на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}"
            )

        await self.send_telegram_message(message, rep_link)


# Запуск бота (локально)
if __name__ == "__main__":
    bot = TelegramWebhookBot()
    bot.run(host="127.0.0.1", port=3333)
