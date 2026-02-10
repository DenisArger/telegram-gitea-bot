# -*- coding: utf-8 -*-
from flask import Flask, request, jsonify
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from typing import Dict, Optional
from dotenv import load_dotenv
import os
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
        self.arr_of_users = [
            {"repName": "AnatolyHryntsevich", "tgName": "htyntsevich"},
            {"repName": "LeonidParfenov", "tgName": "ljkiu_o"},
            {"repName": "RomanProtchenko", "tgName": "nesterpetrov"},
            {"repName": "DenisArger", "tgName": "Denis_Arger"},
            {"repName": "VeronikaRitareva", "tgName": "vritareva"},
            {"repName": "AnastasiaKonopatskaya", "tgName": "anstsknptsk"},
            {"repName": "NikitaHalukh", "tgName": "gn370p0"},
            {"repName": "AleksandrOvsyanikov", "tgName": "iressq"},
            {"repName": "DaniilKrauchanka", "tgName": "Krava_DpS"},
        ]

        # Инициализация Flask и Telegram Bot
        self.app = Flask(__name__)
        self.bot = Bot(token=self.TOKEN)

        # Регистрация маршрута для webhook
        self.app.route("/", methods=["POST"])(self.webhook)
        # Health-check for platform probes
        self.app.route("/", methods=["GET"])(self.health_check)

    def health_check(self):
        return "ok", 200

    def run(self, host: str = "127.0.0.1", port: int = 3333):
        """Запуск сервера."""
        self.app.run(host=host, port=port)  # Явно указываем локальный адрес (127.0.0.1)

    async def webhook(self):
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
                rep_link = data["pull_request"]["html_url"]
                branch = data["pull_request"]["head"]["ref"]
            elif data.get("issue") and data.get("comment"):
                rep_link = data["issue"]["pull_request"]["html_url"] if data["issue"].get("pull_request") else \
                    data["issue"]["url"]
                branch = data["issue"]["title"]

            if not rep_link or not branch:
                print("Неизвестная структура данных:", data)
                return jsonify({"status": "error", "message": "Unknown data structure"}), 400

            # Обработка событий
            if action == "assigned" and data.get("pull_request"):
                await self.handle_assigned_event(data, main_user, repo_name, branch, rep_link)
            elif action == "unassigned" and data.get("pull_request"):
                await self.handle_unassigned_event(data, main_user, repo_name, branch, rep_link)
            elif action == "review_requested":
                await self.handle_review_requested_event(data, main_user, repo_name, branch, rep_link)
            elif action in ["opened", "closed", "reopened", "created", "edited", "deleted"]:
                await self.handle_generic_event(action, data, main_user, repo_name, branch, rep_link)
            elif action == "reviewed":
                await self.handle_reviewed_event(data, main_user, repo_name, branch, rep_link)
            else:
                print(f"Неизвестное действие: {action}")

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
        requested_reviewers = data.get("pull_request", {}).get("requested_reviewers", [])
        if not requested_reviewers:
            print("Список запрошенных рецензентов пуст:", data)
            return

        for reviewer in requested_reviewers:
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

    async def handle_generic_event(self, action: str, data: Dict, main_user: Dict, repo_name: str, branch: str,
                                   rep_link: str):
        """Обработка общих событий (opened, closed, reopened, created, edited, deleted)."""
        pull_request_creator_name = data.get("pull_request", {}).get("user", {}).get("login")
        pull_request_creator_tg_name = next(
            (user["tgName"] for user in self.arr_of_users if user["repName"] == pull_request_creator_name),
            "Неизвестный"
        )

        if action == "opened":
            message = (

                f"🔔{main_user['repName']}\n"
                f"🚀 Создал запрос на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}"
            )
        elif action == "closed":
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
        elif action == "edited":
            message = (
                f"🔔{main_user['repName']}\n"
                f"✏️ Изменил комментарий к запросу на слияние #{data['pull_request']['number']}\n"
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
        review_type = review.get("type")
        if not review_type:
            print("Отзыв не содержит типа (type):", review)
            message = (
                f"🔔 @{main_user['tgName']}\n"
                f"⚠️ Ошибка: Отзыв не содержит типа (type) для запроса на слияние #{data['pull_request']['number']}\n"
                f"Ветка: {branch}"
            )
            await self.send_telegram_message(message, rep_link)
            return

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
