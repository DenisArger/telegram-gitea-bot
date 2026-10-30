# -*- coding: utf-8 -*-
"""
Обработчик команды /weekly_report_detailed.
Вынесен из TelegramWebhookBot.py в отдельный модуль.

Особенности:
- доступ только для Denis_Arger в личном чате;
- сбор данных выполняется один раз;
- генерация PDF выполняется в отдельном потоке;
- статусное сообщение гарантированно завершается;
- временный PDF удаляется автоматически;
- повторяются только временные ошибки Telegram.
"""

import asyncio
import logging
import os
import tempfile
import time
from typing import Awaitable, Callable, List, Optional

from telegram import Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CollectWeeklyReport = Callable[..., Awaitable[tuple]]
BuildDetailedPDF = Callable[..., bytes]
BuildDetailedText = Callable[..., str]
SplitMessage = Callable[[str], List[str]]

STATUS_UPDATE_INTERVAL = 30.0
REPORT_TIMEOUT_SECONDS = 420.0
PDF_BUILD_TIMEOUT_SECONDS = 120.0
SEND_ATTEMPTS = 4
SEND_RETRY_DELAY_SECONDS = 5.0


async def _send_document_with_retry(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    pdf_content: bytes,
    filename: str,
) -> None:
    """Отправляет PDF с повторными попытками при временных ошибках."""
    last_error: Optional[Exception] = None

    with tempfile.TemporaryDirectory(prefix="weekly_report_detailed_") as tmp_dir:
        pdf_path = os.path.join(tmp_dir, filename)

        with open(pdf_path, "wb") as pdf_file:
            pdf_file.write(pdf_content)

        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                with open(pdf_path, "rb") as pdf_file:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=pdf_file,
                        filename=filename,
                        caption="Детальный PDF-отчёт для печати",
                    )
                logger.info("Detailed weekly PDF sent successfully")
                return

            except RetryAfter as exc:
                last_error = exc
                if attempt == SEND_ATTEMPTS:
                    break

                retry_after = getattr(exc, "retry_after", SEND_RETRY_DELAY_SECONDS)
                if hasattr(retry_after, "total_seconds"):
                    retry_after = retry_after.total_seconds()

                delay = max(float(retry_after), 1.0)
                logger.warning(
                    "Telegram rate limit while sending PDF "
                    "(attempt %d/%d); retrying in %.1f seconds",
                    attempt,
                    SEND_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)

            except (NetworkError, TimedOut) as exc:
                last_error = exc
                if attempt == SEND_ATTEMPTS:
                    break

                delay = SEND_RETRY_DELAY_SECONDS * attempt
                logger.warning(
                    "Temporary error sending PDF (attempt %d/%d): %s; "
                    "retrying in %.1f seconds",
                    attempt,
                    SEND_ATTEMPTS,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

            except Exception:
                # Ошибки вроде неверного chat_id или отсутствия прав
                # повторять бессмысленно.
                logger.exception("Non-retryable error while sending detailed PDF")
                raise

    if last_error is not None:
        raise last_error

    raise RuntimeError("PDF could not be sent for an unknown reason")


async def _run_status_loop(
    *,
    status_msg,
    stop_event: asyncio.Event,
    started_at: float,
) -> None:
    """Периодически обновляет статусное сообщение."""
    dots = 0

    while not stop_event.is_set():
        dots = (dots + 1) % 4
        elapsed = int(time.monotonic() - started_at)

        try:
            await status_msg.edit_text(
                f"Собираю коммиты из всех веток{'.' * dots} "
                f"(прошло {elapsed} с)"
            )
        except Exception as exc:
            # Не завершаем основной процесс из-за ошибки обновления статуса.
            logger.debug("Could not update report status message: %s", exc)

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=STATUS_UPDATE_INTERVAL,
            )
        except asyncio.TimeoutError:
            continue


async def _stop_status_task(
    *,
    stop_event: asyncio.Event,
    status_task: Optional[asyncio.Task],
) -> None:
    """Останавливает фоновую задачу обновления статуса."""
    stop_event.set()

    if status_task is None:
        return

    try:
        await status_task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Status task finished with an error")


