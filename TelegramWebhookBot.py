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
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
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

        # Gitea API (для напоминаний о непроверенных ревью)
        self.gitea_url = os.getenv("GITEA_URL", "").rstrip("/")
        self.gitea_token = os.getenv("GITEA_TOKEN", "")
        repos_raw = os.getenv("GITEA_REPOSITORIES", "")
        self.gitea_repositories = [r.strip() for r in repos_raw.split(",") if r.strip()]
        try:
            self.review_reminder_hours = float(os.getenv("GITEA_REVIEW_REMINDER_HOURS", "24"))
        except (TypeError, ValueError):
            self.review_reminder_hours = 24.0
        self.review_reminder_threshold = self.review_reminder_hours * 3600

        self.app = Flask(__name__)
        self.app.route("/", methods=["POST"])(self.webhook)
        self.app.route("/", methods=["GET"])(self.health_check)
        self.app.route("/weekly-reminder", methods=["GET"])(self.weekly_reminder)
        self.app.route("/review-reminder", methods=["GET"])(self.review_reminder)

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

    def review_reminder(self):
        """Крон: проверяет непроверенные PR и шлёт напоминания."""
        try:
            asyncio.run(self.send_review_reminders())
            return jsonify({"status": "success"}), 200
        except Exception:
            logger.exception("Review reminder failed")
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
    # Gitea API client (stateless — метки хранятся в комментариях PR)
    # ------------------------------------------------------------------ #
    def _gitea_request(self, method: str, path: str, **kwargs) -> Optional[Dict]:
        if not self.gitea_url or not self.gitea_token:
            logger.warning("Gitea API not configured (GITEA_URL/GITEA_TOKEN)")
            return None
        url = f"{self.gitea_url}/api/v1{path}"
        headers = {"Authorization": f"token {self.gitea_token}"}
        try:
            resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
            if resp.status_code in (200, 201):
                return resp.json()
            if resp.status_code == 404:
                # Репозиторий/PR недоступен — пропускаем без паники
                logger.debug("Gitea API 404: %s %s", method, path)
                return None
            logger.error("Gitea API %s %s -> %s %s", method, path,
                         resp.status_code, resp.text[:200])
            return None
        except Exception:
            logger.exception("Gitea API request failed: %s %s", method, path)
            return None

    def _gitea_list_open_prs(self, owner: str, repo: str) -> List[Dict]:
        prs = []
        page = 1
        while True:
            data = self._gitea_request(
                "GET",
                f"/repos/{owner}/{repo}/pulls",
                params={"state": "open", "page": page, "per_page": 50},
            )
            if not data:
                break
            prs.extend(data)
            if len(data) < 50:
                break
            page += 1
        return prs

    def _gitea_get_pr(self, owner: str, repo: str, number: int) -> Optional[Dict]:
        return self._gitea_request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def _gitea_get_pr_reviews(self, owner: str, repo: str, number: int) -> List[Dict]:
        data = self._gitea_request(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/reviews"
        )
        return data or []

    def _gitea_get_pr_comments(self, owner: str, repo: str, number: int) -> List[Dict]:
        # Gitea хранит комментарии PR через issues API
        data = self._gitea_request(
            "GET", f"/repos/{owner}/{repo}/issues/{number}/comments"
        )
        return data or []

    def _gitea_post_comment(self, owner: str, repo: str, number: int, body: str) -> bool:
        # Gitea: комментарии к PR пишутся через issues API
        data = self._gitea_request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return data is not None

    def _pr_is_reviewed(self, pr: Dict) -> bool:
        """Проверяет, оставлен ли рецензент комментарий/одобрение/отклонение
        или запрошены правки (changes_requested)."""
        state = (pr.get("state") or "").lower()
        if state in ("approved", "changes_requested", "rejected", "commented"):
            return True
        # Fallback — проверяем наличие review-объектов
        return False

    def _pr_has_reminder_marker(self, comments: Optional[List[Dict]]) -> bool:
        """Проверяет, есть ли в комментариях метка о том, что напоминание уже отправлялось."""
        if not comments:
            return False
        for c in comments:
            body = c.get("body", "") or ""
            if "review-reminder-sent" in body:
                return True
        return False

    def _extract_owner_repo(self, full_name: str) -> Optional[tuple]:
        parts = full_name.split("/")
        if len(parts) == 2:
            return parts[0], parts[1]
        return None

    def _gitea_list_all_repos(self) -> List[str]:
        """Возвращает список 'owner/repo' для всех репозиториев, доступных токену.

        Использует /repos/search, так как /user/repos требует scope read:user,
        который может быть недоступен. Ответ имеет обёртку {"ok": true, "data": [...]}.
        """
        repos: List[str] = []
        page = 1
        while True:
            data = self._gitea_request(
                "GET", "/repos/search",
                params={"page": page, "limit": 50},
            )
            if not data:
                break
            items = data.get("data") if isinstance(data, dict) else data
            if not items:
                break
            for r in items:
                full = r.get("full_name")
                if full and "/" in full:
                    repos.append(full)
            if len(items) < 50:
                break
            page += 1
        return repos

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
                    parse_mode="HTML",
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

    # ------------------------------------------------------------------ #
    # Review reminder (Gitea API + крон)
    # ------------------------------------------------------------------ #
    async def send_review_reminders(self) -> None:
        """Проверяет непроверенные PR через Gitea API и шлёт напоминания.

        Логика:
        - Игнорируем выходные (суббота/воскресенье).
        - Для каждого open PR без ревью старше `review_reminder_threshold`
          шлём напоминание рецензентам.
        - Метка `<!-- review-reminder-sent -->` добавляется в комментарий PR,
          чтобы не спамить повторно.
        """
        if not self.gitea_repositories:
            logger.info("GITEA_REPOSITORIES not set — skipping review reminder")
            return

        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:  # 5=суббота, 6=воскресенье (UTC)
            logger.info("Skipping review reminder on weekend")
            return

        # Если список репозиториев пуст или задан ALL — получаем все доступные
        repos = self.gitea_repositories
        if not repos or repos == ["ALL"]:
            repos = self._gitea_list_all_repos()
            if not repos:
                logger.warning("No repositories found via Gitea API")
                return

        for full_name in repos:
            parsed = self._extract_owner_repo(full_name)
            if not parsed:
                logger.warning("Invalid repository format: %s", full_name)
                continue
            owner, repo = parsed
            await self._check_repository(owner, repo)

    async def _check_repository(self, owner: str, repo: str) -> None:
        prs = self._gitea_list_open_prs(owner, repo)
        for pr in prs:
            try:
                await self._check_pr(owner, repo, pr)
            except Exception:
                logger.exception("Failed to check PR #%s in %s/%s",
                                 pr.get("number"), owner, repo)

    async def _check_pr(self, owner: str, repo: str, pr: Dict) -> None:
        number = pr.get("number")
        if not number:
            return

        # Уже отправляли напоминание?
        comments = self._gitea_get_pr_comments(owner, repo, number)
        if self._pr_has_reminder_marker(comments):
            return

        # PR должен быть в состоянии ожидания ревью
        state = (pr.get("state") or "").lower()
        if state != "open":
            return

        # Проверяем, есть ли ревью (комментарий/одобрение/отклонение/запрос правок)
        reviews = self._gitea_get_pr_reviews(owner, repo, number)
        has_review = any(
            (r.get("state") or "").lower() in (
                "approved", "changes_requested", "rejected", "commented",
                "request_changes",  # Gitea возвращает REQUEST_CHANGES
            )
            for r in reviews
        )
        if has_review:
            return

        # Проверяем, что PR старше порога
        created_at = pr.get("created_at")
        if not created_at:
            return
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            logger.warning("Cannot parse created_at: %s", created_at)
            return

        now = datetime.now(created_dt.tzinfo) if created_dt.tzinfo else datetime.now()
        age = (now - created_dt).total_seconds()
        if age < self.review_reminder_threshold:
            return

        # Шлём напоминание
        await self._send_review_reminder(owner, repo, pr, number)

    async def _send_review_reminder(self, owner: str, repo: str, pr: Dict, number: int) -> None:
        reviewers = pr.get("requested_reviewers") or []
        mentions = []
        for r in reviewers:
            login = r.get("login") if isinstance(r, dict) else r
            tg = self._tg_name(login)
            if tg:
                mentions.append(tg)

        if not mentions:
            logger.info("No known reviewers for PR #%s in %s/%s", number, owner, repo)
            return

        url = pr.get("html_url") or f"{self.gitea_url}/{owner}/{repo}/pull/{number}"
        text = (
            f"🔔 {self._mentions_str(mentions)}\n"
            f"⏰ Напоминание: PR <a href=\"{url}\">#{number}</a> ({owner}/{repo}) "
            f"ожидает ревью более {self.review_reminder_hours:g} ч."
        )
        await self.deliver(Notification(text=text, rep_link=url))

        # Добавляем метку в комментарий, чтобы больше не напоминать
        marker_body = (
            "<!-- review-reminder-sent -->\n"
            f"Напоминание о непроверенном PR #{number} отправлено в Telegram."
        )
        self._gitea_post_comment(owner, repo, number, marker_body)

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
        return " ".join(m if m.startswith("@") else f"@{m}" for m in mentions)

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
    # Единый билдер: "🔔 @mentions\nACTION PR #N (repo)"  #N — ссылка
    # ------------------------------------------------------------------ #
    def _build(self, action_text: str, data: Dict, repo: str, url: str,
               mentions: Optional[List[str]] = None) -> str:
        pr_num = self._pr_number(data)
        if url:
            pr_link = f'<a href="{url}">#{pr_num}</a>'
        else:
            pr_link = f'#{pr_num}'
        head = f"{action_text} PR {pr_link} ({repo})"
        if mentions:
            return f"🔔 {self._mentions_str(mentions)}\n{head}"
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
            "unassigned": self.handle_unassigned_event,
        }
        handler = handlers.get(action)
        if handler is None:
            logger.info("Event ignored by configuration: %s", action)
            return
        await handler(action, data, main_user, repo_name, branch, rep_link)

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
            f"🔔 {self._mentions_str(mentions)}\n"
            f"❌ {actor} убрал(а) с PR <a href=\"{rep_link}\">#{self._pr_number(data)}</a> ({repo_name})"
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
            f"🫡 Проверьте PR <a href=\"{rep_link}\">#{self._pr_number(data)}</a> ({repo_name})"
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
            f"❌ Запрос на ревью отозван: PR <a href=\"{rep_link}\">#{self._pr_number(data)}</a> ({repo_name})  🔔 {tg}"
        )
        await self.deliver(Notification(text=text, rep_link=rep_link))

    async def handle_generic_event(self, action, data, main_user, repo_name, branch, rep_link):
        """Общие события (closed, reopened, created, deleted, synchronized)."""
        actor = self._actor_mention(main_user)
        url = rep_link
        repo = repo_name

        if action == "closed":
            is_merged = data.get("pull_request", {}).get("merged", False)
            if is_merged:
                text = self._build("✅ слит", data, repo, url)
            else:
                text = self._build("❌ закрыт(а)", data, repo, url, [actor])
        elif action == "reopened":
            text = self._build("🔄 переоткрыт(а)", data, repo, url, [actor])
        elif action == "created":
            # Comment on PR: notify PR author and reviewers, NOT the commenter
            comment_author = data.get("comment", {}).get("user", {}).get("login") or main_user.get("repName")
            pr_author_login = data.get("pull_request", {}).get("user", {}).get("login")
            pr_author_tg = self._tg_name(pr_author_login)
            author_mention = f"@{pr_author_tg}" if pr_author_tg else (pr_author_login or "Автор PR")

            mentions = self._reviewer_mentions(data, exclude=comment_author)
            if pr_author_tg and pr_author_tg not in mentions and pr_author_login != comment_author:
                mentions.append(f"@{pr_author_tg}")

            text = self._build("💬 новый комментарий", data, repo, url, mentions)
        elif action == "synchronized":
            # Disabled: notifications only via review_requested
            return
        elif action == "deleted":
            text = self._build("🗑️ удалён(а)", data, repo, url, [actor])
        else:
            logger.warning("Unknown generic action: %s", action)
            return
        await self.deliver(Notification(text=text, rep_link=rep_link))

    async def handle_reviewed_event(self, action, data, main_user, repo_name, branch, rep_link):
        """Обработка отзыва."""
        review = data.get("review", {}) or {}
        raw_type = review.get("type") or review.get("state")
        review_type = self._normalize_review_type(raw_type)

        logger.info("Review event: raw_type=%r review_type=%r review=%s", raw_type, review_type, review)

        url = rep_link
        repo = repo_name

        # Comment author (sender of webhook)
        comment_author = data.get("sender", {}).get("login") or main_user.get("repName")

        # PR author
        pr_author_login = data.get("pull_request", {}).get("user", {}).get("login")
        pr_author_tg = self._tg_name(pr_author_login)
        author_mention = f"@{pr_author_tg}" if pr_author_tg else (pr_author_login or "Автор PR")

        # Build mentions: PR author + reviewers (excluding comment author)
        def build_comment_mentions() -> List[str]:
            mentions = []
            if pr_author_tg and pr_author_login != comment_author:
                mentions.append(f"@{pr_author_tg}")
            reviewers = self._reviewer_mentions(data, exclude=comment_author)
            mentions.extend(reviewers)
            return mentions

        if review_type == "pull_request_review_approved":
            text = self._build("✅ одобрен", data, repo, url, [author_mention])
        elif review_type == "pull_request_review_commented":
            mentions = build_comment_mentions()
            text = self._build("💬 оставлен комментарий", data, repo, url, mentions)
        elif review_type == "pull_request_review_rejected":
            text = self._build("❌ отклонён", data, repo, url, [author_mention])
        elif review_type == "pull_request_comment":
            mentions = build_comment_mentions()
            text = self._build("💬 оставлен комментарий", data, repo, url, mentions)
        elif review_type == "pull_request_review_comment":
            throttle_key = f"{comment_author}:{self._pr_number(data)}"
            mentions = build_comment_mentions()
            text = self._build("📝 есть новые комментарии", data, repo, url, mentions)
            await self.deliver(Notification(text=text, rep_link=rep_link, throttle_key=throttle_key))
            return
        else:
            logger.warning("Unknown review type: %r (raw=%r)", review_type, raw_type)
            text = self._build(
                f"⚠️ Неизвестный отзыв ({raw_type})",
                data, repo, url, [author_mention]
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
