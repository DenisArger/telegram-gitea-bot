# -*- coding: utf-8 -*-
"""
TelegramWebhookBot — принимает вебхуки от Gitea и транслирует события
в Telegram-чат/тему минималистичными русскими уведомлениями.

Стиль (по образцу):
    🔔 @r1 @r2
    🫡 Проверьте PR #17 (url) (repo)

    ✅ PR #17 (url) (repo) слился  🔔 @actor
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
from typing import Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

COMMENT_MAX_LEN = 200
THROTTLE_INTERVAL_SECONDS = 30 * 60
THROTTLE_EVICTION_EVERY = 50


class Notification:
    """Контейнер для одного уведомления — текст + метаданные доставки."""
    __slots__ = ("text", "rep_link", "throttle_key", "with_keyboard")

    def __init__(self, text: str, rep_link: Optional[str] = None,
                 throttle_key: Optional[str] = None, with_keyboard: bool = True):
        self.text = text
        self.rep_link = rep_link
        self.throttle_key = throttle_key
        self.with_keyboard = with_keyboard


class TelegramWebhookBot:
    def __init__(self):
        load_dotenv()

        self.TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        self.TARGET_CHAT_ID = os.getenv("TELEGRAM_TARGET_CHAT_ID")
        message_thread_id = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")
        self.WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET") or os.getenv("WEBHOOK_SECRET")

        missing = [
            name for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.TOKEN),
                ("TELEGRAM_TARGET_CHAT_ID", self.TARGET_CHAT_ID),
                ("TELEGRAM_MESSAGE_THREAD_ID", message_thread_id),
            ) if not value
        ]
        if missing:
            raise ValueError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        self.MESSAGE_THREAD_ID = int(message_thread_id)

        self.arr_of_users = self.load_users()
        self._tg_by_login: Dict[str, Optional[str]] = {
            u.get("repName"): u.get("tgName")
            for u in self.arr_of_users if u.get("repName")
        }

        # Throttle: key -> next allowed epoch
        self._throttle: Dict[str, float] = {}
        self._throttle_interval = THROTTLE_INTERVAL_SECONDS
        self._throttle_evict_counter = 0

        exclude_raw = os.getenv("WEEKLY_REMINDER_EXCLUDE", "")
        self._weekly_exclude = {s.strip() for s in exclude_raw.split(",") if s.strip()}

        self.app = Flask(__name__)
        self.app.route("/", methods=["POST"])(self.webhook)
        self.app.route("/", methods=["GET"])(self.health_check)
        self.app.route("/weekly-reminder", methods=["GET"])(self.weekly_reminder)

    def load_users(self) -> List[Dict]:
        """Load users from users.json"""
        import json
        users_path = os.path.join(os.path.dirname(__file__), "users.json")
        with open(users_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------ #
    # Flask routes
    # ------------------------------------------------------------------ #
    def health_check(self):
        return "ok", 200

    def webhook(self):
        try:
            raw_body = request.get_data()
            signature = (
                request.headers.get("X-Gitea-Signature")
                or request.headers.get("X-Gitea-Event-Signature")
            )
            if not self._verify_signature(raw_body, signature):
                logger.warning("Webhook signature verification failed")
                return jsonify({"status": "error", "message": "Invalid signature"}), 401

            data = request.get_json(silent=True)
            if not data or not isinstance(data, dict):
                logger.warning("Invalid webhook payload: %r", data)
                return jsonify({"status": "error", "message": "Bad Request"}), 400

            rep_user_name = data.get("sender", {}).get("login")
            if not rep_user_name:
                logger.warning("Payload missing sender.login: %r", data)
                return jsonify({"status": "error", "message": "Missing sender login"}), 400

            main_user = next(
                (u for u in self.arr_of_users if u.get("repName") == rep_user_name), None
            )
            if not main_user:
                logger.warning("Unknown sender: %s", rep_user_name)
                return jsonify({"status": "error", "message": "User not found"}), 400

            action = data.get("action")
            repo_name = data.get("repository", {}).get("name")
            branch = None
            rep_link = None

            if data.get("pull_request"):
                rep_link = data["pull_request"].get("html_url")
                branch = data.get("pull_request", {}).get("head", {}).get("ref")
                if not branch:
                    branch = (
                        data.get("pull_request", {}).get("title")
                        or data.get("issue", {}).get("title")
                    )
            elif data.get("issue") and data.get("comment"):
                if data["issue"].get("pull_request"):
                    rep_link = data["issue"]["pull_request"].get("html_url")
                else:
                    rep_link = data["issue"].get("url")
                branch = data["issue"].get("title")

            if not rep_link or not branch:
                logger.warning("Unknown data structure: %r", data)
                return jsonify({"status": "error", "message": "Unknown data structure"}), 400

            asyncio.run(
                self.process_event(action, data, main_user, repo_name, branch, rep_link)
            )
            return jsonify({"status": "success"}), 200
        except Exception:
            logger.exception("Webhook handling failed")
            return jsonify({"status": "error", "message": "Internal error"}), 500

    def weekly_reminder(self):
        try:
            message = self.build_weekly_reminder_message()
            asyncio.run(self.send_plain_message(message))
            return jsonify({"status": "success"}), 200
        except Exception:
            logger.exception("Weekly reminder failed")
            return jsonify({"status": "error", "message": "Internal error"}), 500

    def run(self, host: str = "127.0.0.1", port: int = 3333):
        """Запуск сервера."""
        self.app.run(host=host, port=port)

    # ------------------------------------------------------------------ #
    # Signature verification (Gitea HMAC-SHA256)
    # ------------------------------------------------------------------ #
    def _verify_signature(self, raw_body: bytes, signature: Optional[str]) -> bool:
        if not self.WEBHOOK_SECRET:
            logger.debug("WEBHOOK_SECRET not set — signature check skipped")
            return True
        if not signature:
            return False
        sig = signature.split("=", 1)[-1] if "=" in signature else signature
        expected = hmac.new(
            self.WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, sig)

    # ------------------------------------------------------------------ #
    # Delivery — единственный путь отправки, единый шаблон клавиатуры
    # ------------------------------------------------------------------ #
    async def deliver(self, notification: Notification) -> bool:
        if notification.throttle_key:
            now = time.time()
            next_allowed = self._throttle.get(notification.throttle_key, 0.0)
            if now < next_allowed:
                logger.info("Throttled notification: %s", notification.throttle_key)
                return False
            self._throttle[notification.throttle_key] = now + self._throttle_interval
            self._maybe_evict_throttle(now)

        keyboard = None
        if notification.with_keyboard and notification.rep_link:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Перейти к pull_request", url=notification.rep_link)]
            ])

        # Bot создаётся локально под текущий event loop — решает проблему
        # "attached to a different event loop" при повторном использовании
        # одного экземпляра Bot между asyncio.run().
        async with Bot(token=self.TOKEN) as bot:
            try:
                await bot.send_message(
                    chat_id=self.TARGET_CHAT_ID,
                    text=notification.text,
                    reply_markup=keyboard,
                    message_thread_id=self.MESSAGE_THREAD_ID,
                )
                logger.info("Notification sent")
                return True
            except Exception as exc:
                logger.error("Failed to send notification: %s", exc)
                return False

    async def send_plain_message(self, message: str) -> bool:
        if not message:
            logger.warning("Empty message for send_plain_message")
            return False
        return await self.deliver(Notification(text=message, with_keyboard=False))

    def _maybe_evict_throttle(self, now: float) -> None:
        self._throttle_evict_counter += 1
        if self._throttle_evict_counter % THROTTLE_EVICTION_EVERY != 0:
            return
        stale = [k for k, t in self._throttle.items() if t < now]
        for key in stale:
            del self._throttle[key]

    # ------------------------------------------------------------------ #
    # Helpers — минималистичный стиль: "PR #N (url) (repo)"
    # ------------------------------------------------------------------ #
    def _tg_name(self, login: Optional[str]) -> Optional[str]:
        if not login:
            return None
        return self._tg_by_login.get(login)

    def _pr_number(self, data: Dict) -> str:
        return str(
            data.get("pull_request", {}).get("number")
            or data.get("issue", {}).get("number")
            or "?"
        )

    def _reviewer_mentions(self, data: Dict, exclude: Optional[str] = None) -> List[str]:
        mentions: List[str] = []
        seen = set()
        for reviewer in data.get("pull_request", {}).get("requested_reviewers", []):
            login = reviewer.get("login") if isinstance(reviewer, dict) else reviewer
            if not login or login == exclude or login in seen:
                continue
            seen.add(login)
            tg = self._tg_name(login)
            if tg:
                mentions.append(tg)
        return mentions

    @staticmethod
    def _mentions_str(mentions: List[str]) -> str:
        return " ".join(f"@{m}" for m in mentions)

    @staticmethod
    def _truncate_comment(text: Optional[str]) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"\s+", " ", str(text)).strip()
        if len(cleaned) > COMMENT_MAX_LEN:
            cleaned = cleaned[: COMMENT_MAX_LEN - 1].rstrip() + "…"
        return cleaned

    def _actor_mention(self, user: Dict) -> str:
        tg = user.get("tgName")
        if tg:
            return f"@{tg}"
        return user.get("repName") or "Кто-то"

    # ------------------------------------------------------------------ #
    # Единый билдер: "ACTION PR #N (url) (repo)  🔔 @mentions"
    # ------------------------------------------------------------------ #
    def _build(self, action_text: str, data: Dict, repo: str, url: str,
               mentions: Optional[List[str]] = None) -> str:
        head = f"{action_text} PR #{self._pr_number(data)} ({url}) ({repo})"
        if mentions:
            return f"{head}  🔔 {self._mentions_str(mentions)}"
        return head

    # ------------------------------------------------------------------ #
    # Weekly reminder
    # ------------------------------------------------------------------ #
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
            "⏳ до завершения недели осталось чуть-чуть — заполните отчет, пожалуйста.",
            "🔍 Итоги недели: пришло время заполнить отчет.",
        ]
        mentions = [
            f"@{user['tgName']}"
            for user in self.arr_of_users
            if user.get("tgName")
            and not user.get("skipWeekly")
            and user["tgName"] not in self._weekly_exclude
        ]
        return f"{random.choice(templates)}\n{self._mentions_str(mentions)}"

    # ------------------------------------------------------------------ #
    # Event routing
    # ------------------------------------------------------------------ #
    async def process_event(self, action, data, main_user, repo_name, branch, rep_link):
        handlers = {
            "review_requested": self.handle_review_requested_event,
            "review_request_removed": self.handle_review_request_removed_event,
            "closed": self.handle_generic_event,
            "reopened": self.handle_generic_event,
            "created": self.handle_generic_event,
            "deleted": self.handle_generic_event,
            "synchronized": self.handle_generic_event,
            "reviewed": self.handle_reviewed_event,
            "assigned": self.handle_assigned_event,
            "unassigned": self.handle_unassigned_event,
        }
        handler = handlers.get(action)
        if handler is None:
            logger.info("Event ignored by configuration: %s", action)
            return
        await handler(action, data, main_user, repo_name, branch, rep_link)

    async def handle_assigned_event(self, action, data, main_user, repo_name, branch, rep_link):
        """Назначение ответственных — одно сообщение на всех."""
        assignees = data.get("pull_request", {}).get("assignees", [])
        mentions = []
        seen = set()
        for assignee in assignees:
            login = assignee.get("login") if isinstance(assignee, dict) else assignee
            if not login or login == main_user.get("repName") or login in seen:
                continue
            seen.add(login)
            tg = self._tg_name(login)
            if tg:
                mentions.append(tg)
        if not mentions:
            logger.info("No new assignees to notify")
            return
        text = (
            f"🔔 {self._mentions_str(mentions)}\n"
            f"🫡 Проверьте PR #{self._pr_number(data)} ({rep_link}) ({repo_name})"
        )
        await self.deliver(Notification(text=text, rep_link=rep_link))

    async def handle_unassigned_event(self, action, data, main_user, repo_name, branch, rep_link):
        """Снятие ответственных/рецензентов."""
        removed = (
            data.get("pull_request", {}).get("removed_reviewers")
            or data.get("pull_request", {}).get("removed_assignees")
            or []
        )
        mentions = []
        seen = set()
        for reviewer in removed:
            login = reviewer.get("login") if isinstance(reviewer, dict) else reviewer
            if not login or login in seen:
                continue
            seen.add(login)
            tg = self._tg_name(login)
            if tg:
                mentions.append(tg)
        if not mentions:
            logger.info("No removed reviewers to notify")
            return
        actor = self._actor_mention(main_user)
        text = (
            f"❌ {actor} убрал(а) {self._mentions_str(mentions)} "
            f"с PR #{self._pr_number(data)} ({rep_link}) ({repo_name})"
        )
        await self.deliver(Notification(text=text, rep_link=rep_link))

    async def handle_review_requested_event(self, action, data, main_user, repo_name, branch, rep_link):
        """Запрос на проверку — одно сообщение на всех рецензентов."""
        single = data.get("requested_reviewer")
        reviewers = [single] if single else data.get("pull_request", {}).get("requested_reviewers", [])
        mentions = self._reviewer_mentions(data)
        if not mentions:
            logger.warning("No known reviewers for review_requested")
            return
        text = (
            f"🔔 {self._mentions_str(mentions)}\n"
            f"🫡 Проверьте PR #{self._pr_number(data)} ({rep_link}) ({repo_name})"
        )
        await self.deliver(Notification(text=text, rep_link=rep_link))

    async def handle_review_request_removed_event(self, action, data, main_user, repo_name, branch, rep_link):
        """Снятие запроса на ревью."""
        reviewer = data.get("requested_reviewer")
        login = reviewer.get("login") if isinstance(reviewer, dict) else reviewer
        tg = self._tg_name(login)
        if not tg:
            logger.warning("Removed reviewer not mapped: %s", login)
            return
        text = (
            f"❌ Запрос на ревью отозван: PR #{self._pr_number(data)} ({rep_link}) ({repo_name})  🔔 @{tg}"
        )
        await self.deliver(Notification(text=text, rep_link=rep_link))

    async def handle_generic_event(self, action, data, main_user, repo_name, branch, rep_link):
        """Общие события (closed, reopened, created, deleted, synchronized)."""
        pr_number = self._pr_number(data)
        actor = self._actor_mention(main_user)
        url = rep_link
        repo = repo_name

        if action == "closed":
            is_merged = data.get("pull_request", {}).get("merged", False)
            if is_merged:
                text = self._build(f"✅ PR #{pr_number} ({url}) ({repo}) слит", data, repo, url, [actor])
            else:
                text = self._build(f"❌ PR #{pr_number} ({url}) ({repo}) закрыт", data, repo, url, [actor])
        elif action == "reopened":
            text = self._build(f"🔄 PR #{pr_number} ({url}) ({repo}) переоткрыт", data, repo, url, [actor])
        elif action == "created":
            creator_login = data.get("pull_request", {}).get("user", {}).get("login")
            creator_tg = self._tg_name(creator_login)
            creator = f"@{creator_tg}" if creator_tg else "Неизвестный"
            text = self._build(f"💬 PR #{pr_number} ({url}) ({repo}) комментарий", data, repo, url, [actor, creator])
        elif action == "synchronized":
            mentions = self._reviewer_mentions(data, exclude=main_user.get("repName"))
            text = self._build(f"🔄 PR #{pr_number} ({url}) ({repo}) обновлён", data, repo, url, [actor] + mentions)
        elif action == "deleted":
            text = self._build(f"🗑️ PR #{pr_number} ({url}) ({repo}) удалён", data, repo, url, [actor])
        else:
            logger.warning("Unknown generic action: %s", action)
            return
        await self.deliver(Notification(text=text, rep_link=rep_link))

    async def handle_reviewed_event(self, action, data, main_user, repo_name, branch, rep_link):
        """Обработка отзыва."""
        review = data.get("review", {}) or {}
        raw_type = review.get("type") or review.get("state")
        review_type = self._normalize_review_type(raw_type)

        pr_number = self._pr_number(data)
        actor = self._actor_mention(main_user)
        url = rep_link
        repo = repo_name

        if review_type == "pull_request_review_approved":
            text = self._build(f"✅ PR #{pr_number} ({url}) ({repo}) одобрен", data, repo, url, [actor])
        elif review_type == "pull_request_review_commented":
            text = self._build(f"💬 PR #{pr_number} ({url}) ({repo}) оставлен комментарий", data, repo, url, [actor])
        elif review_type == "pull_request_review_rejected":
            text = self._build(f"❌ PR #{pr_number} ({url}) ({repo}) отклонён", data, repo, url, [actor])
        elif review_type == "pull_request_comment":
            text = self._build(f"💬 PR #{pr_number} ({url}) ({repo}) оставлен комментарий", data, repo, url, [actor])
        elif review_type == "pull_request_review_comment":
            author_login = data.get("sender", {}).get("login") or main_user.get("repName")
            throttle_key = f"{author_login}:{pr_number}"
            text = self._build(f"📝 PR #{pr_number} ({url}) ({repo}) есть новые комментарии", data, repo, url, [actor])
            await self.deliver(Notification(text=text, rep_link=rep_link, throttle_key=throttle_key))
            return
        else:
            logger.warning("Unknown review type: %r (raw=%r)", review_type, raw_type)
            text = self._build(
                f"⚠️ Неизвестный отзыв ({raw_type})",
                data, repo, url, [actor]
            )
        await self.deliver(Notification(text=text, rep_link=rep_link))

    @staticmethod
    def _normalize_review_type(raw_type) -> Optional[str]:
        if not raw_type:
            return None
        rt = str(raw_type).lower()
        mapping = {
            "pull_request_review_comment": {"pull_request_review_comment"},
            "pull_request_review_commented": {
                "pull_request_review_commented", "comment", "commented",
            },
            "pull_request_review_approved": {
                "pull_request_review_approved", "approved", "approve",
            },
            "pull_request_review_rejected": {
                "pull_request_review_rejected",
                "pull_request_review_request_changes",
                "request_changes",
                "changes_requested",
                "rejected",
            },
            "pull_request_comment": {"pull_request_comment"},
        }
        for canonical, aliases in mapping.items():
            if rt in aliases:
                return canonical
        return rt


# Запуск бота (локально)
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bot = TelegramWebhookBot()
    bot.run(host="127.0.0.1", port=3333)
