#!/usr/bin/env python3
"""
Telegram бот для модерации группового чата.

Функционал:
- /tishe - 3+ голосов = запрет медиа на 24 часа
- /zaebal - 5+ голосов = полный мьют на 24 часа
"""

import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, Set, Optional
from dotenv import load_dotenv

from telegram import Update, ChatPermissions
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env файле")

# Хранилище голосов: {chat_id: {message_id: {'tishe': set(user_ids), 'zaebal': set(user_ids)}}}
votes: Dict[int, Dict[int, Dict[str, Set[int]]]] = defaultdict(
    lambda: defaultdict(lambda: {'tishe': set(), 'zaebal': set()})
)

# Хранилище активных ограничений: {chat_id: {user_id: {'type': str, 'until': datetime}}}
restrictions: Dict[int, Dict[int, Dict]] = defaultdict(dict)

# Константы
TISHE_VOTES_REQUIRED = 3  # Количество голосов для запрета медиа
ZAEBAL_VOTES_REQUIRED = 5  # Количество голосов для полного мьюта
RESTRICTION_DURATION = timedelta(hours=24)  # Длительность ограничения


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    help_text = """
🤖 Бот для модерации чата

Команды (в ответ на сообщение):
• /tishe - Голосовать за запрет медиа (нужно 3 голоса)
• /zaebal - Голосовать за полный мьют (нужно 5 голосов)
• /status - Проверить статус ограничений

Ограничения действуют 24 часа.
    """
    await update.message.reply_text(help_text)


async def tishe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /tishe - запрет медиа"""
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Эта команда должна быть ответом на сообщение!")
        return

    chat_id = update.effective_chat.id
    voter_id = update.effective_user.id
    target_message = update.message.reply_to_message
    target_user_id = target_message.from_user.id
    target_username = target_message.from_user.first_name

    # Нельзя голосовать за самого себя
    if voter_id == target_user_id:
        await update.message.reply_text("❌ Нельзя голосовать за самого себя!")
        return

    # Добавляем голос
    message_id = target_message.message_id
    votes[chat_id][message_id]['tishe'].add(voter_id)

    vote_count = len(votes[chat_id][message_id]['tishe'])
    logger.info(f"Голос /tishe от {voter_id} за {target_user_id}. Всего голосов: {vote_count}")

    # Проверяем, достигнут ли порог
    if vote_count >= TISHE_VOTES_REQUIRED:
        try:
            # Запрещаем отправку медиа
            permissions = ChatPermissions(
                can_send_messages=True,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_polls=False,
                can_send_other_messages=False,
            )

            until_date = datetime.now() + RESTRICTION_DURATION
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user_id,
                permissions=permissions,
                until_date=until_date
            )

            # Сохраняем информацию об ограничении
            restrictions[chat_id][target_user_id] = {
                'type': 'media_ban',
                'until': until_date
            }

            # Очищаем голоса
            del votes[chat_id][message_id]

            await update.message.reply_text(
                f"🔇 {target_username} не может отправлять медиа в течение 24 часов!\n"
                f"Голосов набрано: {vote_count}/{TISHE_VOTES_REQUIRED}"
            )
            logger.info(f"Пользователь {target_user_id} получил запрет на медиа")

        except Exception as e:
            logger.error(f"Ошибка при ограничении пользователя: {e}")
            await update.message.reply_text(
                "❌ Не удалось применить ограничение. "
                "Убедитесь, что бот является администратором с правами на ограничение пользователей."
            )
    else:
        await update.message.reply_text(
            f"🔕 Голос учтён! {vote_count}/{TISHE_VOTES_REQUIRED} для запрета медиа"
        )


async def zaebal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /zaebal - полный мьют"""
    if not update.message.reply_to_message:
        await update.message.reply_text("⚠️ Эта команда должна быть ответом на сообщение!")
        return

    chat_id = update.effective_chat.id
    voter_id = update.effective_user.id
    target_message = update.message.reply_to_message
    target_user_id = target_message.from_user.id
    target_username = target_message.from_user.first_name

    # Нельзя голосовать за самого себя
    if voter_id == target_user_id:
        await update.message.reply_text("❌ Нельзя голосовать за самого себя!")
        return

    # Добавляем голос
    message_id = target_message.message_id
    votes[chat_id][message_id]['zaebal'].add(voter_id)

    vote_count = len(votes[chat_id][message_id]['zaebal'])
    logger.info(f"Голос /zaebal от {voter_id} за {target_user_id}. Всего голосов: {vote_count}")

    # Проверяем, достигнут ли порог
    if vote_count >= ZAEBAL_VOTES_REQUIRED:
        try:
            # Полный мьют - запрещаем всё
            permissions = ChatPermissions(
                can_send_messages=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_polls=False,
                can_send_other_messages=False,
            )

            until_date = datetime.now() + RESTRICTION_DURATION
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_user_id,
                permissions=permissions,
                until_date=until_date
            )

            # Сохраняем информацию об ограничении
            restrictions[chat_id][target_user_id] = {
                'type': 'full_mute',
                'until': until_date
            }

            # Очищаем голоса
            del votes[chat_id][message_id]

            await update.message.reply_text(
                f"🔇 {target_username} замьючен на 24 часа!\n"
                f"Голосов набрано: {vote_count}/{ZAEBAL_VOTES_REQUIRED}"
            )
            logger.info(f"Пользователь {target_user_id} получил полный мьют")

        except Exception as e:
            logger.error(f"Ошибка при мьюте пользователя: {e}")
            await update.message.reply_text(
                "❌ Не удалось замьютить пользователя. "
                "Убедитесь, что бот является администратором с правами на ограничение пользователей."
            )
    else:
        await update.message.reply_text(
            f"🔇 Голос учтён! {vote_count}/{ZAEBAL_VOTES_REQUIRED} для полного мьюта"
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать текущие активные ограничения"""
    chat_id = update.effective_chat.id

    if chat_id not in restrictions or not restrictions[chat_id]:
        await update.message.reply_text("✅ Нет активных ограничений")
        return

    status_text = "📋 Активные ограничения:\n\n"
    now = datetime.now()

    for user_id, restriction in list(restrictions[chat_id].items()):
        if restriction['until'] < now:
            # Ограничение истекло, удаляем
            del restrictions[chat_id][user_id]
            continue

        try:
            user = await context.bot.get_chat_member(chat_id, user_id)
            username = user.user.first_name
        except:
            username = f"ID:{user_id}"

        restriction_type = "🔇 Полный мьют" if restriction['type'] == 'full_mute' else "🔕 Запрет медиа"
        time_left = restriction['until'] - now
        hours = int(time_left.total_seconds() // 3600)
        minutes = int((time_left.total_seconds() % 3600) // 60)

        status_text += f"{restriction_type}: {username}\n"
        status_text += f"  Осталось: {hours}ч {minutes}м\n\n"

    if status_text == "📋 Активные ограничения:\n\n":
        status_text = "✅ Нет активных ограничений"

    await update.message.reply_text(status_text)


async def cleanup_old_votes(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очистка старых голосов (вызывается периодически)"""
    # Можно добавить логику очистки голосов старше определенного времени
    pass


def main() -> None:
    """Запуск бота"""
    logger.info("Запуск бота...")

    # Создаем приложение
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("tishe", tishe_command))
    application.add_handler(CommandHandler("zaebal", zaebal_command))
    application.add_handler(CommandHandler("status", status_command))

    # Запускаем бота
    logger.info("Бот запущен и готов к работе!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
