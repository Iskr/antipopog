#!/usr/bin/env python3
"""
Telegram бот для модерации группового чата.

Функционал:
- /tishe - 5+ голосов = запрет медиа на 1 час
- /zaebal - 5+ голосов = полный мьют на 1 час
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Dict, Optional
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
MUTE_GIF_URL = os.getenv('MUTE_GIF_URL')  # Общая гифка для обоих типов мьюта
TISHE_GIF_URL = os.getenv('TISHE_GIF_URL', MUTE_GIF_URL)  # Специфичная для /tishe
ZAEBAL_GIF_URL = os.getenv('ZAEBAL_GIF_URL', MUTE_GIF_URL)  # Специфичная для /zaebal

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не найден в .env файле")

# Хранилище голосов: {chat_id: {target_user_id: {'tishe': {voter_id: timestamp}, 'zaebal': {voter_id: timestamp}}}}
votes: Dict[int, Dict[int, Dict[str, Dict[int, datetime]]]] = defaultdict(
    lambda: defaultdict(lambda: {'tishe': {}, 'zaebal': {}})
)

# Хранилище активных ограничений: {chat_id: {user_id: {'type': str, 'until': datetime}}}
restrictions: Dict[int, Dict[int, Dict]] = defaultdict(dict)

# Хранилище кулдаунов голосования: {chat_id: {(voter_id, target_id, vote_type): datetime}}
vote_cooldowns: Dict[int, Dict[tuple, datetime]] = defaultdict(dict)

# Константы
TISHE_VOTES_REQUIRED = 5  # Количество голосов для запрета медиа
ZAEBAL_VOTES_REQUIRED = 5  # Количество голосов для полного мьюта
RESTRICTION_DURATION = timedelta(hours=1)  # Длительность ограничения
VOTE_EXPIRATION = timedelta(hours=1)  # Время жизни голоса
VOTE_COOLDOWN = timedelta(hours=1)  # Кулдаун между голосами одного пользователя против другого


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверка, является ли пользователь администратором чата"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception as e:
        logger.error(f"Ошибка при проверке прав администратора: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    help_text = """
🤖 Бот для модерации чата

Команды (в ответ на сообщение):
• /tishe - Голосовать за запрет медиа (нужно 5 голосов)
• /zaebal - Голосовать за полный мьют (нужно 5 голосов)
• /status - Проверить статус ограничений

