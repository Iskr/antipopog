#!/usr/bin/env python3
"""
Telegram бот для модерации группового чата.

Функционал:
- /tishe - 5+ голосов = запрет медиа на 3 часа
- /zaebal - 5+ голосов = полный мьют на 3 часа
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Dict, Optional
from pathlib import Path
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

# Константы
TISHE_VOTES_REQUIRED = 5  # Количество голосов для запрета медиа
ZAEBAL_VOTES_REQUIRED = 5  # Количество голосов для полного мьюта
RESTRICTION_DURATION = timedelta(hours=3)  # Базовая длительность ограничения
VOTE_EXPIRATION = timedelta(hours=24)  # Время жизни голоса (сутки)
BAN_INCREMENT = timedelta(minutes=5)  # Увеличение времени бана за каждый предыдущий бан
DATA_FILE = Path(__file__).parent / "ban_data.json"  # Файл для хранения данных


# Хранилище истории банов: {chat_id: {user_id: {"count": int, "username": str}}}
ban_history: Dict[int, Dict[int, Dict]] = defaultdict(dict)


def load_data() -> None:
    """Загрузка всех данных из файла"""
    global ban_history, votes
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

                # Загружаем историю банов
                for chat_id_str, users in data.get('ban_history', {}).items():
                    chat_id = int(chat_id_str)
                    for user_id_str, user_data in users.items():
                        user_id = int(user_id_str)
                        ban_history[chat_id][user_id] = user_data

                # Загружаем голоса (с конвертацией datetime)
                for chat_id_str, targets in data.get('votes', {}).items():
                    chat_id = int(chat_id_str)
                    for target_id_str, vote_types in targets.items():
                        target_id = int(target_id_str)
                        for vote_type in ['tishe', 'zaebal']:
                            if vote_type in vote_types:
                                for voter_id_str, timestamp_str in vote_types[vote_type].items():
                                    voter_id = int(voter_id_str)
                                    timestamp = datetime.fromisoformat(timestamp_str)
                                    votes[chat_id][target_id][vote_type][voter_id] = timestamp

            logger.info(f"Загружены данные из {DATA_FILE}")
    except Exception as e:
        logger.error(f"Ошибка при загрузке данных: {e}")


def save_data() -> None:
    """Сохранение всех данных в файл"""
    try:
        # Подготавливаем голоса для сериализации
        votes_serializable = {}
        for chat_id, targets in votes.items():
            votes_serializable[str(chat_id)] = {}
            for target_id, vote_types in targets.items():
                votes_serializable[str(chat_id)][str(target_id)] = {}
                for vote_type in ['tishe', 'zaebal']:
                    if vote_types.get(vote_type):
                        votes_serializable[str(chat_id)][str(target_id)][vote_type] = {
                            str(voter_id): timestamp.isoformat()
                            for voter_id, timestamp in vote_types[vote_type].items()
                        }

        data = {
            'ban_history': {
                str(chat_id): {
                    str(user_id): user_data
                    for user_id, user_data in users.items()
                }
                for chat_id, users in ban_history.items()
            },
            'votes': votes_serializable
        }
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Данные сохранены в {DATA_FILE}")
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных: {e}")


def get_ban_count(chat_id: int, user_id: int) -> int:
    """Получить количество банов пользователя"""
    if user_id in ban_history[chat_id]:
        return ban_history[chat_id][user_id].get('count', 0)
    return 0


def increment_ban_count(chat_id: int, user_id: int, username: str) -> int:
    """Увеличить счётчик банов и вернуть новое значение"""
    if user_id not in ban_history[chat_id]:
        ban_history[chat_id][user_id] = {'count': 0, 'username': username}

    ban_history[chat_id][user_id]['count'] += 1
    ban_history[chat_id][user_id]['username'] = username  # Обновляем имя
    save_data()
    return ban_history[chat_id][user_id]['count']


def get_restriction_duration(ban_count: int) -> timedelta:
    """Рассчитать длительность бана на основе количества предыдущих банов"""
    # ban_count - это уже обновлённый счётчик (после текущего бана)
    # Первый бан: 3 часа, второй: 3 часа 5 минут, третий: 3 часа 10 минут и т.д.
    extra_time = BAN_INCREMENT * (ban_count - 1)
    return RESTRICTION_DURATION + extra_time


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
• /status - Проверить статус и статистику нарушений

Правила:
• Базовое ограничение: 3 часа
• Каждый следующий бан: +5 минут к времени
• Голоса живут 24 часа (повторный голос продлевает)
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

    # Очищаем просроченные голоса
    cleanup_expired_votes(chat_id, target_user_id, 'tishe')

    # Проверяем, не голосовал ли уже этот пользователь
    already_voted = voter_id in votes[chat_id][target_user_id]['tishe']

    # Добавляем или обновляем голос (продлеваем на сутки)
    votes[chat_id][target_user_id]['tishe'][voter_id] = now

    # Сохраняем данные
    save_data()

    vote_count = len(votes[chat_id][target_user_id]['tishe'])

    if already_voted:
        logger.info(f"Голос /tishe от {voter_id} за {target_user_id} продлён. Всего голосов: {vote_count}")
    else:
        logger.info(f"Голос /tishe от {voter_id} за {target_user_id}. Всего голосов: {vote_count}")

    # Проверяем, достигнут ли порог
    if vote_count >= TISHE_VOTES_REQUIRED:
        try:
            # Увеличиваем счётчик банов и получаем длительность
            ban_count = increment_ban_count(chat_id, target_user_id, target_username)
            restriction_duration = get_restriction_duration(ban_count)

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

            until_date = now + restriction_duration
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

            # Форматируем длительность
            hours = int(restriction_duration.total_seconds() // 3600)
            minutes = int((restriction_duration.total_seconds() % 3600) // 60)
            duration_text = f"{hours}ч" if minutes == 0 else f"{hours}ч {minutes}м"
            extra_text = f" (+{(ban_count-1)*5}м)" if ban_count > 1 else ""

            await update.message.reply_text(
                f"🔇 {target_username} не может отправлять медиа в течение {duration_text}{extra_text}!\n"
                f"Голосов набрано: {vote_count}/{TISHE_VOTES_REQUIRED}\n"
                f"Нарушений всего: {ban_count}"
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

            logger.info(f"Пользователь {target_user_id} получил запрет на медиа (бан #{ban_count})")

        except Exception as e:
            logger.error(f"Ошибка при ограничении пользователя: {e}")
            await update.message.reply_text(
                "❌ Не удалось применить ограничение. "
                "Убедитесь, что бот является администратором с правами на ограничение пользователей."
            )
    else:
        if already_voted:
            await update.message.reply_text(
                f"🔄 Голос продлён! {vote_count}/{TISHE_VOTES_REQUIRED} для запрета медиа"
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

    # Очищаем просроченные голоса
    cleanup_expired_votes(chat_id, target_user_id, 'zaebal')

    # Проверяем, не голосовал ли уже этот пользователь
    already_voted = voter_id in votes[chat_id][target_user_id]['zaebal']

    # Добавляем или обновляем голос (продлеваем на сутки)
    votes[chat_id][target_user_id]['zaebal'][voter_id] = now

    # Сохраняем данные
    save_data()

    vote_count = len(votes[chat_id][target_user_id]['zaebal'])

    if already_voted:
        logger.info(f"Голос /zaebal от {voter_id} за {target_user_id} продлён. Всего голосов: {vote_count}")
    else:
        logger.info(f"Голос /zaebal от {voter_id} за {target_user_id}. Всего голосов: {vote_count}")

    # Проверяем, достигнут ли порог
    if vote_count >= ZAEBAL_VOTES_REQUIRED:
        try:
            # Увеличиваем счётчик банов и получаем длительность
            ban_count = increment_ban_count(chat_id, target_user_id, target_username)
            restriction_duration = get_restriction_duration(ban_count)

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

            until_date = now + restriction_duration
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

            # Форматируем длительность
            hours = int(restriction_duration.total_seconds() // 3600)
            minutes = int((restriction_duration.total_seconds() % 3600) // 60)
            duration_text = f"{hours}ч" if minutes == 0 else f"{hours}ч {minutes}м"
            extra_text = f" (+{(ban_count-1)*5}м)" if ban_count > 1 else ""

            await update.message.reply_text(
                f"🔇 {target_username} замьючен на {duration_text}{extra_text}!\n"
                f"Голосов набрано: {vote_count}/{ZAEBAL_VOTES_REQUIRED}\n"
                f"Нарушений всего: {ban_count}"
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

            logger.info(f"Пользователь {target_user_id} получил полный мьют (бан #{ban_count})")

        except Exception as e:
            logger.error(f"Ошибка при мьюте пользователя: {e}")
            await update.message.reply_text(
                "❌ Не удалось замьютить пользователя. "
                "Убедитесь, что бот является администратором с правами на ограничение пользователей."
            )
    else:
        if already_voted:
            await update.message.reply_text(
                f"🔄 Голос продлён! {vote_count}/{ZAEBAL_VOTES_REQUIRED} для полного мьюта"
            )
        else:
            await update.message.reply_text(
                f"🔇 Голос учтён! {vote_count}/{ZAEBAL_VOTES_REQUIRED} для полного мьюта"
            )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать текущие активные ограничения и голосования"""
    chat_id = update.effective_chat.id
    now = datetime.now(timezone.utc)

    # Проверяем ограничения
    has_restrictions = False
    restrictions_text = "📋 Активные ограничения:\n\n"

    if chat_id in restrictions and restrictions[chat_id]:
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

            restrictions_text += f"{restriction_type}: {username}\n"
            restrictions_text += f"  Осталось: {hours}ч {minutes}м\n\n"
            has_restrictions = True

    # Проверяем активные голосования
    has_votes = False
    votes_text = "📊 Активные голосования:\n\n"

    if chat_id in votes and votes[chat_id]:
        for target_user_id, vote_types in votes[chat_id].items():
            # Очищаем просроченные голоса
            cleanup_expired_votes(chat_id, target_user_id, 'tishe')
            cleanup_expired_votes(chat_id, target_user_id, 'zaebal')

            tishe_count = len(vote_types['tishe'])
            zaebal_count = len(vote_types['zaebal'])

            # Показываем только если есть активные голоса
            if tishe_count > 0 or zaebal_count > 0:
                try:
                    user = await context.bot.get_chat_member(chat_id, target_user_id)
                    username = user.user.first_name
                except:
                    username = f"ID:{target_user_id}"

                votes_text += f"• За {username}:\n"
                if tishe_count > 0:
                    votes_text += f"  /tishe: {tishe_count}/{TISHE_VOTES_REQUIRED} голосов\n"
                if zaebal_count > 0:
                    votes_text += f"  /zaebal: {zaebal_count}/{ZAEBAL_VOTES_REQUIRED} голосов\n"
                votes_text += "\n"
                has_votes = True

    # Проверяем историю банов
    has_ban_history = False
    ban_history_text = "📈 Статистика нарушений:\n\n"

    if chat_id in ban_history and ban_history[chat_id]:
        # Сортируем по количеству нарушений (больше = выше)
        sorted_users = sorted(
            ban_history[chat_id].items(),
            key=lambda x: x[1].get('count', 0),
            reverse=True
        )

        for user_id, user_data in sorted_users:
            count = user_data.get('count', 0)
            if count > 0:
                username = user_data.get('username', f"ID:{user_id}")
                # Рассчитываем доп. время для следующего бана
                next_extra = count * 5  # минут
                ban_history_text += f"• {username}: {count} нарушений"
                if count > 0:
                    ban_history_text += f" (следующий бан: +{next_extra}м)"
                ban_history_text += "\n"
                has_ban_history = True

    # Формируем итоговое сообщение
    if not has_restrictions and not has_votes and not has_ban_history:
        final_text = "✅ Нет активных ограничений, голосований и истории нарушений"
    else:
        final_text = ""
        if has_restrictions:
            final_text += restrictions_text
        if has_votes:
            if has_restrictions:
                final_text += "➖➖➖➖➖➖➖➖➖\n\n"
            final_text += votes_text
        if has_ban_history:
            if has_restrictions or has_votes:
                final_text += "➖➖➖➖➖➖➖➖➖\n\n"
            final_text += ban_history_text

    await update.message.reply_text(final_text.strip())


async def cleanup_old_votes(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очистка старых голосов (вызывается периодически)"""
    # Можно добавить логику очистки голосов старше определенного времени
    pass


def main() -> None:
    """Запуск бота"""
    logger.info("Запуск бота...")

    # Загружаем все данные из файла
    load_data()

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