async def handle_weekly_report_detailed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    arr_of_users: list,
    collect_weekly_report: CollectWeeklyReport,
    build_detailed_pdf: BuildDetailedPDF,
    build_detailed_text: BuildDetailedText,
    split_message: SplitMessage,
) -> None:
    """
    Команда /weekly_report_detailed — детальный отчёт за неделю
    по макету (только Denis_Arger, личный чат).
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if message is None or user is None or chat is None:
        logger.warning("Detailed weekly report called without message/user/chat")
        return

    tg_username = user.username
    if not tg_username or tg_username.lstrip("@").lower() != "denis_arger":
        await message.reply_text("Команда недоступна.")
        return

    if chat.type != "private":
        await message.reply_text("Команда доступна только в личном чате.")
        return

    denis = next(
        (
            item
            for item in arr_of_users
            if (item.get("tgName") or "").lstrip("@").lower() == "denis_arger"
        ),
        None,
    )

    if denis is None:
        await message.reply_text("Не найден в users.json.")
        return

    status_msg = None
    status_stop = asyncio.Event()
    status_task = None
    report = None
    start_local = None
    end_local = None
    operation_started_at = time.monotonic()

    try:
        status_msg = await message.reply_text("Формирую отчёт, подождите…")

        # Сбор данных выполняется ровно один раз.
        collect_started_at = time.monotonic()
        try:
            report, start_local, end_local = await asyncio.wait_for(
                collect_weekly_report(exclude_login=denis.get("repName")),
                timeout=REPORT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.exception("Weekly report data collection timed out")
            await message.reply_text(
                "Сбор данных превысил допустимое время. "
                "Не удалось получить полный отчёт; повторный сбор не запускается."
            )
            return

        logger.info(
            "Weekly report collected in %.2f seconds",
            time.monotonic() - collect_started_at,
        )

        if not report:
            await message.reply_text("За выбранный период данные для отчёта не найдены.")
            return

        await status_msg.edit_text("Формирую PDF-отчёт…")

        # Обновление статуса работает только во время генерации и отправки PDF.
        status_started_at = time.monotonic()
        status_task = asyncio.create_task(
            _run_status_loop(
                status_msg=status_msg,
                stop_event=status_stop,
                started_at=status_started_at,
            )
        )

        # Синхронную генерацию PDF переносим в executor, чтобы не блокировать event loop.
        pdf_started_at = time.monotonic()
        loop = asyncio.get_running_loop()

        pdf_content = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                build_detailed_pdf,
                report,
                start_local,
                end_local,
            ),
            timeout=PDF_BUILD_TIMEOUT_SECONDS,
        )

        if not isinstance(pdf_content, (bytes, bytearray)):
            raise TypeError(
                "build_detailed_pdf must return bytes or bytearray, "
                f"got {type(pdf_content).__name__}"
            )

        pdf_content = bytes(pdf_content)
        if not pdf_content:
            raise ValueError("PDF generator returned an empty document")

        logger.info(
            "Detailed weekly PDF generated in %.2f seconds (%d bytes)",
            time.monotonic() - pdf_started_at,
            len(pdf_content),
        )

        filename = (
            f"weekly_report_detailed_{start_local.strftime('%d%m')}.pdf"
        )

        send_started_at = time.monotonic()
        await _send_document_with_retry(
            context=context,
            chat_id=chat.id,
            pdf_content=pdf_content,
            filename=filename,
        )

        logger.info(
            "Detailed weekly PDF sent in %.2f seconds",
            time.monotonic() - send_started_at,
        )

        await message.reply_text("Детальный PDF-отчёт сформирован и отправлен.")

    except asyncio.TimeoutError:
        logger.exception("PDF generation timed out")
        await message.reply_text(
            "Формирование PDF превысило допустимое время. "
            "Данные уже собраны, но PDF не удалось подготовить."
        )

        # Если данные уже собраны, пробуем отправить текстовую версию
        # из тех же данных, не обращаясь повторно к Gitea.
        if report is not None:
            try:
                text_report = build_detailed_text(
                    report,
                    start_local,
                    end_local,
                )
                for part in split_message(text_report):
                    await message.reply_text(part, parse_mode="HTML")
            except Exception:
                logger.exception("Failed to build/send text fallback report")
                await message.reply_text(
                    "Не удалось сформировать текстовую версию отчёта."
                )

    except Exception:
        logger.exception("/weekly_report_detailed failed")
        await message.reply_text(
            "Ошибка при формировании или отправке отчёта. Попробуйте позже."
        )

    finally:
        await _stop_status_task(
            stop_event=status_stop,
            status_task=status_task,
        )

        if status_msg is not None:
            try:
                await status_msg.delete()
            except Exception as exc:
                logger.debug("Could not delete status message: %s", exc)

        logger.info(
            "/weekly_report_detailed finished in %.2f seconds",
            time.monotonic() - operation_started_at,
        )