Правила:
• Ограничения действуют 1 час
• Голоса сгорают через 1 час, если не набран порог
• Кулдауны раздельные для /tishe и /zaebal (1 час после успешного мьюта)
• Нельзя голосовать за администраторов
    """
    await update.message.reply_text(help_text)


def cleanup_expired_votes(chat_id: int, target_user_id: int, vote_type: str) -> None:
    """Удаление просроченных голосов"""
    now = datetime.now(timezone.utc)
    expired_voters = [
        voter_id
        for voter_id, timestamp in votes[chat_id][target_user_id][vote_type].items()
        if now - timestamp > VOTE_EXPIRATION
    ]

    for voter_id in expired_voters:
        del votes[chat_id][target_user_id][vote_type][voter_id]
        logger.info(f"Голос {vote_type} от {voter_id} за пользователя {target_user_id} истёк")


async def can_vote_for_user(chat_id: int, target_user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверка, можно ли голосовать за пользователя (не админ)"""
    try:
        member = await context.bot.get_chat_member(chat_id, target_user_id)
        return member.status not in ['creator', 'administrator']
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса пользователя: {e}")
        return True  # В случае ошибки разрешаем голосование


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
    now = datetime.now(timezone.utc)

    # Нельзя голосовать за самого себя
    if voter_id == target_user_id:
        await update.message.reply_text("❌ Нельзя голосовать за самого себя!")
        return

    # Проверяем, что пользователь не админ
    if not await can_vote_for_user(chat_id, target_user_id, context):
        await update.message.reply_text(f"❌ Нельзя голосовать за администраторов!")
        return

    # Проверяем кулдаун голосования для /tishe
    cooldown_key = (voter_id, target_user_id, 'tishe')
    if cooldown_key in vote_cooldowns[chat_id]:
        last_vote_time = vote_cooldowns[chat_id][cooldown_key]
        time_left = VOTE_COOLDOWN - (now - last_vote_time)
        if time_left.total_seconds() > 0:
            minutes = int(time_left.total_seconds() // 60)
            await update.message.reply_text(
                f"⏳ Вы уже голосовали /tishe за {target_username}. "
                f"Подождите ещё {minutes} мин."
            )
            return

    # Очищаем просроченные голоса
    cleanup_expired_votes(chat_id, target_user_id, 'tishe')

    # Проверяем, не голосовал ли уже этот пользователь
    if voter_id in votes[chat_id][target_user_id]['tishe']:
        await update.message.reply_text("⚠️ Вы уже голосовали!")
        return

    # Добавляем голос с временной меткой
    votes[chat_id][target_user_id]['tishe'][voter_id] = now

    vote_count = len(votes[chat_id][target_user_id]['tishe'])
    logger.info(f"Голос /tishe от {voter_id} за {target_user_id}. Всего голосов: {vote_count}")

    # Проверяем, достигнут ли порог
    if vote_count >= TISHE_VOTES_REQUIRED:
        try:
            # Устанавливаем кулдаун для всех кто голосовал
            for voted_user_id in votes[chat_id][target_user_id]['tishe'].keys():
                cooldown_key_for_voter = (voted_user_id, target_user_id, 'tishe')
                vote_cooldowns[chat_id][cooldown_key_for_voter] = now

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

            until_date = now + RESTRICTION_DURATION
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
            del votes[chat_id][target_user_id]

            await update.message.reply_text(
                f"🔇 {target_username} не может отправлять медиа в течение 1 часа!\n"
                f"Голосов набрано: {vote_count}/{TISHE_VOTES_REQUIRED}"
            )

            # Отправляем гифку, если указана
            if TISHE_GIF_URL:
                try:
                    await context.bot.send_animation(
                        chat_id=chat_id,
                        animation=TISHE_GIF_URL
                    )
                except Exception as e:
                    logger.error(f"Ошибка при отправке гифки: {e}")

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
    now = datetime.now(timezone.utc)

    # Нельзя голосовать за самого себя
    if voter_id == target_user_id:
        await update.message.reply_text("❌ Нельзя голосовать за самого себя!")
        return

    # Проверяем, что пользователь не админ
    if not await can_vote_for_user(chat_id, target_user_id, context):
        await update.message.reply_text(f"❌ Нельзя голосовать за администраторов!")
        return

    # Проверяем кулдаун голосования для /zaebal
    cooldown_key = (voter_id, target_user_id, 'zaebal')
    if cooldown_key in vote_cooldowns[chat_id]:
        last_vote_time = vote_cooldowns[chat_id][cooldown_key]
        time_left = VOTE_COOLDOWN - (now - last_vote_time)
        if time_left.total_seconds() > 0:
            minutes = int(time_left.total_seconds() // 60)
            await update.message.reply_text(
                f"⏳ Вы уже голосовали /zaebal за {target_username}. "
                f"Подождите ещё {minutes} мин."
            )
            return

    # Очищаем просроченные голоса
    cleanup_expired_votes(chat_id, target_user_id, 'zaebal')

    # Проверяем, не голосовал ли уже этот пользователь
    if voter_id in votes[chat_id][target_user_id]['zaebal']:
        await update.message.reply_text("⚠️ Вы уже голосовали!")
        return

    # Добавляем голос с временной меткой
    votes[chat_id][target_user_id]['zaebal'][voter_id] = now

    vote_count = len(votes[chat_id][target_user_id]['zaebal'])
    logger.info(f"Голос /zaebal от {voter_id} за {target_user_id}. Всего голосов: {vote_count}")

    # Проверяем, достигнут ли порог
    if vote_count >= ZAEBAL_VOTES_REQUIRED:
        try:
            # Устанавливаем кулдаун для всех кто голосовал
            for voted_user_id in votes[chat_id][target_user_id]['zaebal'].keys():
                cooldown_key_for_voter = (voted_user_id, target_user_id, 'zaebal')
                vote_cooldowns[chat_id][cooldown_key_for_voter] = now

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

            until_date = now + RESTRICTION_DURATION
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
            del votes[chat_id][target_user_id]

            await update.message.reply_text(
                f"🔇 {target_username} замьючен на 1 час!\n"
                f"Голосов набрано: {vote_count}/{ZAEBAL_VOTES_REQUIRED}"
            )

            # Отправляем гифку, если указана
            if ZAEBAL_GIF_URL:
                try:
                    await context.bot.send_animation(
                        chat_id=chat_id,
                        animation=ZAEBAL_GIF_URL
                    )
                except Exception as e:
                    logger.error(f"Ошибка при отправке гифки: {e}")

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
    now = datetime.now(timezone.utc)

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
