import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import aiosqlite
import sqlite3
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters.state import StateFilter
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
OWNER_ID = int(os.getenv("OWNER_ID", "PUT_YOUR_TELEGRAM_ID"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "duty_bot.db"))

logging.basicConfig(level=logging.INFO)


class SkipDutyForm(StatesGroup):
    waiting_reason = State()


class ChangeDutyReasonForm(StatesGroup):
    waiting_reason = State()


class ReplaceDutyForm(StatesGroup):
    waiting_data = State()


class AdminInputForm(StatesGroup):
    waiting_assign_date = State()
    waiting_absent_date = State()
    waiting_plus_one = State()


@dataclass
class UserRow:
    tg_id: int
    username: Optional[str]
    last_name: str
    first_name: str
    role: str
    duty_count: int
    has_pm: bool
    sort_order: Optional[int]
    in_queue: bool


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    tg_id INTEGER PRIMARY KEY,
                    username TEXT NULL,
                    last_name TEXT NOT NULL,
                    first_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('owner', 'admin', 'moderator', 'student')),
                    duty_count INTEGER NOT NULL DEFAULT 0,
                    has_pm INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NULL,
                    in_queue INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            try:
                await db.execute("ALTER TABLE users ADD COLUMN sort_order INTEGER NULL")
            except sqlite3.OperationalError:
                pass
            try:
                await db.execute("ALTER TABLE users ADD COLUMN in_queue INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    current_index INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS duty_log (
                    date TEXT NOT NULL,
                    user1_id INTEGER NOT NULL,
                    user2_id INTEGER NOT NULL,
                    FOREIGN KEY (user1_id) REFERENCES users(tg_id),
                    FOREIGN KEY (user2_id) REFERENCES users(tg_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    group_chat_id INTEGER NULL,
                    status TEXT NOT NULL DEFAULT 'normal'
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS absences (
                    date TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    marked_by INTEGER NOT NULL,
                    PRIMARY KEY (date, user_id),
                    FOREIGN KEY (user_id) REFERENCES users(tg_id),
                    FOREIGN KEY (marked_by) REFERENCES users(tg_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS action_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_type TEXT NOT NULL,
                    initiator_id INTEGER NOT NULL,
                    target_user_id INTEGER NULL,
                    payload TEXT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (initiator_id) REFERENCES users(tg_id),
                    FOREIGN KEY (target_user_id) REFERENCES users(tg_id)
                )
                """
            )
            await db.execute("INSERT OR IGNORE INTO queue(id, current_index) VALUES(1, 0)")
            await db.execute("INSERT OR IGNORE INTO settings(id, group_chat_id, status) VALUES(1, NULL, 'normal')")
            await db.commit()

    async def upsert_user(
        self,
        tg_id: int,
        username: Optional[str],
        last_name: str,
        first_name: str,
        role: Optional[str] = None,
        has_pm: Optional[bool] = None,
        sort_order: Optional[int] = None,
        in_queue: Optional[bool] = None,
    ) -> None:
        existing = await self.get_user(tg_id)
        if existing:
            new_role = role or existing.role
            new_has_pm = int(existing.has_pm if has_pm is None else has_pm)
            new_sort_order = existing.sort_order if sort_order is None else sort_order
            new_in_queue = int(existing.in_queue if in_queue is None else in_queue)
        else:
            new_role = role or ("owner" if tg_id == OWNER_ID else "student")
            new_has_pm = int(bool(has_pm))
            new_sort_order = sort_order
            new_in_queue = int(bool(in_queue))

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users(tg_id, username, last_name, first_name, role, has_pm, sort_order, in_queue)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    username=excluded.username,
                    last_name=excluded.last_name,
                    first_name=excluded.first_name,
                    role=?,
                    has_pm=?,
                    sort_order=?,
                    in_queue=?
                """,
                (
                    tg_id,
                    username,
                    last_name,
                    first_name,
                    new_role,
                    new_has_pm,
                    new_sort_order,
                    new_in_queue,
                    new_role,
                    new_has_pm,
                    new_sort_order,
                    new_in_queue,
                ),
            )
            await db.commit()

    async def get_user(self, tg_id: int) -> Optional[UserRow]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT tg_id, username, last_name, first_name, role, duty_count, has_pm, sort_order, in_queue FROM users WHERE tg_id=?",
                (tg_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return UserRow(*row)

    async def touch_user(self, tg_id: int, username: Optional[str], has_pm: Optional[bool] = None) -> bool:
        user = await self.get_user(tg_id)
        if not user:
            return False
        new_has_pm = int(user.has_pm if has_pm is None else has_pm)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET username=?, has_pm=? WHERE tg_id=?",
                (username, new_has_pm, tg_id),
            )
            await db.commit()
        return True

    async def set_role(self, tg_id: int, role: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE users SET role=? WHERE tg_id=?", (role, tg_id))
            await db.commit()

    async def update_student(
        self,
        tg_id: int,
        last_name: Optional[str] = None,
        first_name: Optional[str] = None,
        username: Optional[str] = None,
        sort_order: Optional[int] = None,
    ) -> bool:
        user = await self.get_user(tg_id)
        if not user or user.role != "student":
            return False
        new_last_name = last_name if last_name is not None else user.last_name
        new_first_name = first_name if first_name is not None else user.first_name
        new_username = username if username is not None else user.username
        await self.upsert_user(
            tg_id=tg_id,
            username=new_username,
            last_name=new_last_name,
            first_name=new_first_name,
            role="student",
            has_pm=user.has_pm,
            sort_order=sort_order if sort_order is not None else user.sort_order,
        )
        return True

    async def delete_user(self, tg_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("DELETE FROM users WHERE tg_id=?", (tg_id,))
            await db.commit()
        return cursor.rowcount > 0

    async def get_admin_ids(self) -> list[int]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT tg_id FROM users WHERE role IN ('owner','admin')")
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_users_by_role(self, role: str) -> list[UserRow]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT tg_id, username, last_name, first_name, role, duty_count, has_pm, sort_order, in_queue
                FROM users
                WHERE role=?
                ORDER BY last_name, first_name, tg_id
                """,
                (role,),
            )
            rows = await cursor.fetchall()
        return [UserRow(*r) for r in rows]

    async def increment_duty_count(self, tg_id: int, value: int = 1) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("UPDATE users SET duty_count=duty_count+? WHERE tg_id=?", (value, tg_id))
            await db.commit()
        return cursor.rowcount > 0

    async def set_group_chat(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE settings SET group_chat_id=? WHERE id=1", (chat_id,))
            await db.commit()

    async def clear_group_chat(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE settings SET group_chat_id=NULL WHERE id=1")
            await db.commit()

    async def get_settings(self) -> tuple[Optional[int], str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT group_chat_id, status FROM settings WHERE id=1")
            row = await cursor.fetchone()
        return row[0], row[1]

    async def set_status(self, status: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE settings SET status=? WHERE id=1", (status,))
            await db.commit()

    async def add_absence(self, user_id: int, day: str, marked_by: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO absences(date, user_id, marked_by) VALUES(?, ?, ?)",
                (day, user_id, marked_by),
            )
            await db.commit()

    async def get_absent_ids(self, day: str) -> set[int]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT user_id FROM absences WHERE date=?", (day,))
            rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def get_absent_rows(self, day: str) -> list[tuple[int, int]]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT user_id, marked_by FROM absences WHERE date=? ORDER BY user_id", (day,))
            rows = await cursor.fetchall()
        return [(r[0], r[1]) for r in rows]

    async def get_students(self) -> list[UserRow]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT tg_id, username, last_name, first_name, role, duty_count, has_pm, sort_order, in_queue
                FROM users
                WHERE in_queue=1 AND sort_order IS NOT NULL
                ORDER BY sort_order, rowid
                """
            )
            rows = await cursor.fetchall()
        return [UserRow(*r) for r in rows]

    async def find_queue_users_by_last_name(self, last_name: str) -> list[UserRow]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT tg_id, username, last_name, first_name, role, duty_count, has_pm, sort_order, in_queue
                FROM users
                WHERE in_queue=1 AND lower(last_name)=lower(?)
                ORDER BY sort_order, tg_id
                """,
                (last_name,),
            )
            rows = await cursor.fetchall()
        return [UserRow(*r) for r in rows]

    async def get_queue_index(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT current_index FROM queue WHERE id=1")
            row = await cursor.fetchone()
        return row[0]

    async def set_queue_index(self, index: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE queue SET current_index=? WHERE id=1", (index,))
            await db.commit()

    async def log_duty(self, day: str, user1_id: int, user2_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO duty_log(date, user1_id, user2_id) VALUES(?, ?, ?)", (day, user1_id, user2_id))
            await db.execute("UPDATE users SET duty_count=duty_count+1 WHERE tg_id IN (?, ?)", (user1_id, user2_id))
            await db.commit()

    async def get_duty_by_date(self, day: str) -> Optional[tuple[int, int]]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT user1_id, user2_id FROM duty_log WHERE date=? LIMIT 1", (day,))
            row = await cursor.fetchone()
        if not row:
            return None
        return row[0], row[1]

    async def replace_duty_member(self, day: str, old_user_id: int, new_user_id: int) -> bool:
        duty = await self.get_duty_by_date(day)
        if not duty:
            return False
        u1, u2 = duty
        if old_user_id not in {u1, u2} or new_user_id in {u1, u2}:
            return False
        new_u1, new_u2 = (new_user_id, u2) if u1 == old_user_id else (u1, new_user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE duty_log SET user1_id=?, user2_id=? WHERE date=?", (new_u1, new_u2, day))
            await db.execute(
                "UPDATE users SET duty_count=CASE WHEN duty_count>0 THEN duty_count-1 ELSE 0 END WHERE tg_id=?",
                (old_user_id,),
            )
            await db.execute("UPDATE users SET duty_count=duty_count+1 WHERE tg_id=?", (new_user_id,))
            await db.commit()
        return True

    async def clear_duty_for_date(self, day: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT user1_id, user2_id FROM duty_log WHERE date=?", (day,))
            rows = await cursor.fetchall()
            if not rows:
                return 0
            counts: dict[int, int] = {}
            for u1, u2 in rows:
                counts[u1] = counts.get(u1, 0) + 1
                counts[u2] = counts.get(u2, 0) + 1
            for user_id, cnt in counts.items():
                await db.execute(
                    "UPDATE users SET duty_count=CASE WHEN duty_count>=? THEN duty_count-? ELSE 0 END WHERE tg_id=?",
                    (cnt, cnt, user_id),
                )
            await db.execute("DELETE FROM duty_log WHERE date=?", (day,))
            await db.commit()
        return len(rows)

    async def remove_absence(self, user_id: int, day: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM absences WHERE date=? AND user_id=?", (day, user_id))
            await db.commit()

    async def clear_today_duty(self, day: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT user1_id, user2_id FROM duty_log WHERE date=?", (day,))
            rows = await cursor.fetchall()
            if not rows:
                return 0
            counts: dict[int, int] = {}
            for u1, u2 in rows:
                counts[u1] = counts.get(u1, 0) + 1
                counts[u2] = counts.get(u2, 0) + 1
            for user_id, cnt in counts.items():
                await db.execute(
                    "UPDATE users SET duty_count=CASE WHEN duty_count>=? THEN duty_count-? ELSE 0 END WHERE tg_id=?",
                    (cnt, cnt, user_id),
                )
            await db.execute("DELETE FROM duty_log WHERE date=?", (day,))
            await db.commit()
        return len(rows)

    async def get_stats(self, tg_id: int) -> tuple[int, int]:
        async with aiosqlite.connect(self.path) as db:
            c1 = await db.execute("SELECT duty_count FROM users WHERE tg_id=?", (tg_id,))
            duty_count_row = await c1.fetchone()
            c2 = await db.execute("SELECT COUNT(*) FROM duty_log WHERE user1_id=? OR user2_id=?", (tg_id, tg_id))
            total_log_row = await c2.fetchone()
        duty_count = duty_count_row[0] if duty_count_row else 0
        return duty_count, total_log_row[0]

    async def get_all_duty_counts(self) -> list[tuple[str, str, int]]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT last_name, first_name, duty_count
                FROM users
                WHERE in_queue=1
                ORDER BY sort_order IS NULL, sort_order, rowid
                """
            )
            rows = await cursor.fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    async def get_duty_dates_page(self, page: int, page_size: int = 10) -> tuple[list[str], int]:
        offset = page * page_size
        async with aiosqlite.connect(self.path) as db:
            c_total = await db.execute("SELECT COUNT(DISTINCT date) FROM duty_log")
            total_dates = (await c_total.fetchone())[0]
            cursor = await db.execute(
                "SELECT DISTINCT date FROM duty_log ORDER BY date DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )
            rows = await cursor.fetchall()
        return [r[0] for r in rows], total_dates

    async def get_activity_dates_page(self, page: int, page_size: int = 10) -> tuple[list[str], int]:
        offset = page * page_size
        async with aiosqlite.connect(self.path) as db:
            c_total = await db.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT date FROM duty_log
                    UNION
                    SELECT date FROM absences
                )
                """
            )
            total_dates = (await c_total.fetchone())[0]
            cursor = await db.execute(
                """
                SELECT date FROM (
                    SELECT date FROM duty_log
                    UNION
                    SELECT date FROM absences
                )
                ORDER BY date DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            )
            rows = await cursor.fetchall()
        return [r[0] for r in rows], total_dates

    async def create_request(
        self,
        request_type: str,
        initiator_id: int,
        target_user_id: Optional[int] = None,
        payload: Optional[str] = None,
    ) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT INTO action_requests(request_type, initiator_id, target_user_id, payload)
                VALUES(?, ?, ?, ?)
                """,
                (request_type, initiator_id, target_user_id, payload),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_request(self, request_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                SELECT id, request_type, initiator_id, target_user_id, payload, status
                FROM action_requests WHERE id=?
                """,
                (request_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "request_type": row[1],
            "initiator_id": row[2],
            "target_user_id": row[3],
            "payload": row[4],
            "status": row[5],
        }

    async def close_request(self, request_id: int, status: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE action_requests SET status=? WHERE id=?", (status, request_id))
            await db.commit()


db = Database(DB_PATH)
router = Router()
sent_requests_tracking: dict[int, list[tuple[int, int]]] = {}


def is_admin_or_owner(role: str) -> bool:
    return role in {"owner", "admin"}


def can_manage_day(role: str) -> bool:
    return role in {"owner", "admin", "moderator"}


def mention_html(user: UserRow) -> str:
    return f"<a href='tg://user?id={user.tg_id}'>{full_name(user)}</a>"


def full_name(user: UserRow) -> str:
    last = (user.last_name or "").strip()
    first = (user.first_name or "").strip()
    if last in {"-", "_"}:
        last = ""
    if first in {"-", "_"}:
        first = ""
    name = f"{last} {first}".strip()
    if name:
        return name
    if user.username:
        return user.username
    return f"ID {user.tg_id}"


async def ensure_user_from_message(message: Message, has_pm: Optional[bool] = None) -> UserRow:
    tg_user = message.from_user
    stored = await db.get_user(tg_user.id)
    if stored:
        await db.touch_user(tg_user.id, tg_user.username, has_pm=has_pm)
        refreshed = await db.get_user(tg_user.id)
        return refreshed
    if tg_user.id == OWNER_ID:
        last_name = tg_user.last_name or "Owner"
        first_name = tg_user.first_name or "Owner"
        await db.upsert_user(tg_user.id, tg_user.username, last_name, first_name, role="owner", has_pm=has_pm)
        return await db.get_user(tg_user.id)
    raise PermissionError("USER_NOT_REGISTERED")


async def get_actor_or_deny(message: Message, has_pm: Optional[bool] = None) -> Optional[UserRow]:
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("Ця команда доступна тільки в особистих повідомленнях з ботом.")
        return None
    try:
        return await ensure_user_from_message(message, has_pm=has_pm)
    except PermissionError:
        await message.answer("Тебе немає у списку групи. Звернись до admin/owner.")
        return None


async def notify_admins(bot: Bot, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    admin_ids = await db.get_admin_ids()
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except TelegramForbiddenError:
            logging.warning("Admin %s blocked bot", admin_id)


def commands_for_role(role: str) -> list[BotCommand]:
    common = [
        BotCommand(command="start", description="Запуск або перезапуск бота"),
        BotCommand(command="help", description="Список команд"),
        BotCommand(command="menu", description="Головне меню"),
        BotCommand(command="stats", description="Моя статистика"),
        BotCommand(command="students", description="Список черги"),
        BotCommand(command="stats_all", description="Загальна статистика"),
        BotCommand(command="cancel", description="Скасувати поточну дію"),
    ]
    if role == "student":
        return common + [BotCommand(command="skip_duty", description="Запит на заміну/пропуск чергування")]
    if role == "moderator":
        return common + [
            BotCommand(command="assign", description="Призначити чергових на сьогодні"),
            BotCommand(command="staff_list", description="Список owner/admin/moderator"),
            BotCommand(command="rm_mod", description="Зняти модератора"),
        ]
    if role == "admin":
        return common + [
            BotCommand(command="assign", description="Призначити чергових на сьогодні"),
            BotCommand(command="replace_duty", description="Ручна заміна чергового"),
            BotCommand(command="absent_on", description="Відсутні на дату"),
            BotCommand(command="set_group_id", description="Вручну задати group_chat_id"),
            BotCommand(command="unset_group", description="Відв'язати групу"),
            BotCommand(command="group_info", description="Поточна група і статус"),
            BotCommand(command="add_student", description="Додати або оновити в черзі"),
            BotCommand(command="edit_student", description="Змінити дані студента"),
            BotCommand(command="del_student", description="Видалити з черги"),
            BotCommand(command="add_mod", description="Призначити модератора"),
            BotCommand(command="rm_mod", description="Зняти модератора"),
            BotCommand(command="sync_user", description="Оновити username одного"),
            BotCommand(command="sync_students", description="Оновити username всіх"),
            BotCommand(command="staff_list", description="Список owner/admin/moderator"),
        ]
    if role == "owner":
        return common + [
            BotCommand(command="assign", description="Призначити чергових на сьогодні"),
            BotCommand(command="replace_duty", description="Ручна заміна чергового"),
            BotCommand(command="absent_on", description="Відсутні на дату"),
            BotCommand(command="set_group_id", description="Вручну задати group_chat_id"),
            BotCommand(command="unset_group", description="Відв'язати групу"),
            BotCommand(command="group_info", description="Поточна група і статус"),
            BotCommand(command="add_student", description="Додати або оновити в черзі"),
            BotCommand(command="edit_student", description="Змінити дані студента"),
            BotCommand(command="del_student", description="Видалити з черги"),
            BotCommand(command="add_mod", description="Призначити модератора"),
            BotCommand(command="rm_mod", description="Зняти модератора"),
            BotCommand(command="add_admin", description="Призначити адміністратора"),
            BotCommand(command="rm_admin", description="Зняти адміністратора"),
            BotCommand(command="sync_user", description="Оновити username одного"),
            BotCommand(command="sync_students", description="Оновити username всіх"),
            BotCommand(command="staff_list", description="Список owner/admin/moderator"),
        ]
    return common


async def set_commands_for_user(bot: Bot, user: UserRow) -> None:
    await bot.set_my_commands(
        commands_for_role(user.role),
        scope=BotCommandScopeChat(chat_id=user.tg_id),
    )

async def send_routed_request(bot: Bot, req_id: int, text: str, kb: InlineKeyboardMarkup, initiator_role: str):
    target_ids = []
    
    if initiator_role == "admin":
        target_ids = [OWNER_ID]
    elif initiator_role in {"moderator", "student"}:

        target_ids = await db.get_admin_ids() 

    sent_requests_tracking[req_id] = []
    
    for t_id in target_ids:
        try:
            msg = await bot.send_message(t_id, text, reply_markup=kb)
            sent_requests_tracking[req_id].append((t_id, msg.message_id))
        except Exception as e:
            logging.warning(f"Не вдалося надіслати запит для ID {t_id}: {e}")


async def owner_daily_reminder_loop(bot: Bot) -> None:
    last_sent_day: Optional[str] = None
    last_reset_day: Optional[str] = None
    while True:
        now = datetime.now()
        today = _today_str()
        if last_reset_day != today:
            _group_id, status = await db.get_settings()
            if status != "normal":
                await db.set_status("normal")
            last_reset_day = today
        if now.hour == 10 and now.minute == 30 and last_sent_day != today:
            _group_id, status = await db.get_settings()
            existing = await db.get_duty_by_date(today)
            is_weekend = now.weekday() >= 5
            if not existing and status != "remote" and not is_weekend:
                owner = await db.get_user(OWNER_ID)
                if owner:
                    try:
                        await bot.send_message(
                            OWNER_ID,
                            "⏰ Нагадування: на сьогодні ще не призначені чергові. Признач їх, будь ласка.",
                        )
                    except TelegramForbiddenError:
                        logging.warning("Owner blocked bot")
            last_sent_day = today
        await asyncio.sleep(20)


async def refresh_user_username_from_group(bot: Bot, tg_id: int) -> bool:
    group_chat_id, _ = await db.get_settings()
    if not group_chat_id:
        return False
    user = await db.get_user(tg_id)
    if not user:
        return False
    try:
        member = await bot.get_chat_member(group_chat_id, tg_id)
    except Exception:
        return False
    new_username = member.user.username
    if new_username == user.username:
        return False
    await db.upsert_user(
        tg_id=tg_id,
        username=new_username,
        last_name=user.last_name,
        first_name=user.first_name,
        role=user.role,
        has_pm=user.has_pm,
        sort_order=user.sort_order,
    )
    return True


def _today_str() -> str:
    return date.today().isoformat()


def is_assignment_time_allowed_for_today(target_day: str) -> bool:
    if target_day != _today_str():
        return True
    now = datetime.now().time()
    return (now.hour > 9 or (now.hour == 9 and now.minute >= 0)) and (
        now.hour < 14 or (now.hour == 14 and now.minute == 0)
    )


def is_today_window_open() -> bool:
    return is_assignment_time_allowed_for_today(_today_str())


TIME_WINDOW_TEXT = "Операції з чергуванням доступні лише з 09:00 до 14:00."


async def assign_duty_and_notify(bot: Bot, triggered_by: int, target_day: Optional[str] = None) -> str:
    duty_day = target_day or _today_str()
    group_chat_id, status = await db.get_settings()
    if not group_chat_id:
        return "Група не налаштована. Використайте /set_group у груповому чаті."
    if status == "remote":
        return "Сьогодні встановлено дистанційний день. Призначення чергових пропущено."

    students = await db.get_students()
    for st in students:
        await refresh_user_username_from_group(bot, st.tg_id)
    students = await db.get_students()
    if len(students) < 2:
        return "Недостатньо студентів у БД для призначення 2 чергових."

    absent = await db.get_absent_ids(duty_day)
    order_students = sorted([s for s in students if s.sort_order is not None], key=lambda s: (s.sort_order, s.tg_id))
    eligible_students = [s for s in order_students if s.tg_id not in absent]
    if len(eligible_students) < 2:
        return "Недостатньо студентів з вказаними номерами (sort_order) для призначення."

    order_values = [s.sort_order for s in order_students]
    start_order = await db.get_queue_index()
    if start_order not in order_values:
        start_order = order_values[0]
    start_idx = 0
    for i, val in enumerate(order_values):
        if val >= start_order:
            start_idx = i
            break

    order_positions = {student.tg_id: i for i, student in enumerate(order_students)}

    def cyclic_distance(student: UserRow) -> int:
        return (order_positions[student.tg_id] - start_idx) % len(order_students)

    # Balance duty_count first; queue order only decides between equal counts.
    picked = sorted(eligible_students, key=lambda s: (s.duty_count, cyclic_distance(s), s.sort_order, s.tg_id))[:2]
    last_picked = max(picked, key=cyclic_distance)
    next_idx = (order_positions[last_picked.tg_id] + 1) % len(order_students)
    next_order = order_students[next_idx].sort_order
    await db.set_queue_index(next_order)
    await db.log_duty(duty_day, picked[0].tg_id, picked[1].tg_id)

    day_text = "Сьогодні" if duty_day == _today_str() else f"На {duty_day}"
    group_text = f"🔔 {day_text} чергові: {mention_html(picked[0])} та {mention_html(picked[1])}"
    await bot.send_message(group_chat_id, group_text)

#    for i, duty_user in enumerate(picked):
#     partner = picked[1 - i]
#     if duty_user.has_pm:
#         try:
#             await bot.send_message(
#                 duty_user.tg_id,
#                 f"Привіт! Ти сьогодні чергуєш. Твій напарник: {mention_html(partner)}",
#             )
#         except TelegramForbiddenError:
#             await db.upsert_user(
#                 duty_user.tg_id,
#                 duty_user.username,
#                 duty_user.last_name,
#                 duty_user.first_name,
#                 role=duty_user.role,
#                 has_pm=False,
#             )
    return f"Призначення виконано адміністратором {triggered_by}."


def menu_keyboard(role: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📊 Моя статистика", callback_data="menu:stats")]]
    rows.append([InlineKeyboardButton(text="📚 Загальна статистика", callback_data="menu:stats_all")])
    if role == "student":
        rows.append([InlineKeyboardButton(text="🔄 Попросити заміну", callback_data="menu:request_swap")])
    if can_manage_day(role):
        rows.append([InlineKeyboardButton(text="🧑‍🤝‍🧑 Призначити чергових", callback_data="menu:assign")])
        rows.append([InlineKeyboardButton(text="🙅 Відмітити відсутнього", callback_data="menu:mark_absent")])
        rows.append([InlineKeyboardButton(text="🏠/💻 Перемкнути дистанційку", callback_data="menu:toggle_remote")])
    if role in {"owner", "admin"}:
        rows.append([InlineKeyboardButton(text="🔁 Заміна чергового", callback_data="menu:replace_duty")])
    if role in {"owner", "admin", "moderator"}:
        rows.append([InlineKeyboardButton(text="👥 Ролі", callback_data="menu:staff_list")])
        rows.append([InlineKeyboardButton(text="📅 Відсутні сьогодні", callback_data="menu:absent_today")])
    if role in {"owner", "admin"}:
        rows.append([InlineKeyboardButton(text="⚙️ Адмін-панель", callback_data="menu:admin_tools")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_tools_keyboard(role: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🧑‍🤝‍🧑 Призначити сьогодні", callback_data="tools:assign_today")],
        [InlineKeyboardButton(text="🔁 Заміна чергового", callback_data="menu:replace_duty")],
        [InlineKeyboardButton(text="➕ +1 чергування", callback_data="tools:plus_one")],
        [InlineKeyboardButton(text="🙅 Відсутні на дату", callback_data="tools:absent_date")],
        [InlineKeyboardButton(text="📚 Загальна статистика", callback_data="menu:stats_all")],
        [InlineKeyboardButton(text="👥 Ролі", callback_data="menu:staff_list")],
    ]
    if role in {"owner", "admin"}:
        rows.append([InlineKeyboardButton(text="ℹ️ Група/статус", callback_data="tools:group_info")])
    rows.append([InlineKeyboardButton(text="🔙 В меню", callback_data="stats_back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def duty_dates_keyboard(dates: list[str], page: int, total_dates: int, page_size: int = 10) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for d in dates:
        rows.append([InlineKeyboardButton(text=d, callback_data=f"stats_date:{d}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"stats_dates_page:{page-1}"))
    if (page + 1) * page_size < total_dates:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"stats_dates_page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 В меню", callback_data="stats_back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def absent_keyboard(students: list[UserRow], absent_ids: set[int]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, st in enumerate(students, start=1):
        order = st.sort_order if st.sort_order is not None else i
        mark = "❌ " if st.tg_id in absent_ids else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{order}. {st.last_name} {st.first_name}",
                    callback_data=f"absent_toggle:{st.tg_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="absent_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot) -> None:
    has_pm = message.chat.type == ChatType.PRIVATE
    user = await get_actor_or_deny(message, has_pm=has_pm)
    if not user:
        return
    if message.chat.type == ChatType.PRIVATE:
        await set_commands_for_user(bot, user)
    await message.answer(
        f"Вітаю, <b>{full_name(user)}</b>! Твоя роль: <b>{user.role}</b>.\nВикористай /menu для керування."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    base_cmds = [
        "/start - реєстрація в боті (в ПП ставить дозвіл на приватні сповіщення)",
        "/menu - головне меню",
        "/stats - моя статистика чергувань",
        "/help - список команд",
        "/assign - призначити чергових на сьогодні",
        "/replace_duty <Старе_прізвище> <Нове_прізвище> [YYYY-MM-DD] - ручна заміна чергового",
        "/skip_duty [причина] - запит на заміну/пропуск чергування",
    ]
    mod_cmds = ["Inline-меню: відмітити відсутніх / запит на дистанційку / запит на призначення"]
    admin_cmds = [
        "/set_group - зберегти поточну групу",
        "/set_group_id &lt;CHAT_ID&gt; - вручну змінити групу для бота",
        "/unset_group - відв'язати групу",
        "/group_info - показати поточний group_chat_id і статус дня",
        "/add_student &lt;N&gt; &lt;TG_ID&gt; &lt;Прізвище&gt; &lt;Ім'я&gt; [username] - додати/оновити студента з порядком",
        "/edit_student &lt;TG_ID&gt; [last=Прізвище] [first=Ім'я] [username=@user|-] [n=номер] - змінити студента",
        "/del_student &lt;TG_ID&gt; - видалити студента",
        "/students - показати список студентів по порядку черги",
        "/sync_user &lt;TG_ID&gt; - підтягнути актуальний username з групи",
        "/sync_students - масово оновити username для студентів",
        "/staff_list - список owner/admin/moderator",
        "/absent_on [YYYY-MM-DD] - список відсутніх на дату",
        "/add_mod &lt;TG_ID&gt; - призначити модератора",
        "/rm_mod &lt;TG_ID&gt; - зняти права модератора",
        "Inline-меню: призначити чергових, відмітити відсутніх, перемкнути дистанційку",
    ]
    owner_cmds = ["/add_admin &lt;TG_ID&gt; - призначити адміністратора", "/rm_admin &lt;TG_ID&gt; - зняти права адміністратора"]

    lines = [f"Команди для ролі <b>{user.role}</b>:", ""] + base_cmds
    if user.role == "student":
        lines += [""] + student_cmds
    if user.role in {"moderator", "admin", "owner"}:
        lines += [""] + mod_cmds
    if user.role in {"admin", "owner"}:
        lines += [""] + admin_cmds
    if user.role == "owner":
        lines += [""] + owner_cmds

    await message.answer("\n".join(lines))


@router.message(StateFilter("*"), Command("help"))
async def cmd_help_any_state(message: Message, state: FSMContext) -> None:
    await state.clear()
    await cmd_help(message)


@router.message(StateFilter("*"), Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Поточну дію скасовано.")


@router.message(Command("set_group"))
async def cmd_set_group(message: Message) -> None:
    try:
        user = await ensure_user_from_message(message)
    except PermissionError:
        await message.answer("Тебе немає у списку групи. Звернись до admin/owner.")
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /set_group")
        return
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.answer("Команду /set_group треба виконати в групі.")
        return
    await db.set_group_chat(message.chat.id)
    await message.answer(f"ID групи збережено: <code>{message.chat.id}</code>")


@router.message(Command("set_group_id"))
async def cmd_set_group_id(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /set_group_id")
        return
    if not command.args:
        await message.answer("Формат: /set_group_id &lt;CHAT_ID&gt;")
        return
    try:
        chat_id = int(command.args.strip())
    except ValueError:
        await message.answer("CHAT_ID має бути числом.")
        return
    await db.set_group_chat(chat_id)
    await message.answer(f"Новий group_chat_id збережено: <code>{chat_id}</code>")


@router.message(Command("group_info"))
async def cmd_group_info(message: Message) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /group_info")
        return
    group_chat_id, status = await db.get_settings()
    group_text = str(group_chat_id) if group_chat_id is not None else "не встановлено"
    await message.answer(
        f"Поточна група: <code>{group_text}</code>\n"
        f"Статус дня: <b>{status}</b>"
    )


@router.message(Command("unset_group"))
async def cmd_unset_group(message: Message) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /unset_group")
        return
    await db.clear_group_chat()
    await message.answer("Групу відв'язано. Щоб знову прив'язати, використай /set_group або /set_group_id.")


@router.message(Command("add_student"))
async def cmd_add_student(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /add_student")
        return

    args = (command.args or "").split()
    if len(args) < 3:
        await message.answer(
            "Формат:\n"
            "/add_student &lt;N&gt; &lt;TG_ID&gt; &lt;Прізвище&gt; &lt;Ім'я&gt; [username]\n"
            "або старий: /add_student &lt;TG_ID&gt; &lt;Прізвище&gt; &lt;Ім'я&gt; [username]"
        )
        return

    try:
        first_num = int(args[0])
    except ValueError:
        await message.answer("Перший аргумент має бути числом.")
        return

    sort_order: Optional[int] = None
    if len(args) >= 4:
        try:
            tg_id = int(args[1])
            sort_order = first_num
            last_name, first_name = args[2], args[3]
            username = args[4].lstrip("@") if len(args) >= 5 else None
        except ValueError:
            tg_id = first_num
            last_name, first_name = args[1], args[2]
            username = args[3].lstrip("@") if len(args) >= 4 else None
    else:
        tg_id = first_num
        last_name, first_name = args[1], args[2]
        username = args[3].lstrip("@") if len(args) >= 4 else None

    existing = await db.get_user(tg_id)
    role_to_set = "student"
    if existing and existing.role in {"owner", "admin", "moderator"}:
        role_to_set = existing.role
    await db.upsert_user(
        tg_id,
        username,
        last_name,
        first_name,
        role=role_to_set,
        sort_order=sort_order,
        in_queue=True,
    )
    order_text = f", №{sort_order}" if sort_order is not None else ""
    role_note = "" if role_to_set == "student" else f", роль збережено: {role_to_set}"
    await message.answer(f"Студента додано: {last_name} {first_name} ({tg_id}{order_text}{role_note})")


@router.message(Command("add_admin"))
async def cmd_add_admin(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if user.role != "owner":
        await message.answer("Лише owner може додавати admin.")
        return
    if not command.args:
        await message.answer("Формат: /add_admin &lt;TG_ID&gt;")
        return
    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("TG_ID має бути числом.")
        return
    target = await db.get_user(target_id)
    if not target:
        await message.answer("Користувача немає в БД. Спершу додайте /add_student.")
        return
    await db.set_role(target_id, "admin")
    await message.answer(f"Користувач {target_id} тепер admin.")


@router.message(Command("edit_student"))
async def cmd_edit_student(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /edit_student")
        return
    args = (command.args or "").split()
    if not args:
        await message.answer(
            "Формат: /edit_student &lt;TG_ID&gt; [last=Прізвище] [first=Ім'я] [username=@user|-] [n=номер]"
        )
        return
    try:
        tg_id = int(args[0])
    except ValueError:
        await message.answer("TG_ID має бути числом.")
        return

    updates: dict[str, str] = {}
    for token in args[1:]:
        if "=" in token:
            k, v = token.split("=", 1)
            updates[k.strip().lower()] = v.strip()

    last_name = updates.get("last")
    first_name = updates.get("first")
    username_raw = updates.get("username")
    sort_raw = updates.get("n")
    sort_order: Optional[int] = None
    if sort_raw is not None:
        try:
            sort_order = int(sort_raw)
        except ValueError:
            await message.answer("n має бути числом.")
            return

    username: Optional[str]
    if username_raw is None:
        username = None
    elif username_raw == "-":
        username = ""
    else:
        username = username_raw.lstrip("@")

    ok = await db.update_student(
        tg_id=tg_id,
        last_name=last_name,
        first_name=first_name,
        username=username,
        sort_order=sort_order,
    )
    if not ok:
        await message.answer("Студента з таким TG_ID не знайдено.")
        return
    await message.answer("Дані студента оновлено.")


@router.message(Command("del_student"))
async def cmd_del_student(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /del_student")
        return
    if not command.args:
        await message.answer("Формат: /del_student &lt;TG_ID&gt;")
        return
    try:
        tg_id = int(command.args.strip())
    except ValueError:
        await message.answer("TG_ID має бути числом.")
        return

    target = await db.get_user(tg_id)
    if not target or not target.in_queue:
        await message.answer("Користувача немає у списку черги.")
        return

    deleted = await db.delete_user(tg_id)
    if deleted:
        await message.answer(f"Студента {tg_id} видалено.")
    else:
        await message.answer("Нічого не видалено.")


@router.message(Command("add_mod"))
async def cmd_add_mod(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Лише admin/owner може додавати moderator.")
        return
    if not command.args:
        await message.answer("Формат: /add_mod &lt;TG_ID&gt;")
        return
    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("TG_ID має бути числом.")
        return
    target = await db.get_user(target_id)
    if not target:
        await message.answer("Користувача немає в БД. Спершу додайте /add_student.")
        return
    await db.set_role(target_id, "moderator")
    await message.answer(f"Користувач {target_id} тепер moderator.")


@router.message(Command("rm_admin"))
async def cmd_rm_admin(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if user.role != "owner":
        await message.answer("Лише owner може виконувати /rm_admin")
        return
    if not command.args:
        await message.answer("Формат: /rm_admin &lt;TG_ID&gt;")
        return
    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("TG_ID має бути числом.")
        return
    target = await db.get_user(target_id)
    if not target or target.role != "admin":
        await message.answer("Користувач не є admin.")
        return
    await db.set_role(target_id, "student")
    await message.answer(f"Права admin знято з {target_id}.")


@router.message(Command("rm_mod"))
async def cmd_rm_mod(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if user.role not in {"owner", "admin"}:
        await message.answer("Лише owner/admin може виконувати /rm_mod")
        return
    if not command.args:
        await message.answer("Формат: /rm_mod &lt;TG_ID&gt;")
        return
    try:
        target_id = int(command.args.strip())
    except ValueError:
        await message.answer("TG_ID має бути числом.")
        return
    target = await db.get_user(target_id)
    if not target or target.role != "moderator":
        await message.answer("Користувач не є moderator.")
        return
    await db.set_role(target_id, "student")
    await message.answer(f"Права moderator знято з {target_id}.")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    duty_count, total_log = await db.get_stats(user.tg_id)
    await message.answer(
        f"📊 Статистика для <b>{full_name(user)}</b>:\n"
        f"- Всього призначень (users.duty_count): <b>{duty_count}</b>\n"
        f"- Записів у журналі (duty_log): <b>{total_log}</b>"
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, bot: Bot) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if message.chat.type == ChatType.PRIVATE:
        await set_commands_for_user(bot, user)
    await message.answer("Головне меню:", reply_markup=menu_keyboard(user.role))


@router.message(Command("stats_all"))
async def cmd_stats_all(message: Message) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 По прізвищах", callback_data="stats_all:users")],
        [InlineKeyboardButton(text="📅 По датах", callback_data="stats_all:dates:0")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="stats_back_menu")],
    ])
    await message.answer("Оберіть тип загальної статистики:", reply_markup=kb)


@router.message(Command("students"))
async def cmd_students(message: Message, bot: Bot) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    students = await db.get_students()
    if not students:
        await message.answer("Список студентів порожній.")
        return
    for st in students:
        await refresh_user_username_from_group(bot, st.tg_id)
    students = await db.get_students()
    lines = ["Список студентів (порядок черги):"]
    for st in students:
        lines.append(f"{st.sort_order}. {st.last_name} {st.first_name} (ID: {st.tg_id})")
    await message.answer("\n".join(lines))


@router.message(Command("sync_user"))
async def cmd_sync_user(message: Message, command: CommandObject, bot: Bot) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /sync_user")
        return
    if not command.args:
        await message.answer("Формат: /sync_user &lt;TG_ID&gt;")
        return
    try:
        tg_id = int(command.args.strip())
    except ValueError:
        await message.answer("TG_ID має бути числом.")
        return
    updated = await refresh_user_username_from_group(bot, tg_id)
    await message.answer("username оновлено." if updated else "Оновлень немає або користувача не знайдено в групі.")


@router.message(Command("sync_students"))
async def cmd_sync_students(message: Message, bot: Bot) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_admin_or_owner(user.role):
        await message.answer("Немає прав для /sync_students")
        return
    students = await db.get_students()
    changed = 0
    for st in students:
        if await refresh_user_username_from_group(bot, st.tg_id):
            changed += 1
    await message.answer(f"Синхронізація завершена. Оновлено username: {changed}.")


@router.message(Command("staff_list"))
async def cmd_staff_list(message: Message) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if user.role not in {"owner", "admin", "moderator"}:
        await message.answer("Немає прав для /staff_list")
        return

    owners = await db.get_users_by_role("owner")
    admins = await db.get_users_by_role("admin")
    moderators = await db.get_users_by_role("moderator")

    def fmt(rows: list[UserRow]) -> list[str]:
        if not rows:
            return ["- немає"]
        return [f"- {full_name(r)} (ID: {r.tg_id})" for r in rows]

    lines = ["👥 Список керівних ролей:", "", "Owner:"] + fmt(owners)
    lines += ["", "Admins:"] + fmt(admins)
    lines += ["", "Moderators:"] + fmt(moderators)
    await message.answer("\n".join(lines))


@router.callback_query(F.data == "menu:stats")
async def cb_menu_stats(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("Тебе немає у списку групи. Звернись до admin/owner.")
        await callback.answer()
        return
    duty_count, total_log = await db.get_stats(user.tg_id)
    await callback.message.edit_text(
        f"📊 Статистика для <b>{full_name(user)}</b>:\n"
        f"- Всього призначень (users.duty_count): <b>{duty_count}</b>\n"
        f"- Записів у журналі (duty_log): <b>{total_log}</b>",
        reply_markup=menu_keyboard(user.role),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:stats_all")
async def cb_menu_stats_all(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Немає доступу", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 По прізвищах", callback_data="stats_all:users")],
        [InlineKeyboardButton(text="📅 По датах", callback_data="stats_all:dates:0")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="stats_back_menu")],
    ])
    await callback.message.edit_text("Оберіть тип загальної статистики:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "stats_all:users")
async def cb_stats_all_users(callback: CallbackQuery) -> None:
    rows = await db.get_all_duty_counts()
    if not rows:
        text = "Статистика порожня."
    else:
        lines = ["📊 Чергування по прізвищах:"]
        for i, (last_name, first_name, cnt) in enumerate(rows, start=1):
            lines.append(f"{i}. {last_name} {first_name} — {cnt}")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 По датах", callback_data="stats_all:dates:0")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="stats_back_menu")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("stats_all:dates:"))
@router.callback_query(F.data.startswith("stats_dates_page:"))
async def cb_stats_dates_pages(callback: CallbackQuery) -> None:
    if callback.data.startswith("stats_all:dates:"):
        page = int(callback.data.split(":")[-1])
    else:
        page = int(callback.data.split(":")[-1])
    dates, total = await db.get_activity_dates_page(page, page_size=10)
    if not dates:
        text = "Журнал чергувань порожній."
    else:
        text = "📅 Оберіть дату для перегляду чергових:"
    await callback.message.edit_text(text, reply_markup=duty_dates_keyboard(dates, page, total, page_size=10))
    await callback.answer()


@router.callback_query(F.data.startswith("stats_date:"))
async def cb_stats_date_detail(callback: CallbackQuery) -> None:
    day = callback.data.split(":", 1)[1]
    duty = await db.get_duty_by_date(day)
    absent_rows = await db.get_absent_rows(day)
    lines = [f"📌 {day}"]
    if duty:
        u1 = await db.get_user(duty[0])
        u2 = await db.get_user(duty[1])
        p1 = full_name(u1) if u1 else str(duty[0])
        p2 = full_name(u2) if u2 else str(duty[1])
        lines.append(f"Чергові: {p1} та {p2}")
    else:
        lines.append("Чергові: немає запису")
    if absent_rows:
        lines.append("Відсутні:")
        for uid, _marked_by in absent_rows:
            u = await db.get_user(uid)
            lines.append(f"- {full_name(u) if u else uid}")
    else:
        lines.append("Відсутні: немає")
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 До дат", callback_data="stats_all:dates:0")],
        [InlineKeyboardButton(text="🔙 В меню", callback_data="stats_back_menu")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "stats_back_menu")
async def cb_stats_back_menu(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Немає доступу", show_alert=True)
        return
    await callback.message.edit_text("Головне меню:", reply_markup=menu_keyboard(user.role))
    await callback.answer()


@router.callback_query(F.data.startswith("ownerchg:"))
async def cb_owner_change_decision(callback: CallbackQuery, bot: Bot) -> None:
    owner = await db.get_user(callback.from_user.id)
    if not owner or owner.role != "owner":
        await callback.answer("Лише owner", show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer(TIME_WINDOW_TEXT, show_alert=True)
        return
    _, action, req_id_str = callback.data.split(":")
    req = await db.get_request(int(req_id_str))
    if not req or req["status"] != "pending" or req["request_type"] != "change_duty_owner_approval":
        await callback.answer("Запит неактуальний", show_alert=True)
        return
    parts = (req["payload"] or "").split("|", 1)
    target_day = parts[0] if parts and parts[0] else _today_str()
    if action == "reject":
        await db.close_request(req["id"], "rejected")
        await callback.message.edit_text("Запит на зміну чергових відхилено.")
        await callback.answer("Відхилено")
        return
    await db.clear_duty_for_date(target_day)
    result = await assign_duty_and_notify(bot, req["initiator_id"], target_day=target_day)
    await db.close_request(req["id"], "approved")
    await callback.message.edit_text(f"Зміну дозволено. {result}")
    await callback.answer("Дозволено")


@router.callback_query(F.data == "menu:assign")
async def cb_menu_assign(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    user_row = await db.get_user(callback.from_user.id)
    if not user_row or not can_manage_day(user_row.role):
        await callback.answer("Немає прав", show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer(TIME_WINDOW_TEXT, show_alert=True)
        return
    duty_day = _today_str()
    if not is_assignment_time_allowed_for_today(duty_day):
        await callback.answer("Призначати чергових на поточний день можна лише з 09:00 до 14:00.", show_alert=True)
        return
    existing_duty = await db.get_duty_by_date(duty_day)
    if existing_duty and user_row.role in {"admin", "moderator"}:
        await state.set_state(ChangeDutyReasonForm.waiting_reason)
        await state.update_data(change_day=duty_day)
        await callback.message.edit_text(
            "Чергові на цей день вже призначені. Напишіть причину зміни:",
            reply_markup=None,
        )
        await callback.answer()
        return
    if existing_duty and user_row.role == "owner":
        await db.clear_duty_for_date(duty_day)

    if user_row.role in {"owner", "admin"}:
        result = await assign_duty_and_notify(bot, callback.from_user.id, target_day=duty_day)
        await callback.message.edit_text(result, reply_markup=menu_keyboard(user_row.role))
    else:
        req_id = await db.create_request("assign_duty", callback.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"req:approve:{req_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"req:reject:{req_id}"),
        ]])
        await notify_admins(bot, f"Запит від модератора {callback.from_user.id}: призначити чергових.", kb)
        await callback.message.edit_text("Запит надіслано адміністраторам.", reply_markup=menu_keyboard(user_row.role))
    await callback.answer()


@router.callback_query(F.data == "menu:toggle_remote")
async def cb_menu_toggle_remote(callback: CallbackQuery, bot: Bot) -> None:
    user_row = await db.get_user(callback.from_user.id)
    if not user_row or not can_manage_day(user_row.role):
        await callback.answer("Немає прав", show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer(TIME_WINDOW_TEXT, show_alert=True)
        return

    _, status = await db.get_settings()
    new_status = "remote" if status != "remote" else "normal"

    if user_row.role in {"owner", "admin"}:
        await db.set_status(new_status)
        await callback.message.edit_text(
            f"Статус дня змінено на: <b>{new_status}</b>",
            reply_markup=menu_keyboard(user_row.role),
        )
    else:
        req_id = await db.create_request("toggle_remote", callback.from_user.id, payload=new_status)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"req:approve:{req_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"req:reject:{req_id}"),
        ]])
        await notify_admins(bot, f"Запит від модератора {callback.from_user.id}: статус дня -> {new_status}", kb)
        await callback.message.edit_text("Запит надіслано адміністраторам.", reply_markup=menu_keyboard(user_row.role))
    await callback.answer()


@router.callback_query(F.data == "menu:mark_absent")
async def cb_menu_mark_absent(callback: CallbackQuery) -> None:
    user_row = await db.get_user(callback.from_user.id)
    if not user_row or not can_manage_day(user_row.role):
        await callback.answer("Немає прав", show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer(TIME_WINDOW_TEXT, show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer("Відмічати відсутніх на поточний день можна лише з 09:00 до 14:00.", show_alert=True)
        return

    students = await db.get_students()
    if not students:
        await callback.message.answer("Список студентів порожній.")
    else:
        today = date.today().isoformat()
        absent_ids = await db.get_absent_ids(today)
        await callback.message.answer(
            "Оберіть відсутніх на сьогодні (натискання перемикає статус):",
            reply_markup=absent_keyboard(students, absent_ids),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("absent_toggle:"))
async def cb_absent_toggle(callback: CallbackQuery, bot: Bot) -> None:
    user_row = await db.get_user(callback.from_user.id)
    if not user_row or not can_manage_day(user_row.role):
        await callback.answer("Немає прав", show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer(TIME_WINDOW_TEXT, show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer("Відмічати відсутніх на поточний день можна лише з 09:00 до 14:00.", show_alert=True)
        return

    target_id = int(callback.data.split(":", 1)[1])
    today = date.today().isoformat()
    absent_ids = await db.get_absent_ids(today)

    if user_row.role in {"owner", "admin"}:
        if target_id in absent_ids:
            await db.remove_absence(target_id, today)
        else:
            await db.add_absence(target_id, today, callback.from_user.id)
        students = await db.get_students()
        new_absent = await db.get_absent_ids(today)
        await callback.message.edit_reply_markup(reply_markup=absent_keyboard(students, new_absent))
        await callback.answer("Оновлено")
    else:
        req_id = await db.create_request("mark_absent", callback.from_user.id, target_user_id=target_id, payload=today)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"req:approve:{req_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"req:reject:{req_id}"),
        ]])
        await notify_admins(bot, f"Запит від модератора {callback.from_user.id}: перемкнути відсутність {target_id} ({today})", kb)
        await callback.answer("Запит надіслано admin")


@router.callback_query(F.data == "absent_done")
async def cb_absent_done(callback: CallbackQuery) -> None:
    user_row = await db.get_user(callback.from_user.id)
    if not user_row or not can_manage_day(user_row.role):
        await callback.answer("Немає прав", show_alert=True)
        return
    await callback.message.edit_text("Головне меню:", reply_markup=menu_keyboard(user_row.role))
    await callback.answer("Готово")


@router.callback_query(F.data == "menu:request_swap")
async def cb_menu_request_swap(callback: CallbackQuery, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Тебе немає у списку групи. Звернись до admin/owner.", show_alert=True)
        return

    today = _today_str()
    current_duty = await db.get_duty_by_date(today)
    if not current_duty or user.tg_id not in current_duty:
        await callback.answer("❌ Ти сьогодні не чергуєш, тому не можеш попросити заміну!", show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer(TIME_WINDOW_TEXT, show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer("Запит на заміну на поточний день можна подати лише з 09:00 до 14:00.", show_alert=True)
        return

    await state.set_state(SkipDutyForm.waiting_reason)
    await callback.message.edit_text("Напиши причину для запиту на заміну:", reply_markup=None)
    await callback.answer()


@router.callback_query(F.data == "menu:staff_list")
async def cb_menu_staff_list(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or user.role not in {"owner", "admin", "moderator"}:
        await callback.answer("Немає прав", show_alert=True)
        return
    owners = await db.get_users_by_role("owner")
    admins = await db.get_users_by_role("admin")
    moderators = await db.get_users_by_role("moderator")

    def fmt(rows: list[UserRow]) -> list[str]:
        if not rows:
            return ["- немає"]
        return [f"- {full_name(r)} (ID: {r.tg_id})" for r in rows]

    lines = ["👥 Список керівних ролей:", "", "Owner:"] + fmt(owners)
    lines += ["", "Admins:"] + fmt(admins)
    lines += ["", "Moderators:"] + fmt(moderators)
    await callback.message.edit_text("\n".join(lines), reply_markup=menu_keyboard(user.role))
    await callback.answer()


@router.callback_query(F.data == "menu:absent_today")
async def cb_menu_absent_today(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or user.role not in {"owner", "admin", "moderator"}:
        await callback.answer("Немає прав", show_alert=True)
        return
    target_day = _today_str()
    rows = await db.get_absent_rows(target_day)
    if not rows:
        text = f"На {target_day} відсутніх не зафіксовано."
    else:
        lines = [f"🙅 Відсутні на {target_day}:"]
        for user_id, marked_by in rows:
            u = await db.get_user(user_id)
            m = await db.get_user(marked_by)
            lines.append(f"- {full_name(u) if u else user_id} (відмітив: {full_name(m) if m else marked_by})")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=menu_keyboard(user.role))
    await callback.answer()


@router.callback_query(F.data == "menu:admin_tools")
async def cb_menu_admin_tools(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or user.role not in {"owner", "admin"}:
        await callback.answer("Немає прав", show_alert=True)
        return
    await callback.message.edit_text("⚙️ Адмін-панель:", reply_markup=admin_tools_keyboard(user.role))
    await callback.answer()


@router.callback_query(F.data == "tools:assign_today")
async def cb_tools_assign_today(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or user.role not in {"owner", "admin"}:
        await callback.answer("Немає прав", show_alert=True)
        return
    if not is_today_window_open():
        await callback.answer(TIME_WINDOW_TEXT, show_alert=True)
        return
    day = _today_str()
    if not is_assignment_time_allowed_for_today(day):
        await callback.answer("Призначати чергових на поточний день можна лише з 09:00 до 14:00.", show_alert=True)
        return
    existing = await db.get_duty_by_date(day)
    if existing and user.role == "owner":
        await db.clear_duty_for_date(day)
    elif existing:
        await callback.answer("Вже призначено. Для зміни потрібне погодження owner.", show_alert=True)
        return
    result = await assign_duty_and_notify(bot, user.tg_id, target_day=day)
    await callback.message.edit_text(result, reply_markup=admin_tools_keyboard(user.role))
    await callback.answer()


@router.callback_query(F.data == "tools:absent_date")
async def cb_tools_absent_date(callback: CallbackQuery, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or user.role not in {"owner", "admin", "moderator"}:
        await callback.answer("Немає прав", show_alert=True)
        return
    await state.set_state(AdminInputForm.waiting_absent_date)
    await callback.message.edit_text("Введи дату для перегляду відсутніх: YYYY-MM-DD", reply_markup=None)
    await callback.answer()


@router.callback_query(F.data == "tools:plus_one")
async def cb_tools_plus_one(callback: CallbackQuery, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or user.role not in {"owner", "admin"}:
        await callback.answer("Немає прав", show_alert=True)
        return
    await state.set_state(AdminInputForm.waiting_plus_one)
    await callback.message.edit_text("Введи TG_ID або прізвище, кому додати +1 чергування:", reply_markup=None)
    await callback.answer()


@router.callback_query(F.data == "tools:group_info")
async def cb_tools_group_info(callback: CallbackQuery) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or user.role not in {"owner", "admin"}:
        await callback.answer("Немає прав", show_alert=True)
        return
    group_chat_id, status = await db.get_settings()
    group_text = str(group_chat_id) if group_chat_id is not None else "не встановлено"
    await callback.message.edit_text(
        f"Поточна група: <code>{group_text}</code>\nСтатус дня: <b>{status}</b>",
        reply_markup=admin_tools_keyboard(user.role),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:replace_duty")
async def cb_menu_replace_duty(callback: CallbackQuery, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or user.role not in {"owner", "admin"}:
        await callback.answer("Немає прав", show_alert=True)
        return
    await state.set_state(ReplaceDutyForm.waiting_data)
    await callback.message.edit_text(
        "⏰ Дія доступна лише з 09:00 до 14:00.\n"
        "Введи заміну у форматі:\n"
        "&lt;Старе_прізвище&gt; &lt;Нове_прізвище&gt; [YYYY-MM-DD]\n"
        "Приклад: Калініченко Капустян 2026-05-19",
        reply_markup=None,
    )
    await callback.answer()


@router.message(Command("assign"))
async def cmd_assign(message: Message, command: CommandObject, bot: Bot, state: FSMContext) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not can_manage_day(user.role):
        await message.answer("Немає прав для /assign")
        return
    if not is_today_window_open():
        await message.answer(TIME_WINDOW_TEXT)
        return
    target_day = _today_str()
    if (command.args or "").strip():
        await message.answer("Призначення доступне тільки на сьогодні. Використай /assign без дати.")
        return
    if not is_assignment_time_allowed_for_today(target_day):
        await message.answer(TIME_WINDOW_TEXT)
        return
    existing = await db.get_duty_by_date(target_day)
    if existing and user.role in {"admin", "moderator"}:
        await state.set_state(ChangeDutyReasonForm.waiting_reason)
        await state.update_data(change_day=target_day)
        await message.answer("Чергові на цей день вже призначені. Напишіть причину зміни:")
        return
    if existing and user.role == "owner":
        await db.clear_duty_for_date(target_day)
    result = await assign_duty_and_notify(bot, user.tg_id, target_day=target_day)
    await message.answer(result)


@router.message(Command("replace_duty"))
async def cmd_replace_duty(message: Message, command: CommandObject, bot: Bot) -> None:
    await process_replace_duty(message, bot, (command.args or "").strip())


async def process_replace_duty(message: Message, bot: Bot, raw_args: str) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if user.role not in {"owner", "admin"}:
        await message.answer("Немає прав для /replace_duty")
        return
    if not is_today_window_open():
        await message.answer(TIME_WINDOW_TEXT)
        return
    args = raw_args.split()
    if len(args) < 2:
        await message.answer("Формат: /replace_duty &lt;Старе_прізвище&gt; &lt;Нове_прізвище&gt; [YYYY-MM-DD]")
        return
    old_last = args[0].strip()
    new_last = args[1].strip()
    target_day = args[2] if len(args) >= 3 else _today_str()
    try:
        datetime.strptime(target_day, "%Y-%m-%d")
    except ValueError:
        await message.answer("Формат дати: YYYY-MM-DD")
        return
    duty = await db.get_duty_by_date(target_day)
    if not duty:
        await message.answer("На цю дату немає призначених чергових.")
        return
    duty_users = [await db.get_user(duty[0]), await db.get_user(duty[1])]
    old_candidates = [u for u in duty_users if u and u.last_name.lower() == old_last.lower()]
    if len(old_candidates) != 1:
        await message.answer("Старе прізвище не знайдено серед поточних чергових або воно неоднозначне.")
        return
    old_user = old_candidates[0]

    new_candidates = await db.find_queue_users_by_last_name(new_last)
    if len(new_candidates) != 1:
        await message.answer("Нове прізвище не знайдено в черзі або воно неоднозначне. Уточни через /students.")
        return
    new_user = new_candidates[0]
    old_id = old_user.tg_id
    new_id = new_user.tg_id
    absent = await db.get_absent_ids(target_day)
    if new_id in absent:
        await message.answer("Цей користувач позначений як відсутній на цю дату.")
        return
    ok = await db.replace_duty_member(target_day, old_id, new_id)
    if not ok:
        await message.answer("Не вдалося виконати заміну. Перевір IDs.")
        return
    group_chat_id, _ = await db.get_settings()
    old_name = full_name(old_user) if old_user else str(old_id)
    new_name = full_name(new_user)
    if group_chat_id:
        try:
            await bot.send_message(group_chat_id, f"🔁 По чергуванню: {mention_html(old_user)} змінено на {mention_html(new_user)}.")
        except Exception:
            pass
    await message.answer(f"✅ Заміна виконана: {old_name} -> {new_name} ({target_day}).")


@router.message(ReplaceDutyForm.waiting_data)
async def replace_duty_input(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await process_replace_duty(message, bot, (message.text or "").strip())


@router.message(AdminInputForm.waiting_absent_date)
async def admin_absent_date_input(message: Message, state: FSMContext) -> None:
    day = (message.text or "").strip()
    await state.clear()
    user = await get_actor_or_deny(message)
    if not user:
        return
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        await message.answer("Невірний формат дати. Використай YYYY-MM-DD.")
        return
    rows = await db.get_absent_rows(day)
    if not rows:
        await message.answer(f"На {day} відсутніх не зафіксовано.")
        return
    lines = [f"🙅 Відсутні на {day}:"]
    for user_id, marked_by in rows:
        u = await db.get_user(user_id)
        m = await db.get_user(marked_by)
        lines.append(f"- {full_name(u) if u else user_id} (відмітив: {full_name(m) if m else marked_by})")
    await message.answer("\n".join(lines))


@router.message(AdminInputForm.waiting_plus_one)
async def admin_plus_one_input(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    await state.clear()
    user = await get_actor_or_deny(message)
    if not user:
        return
    if user.role not in {"owner", "admin"}:
        await message.answer("Немає прав.")
        return
    target: Optional[UserRow] = None
    if query.isdigit():
        target = await db.get_user(int(query))
    else:
        matches = await db.find_queue_users_by_last_name(query)
        if len(matches) != 1:
            await message.answer("Не знайдено або неоднозначне прізвище. Вкажи TG_ID.")
            return
        target = matches[0]
    if not target:
        await message.answer("Користувача не знайдено.")
        return
    ok = await db.increment_duty_count(target.tg_id, 1)
    if not ok:
        await message.answer("Не вдалося оновити статистику.")
        return
    await message.answer(f"✅ Додано +1 чергування для {full_name(target)}.")


@router.message(Command("absent_on"))
async def cmd_absent_on(message: Message, command: CommandObject) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if user.role not in {"owner", "admin", "moderator"}:
        await message.answer("Немає прав для /absent_on")
        return
    target_day = (command.args or "").strip() or _today_str()
    try:
        datetime.strptime(target_day, "%Y-%m-%d")
    except ValueError:
        await message.answer("Формат: /absent_on [YYYY-MM-DD]")
        return
    rows = await db.get_absent_rows(target_day)
    if not rows:
        await message.answer(f"На {target_day} відсутніх не зафіксовано.")
        return
    lines = [f"🙅 Відсутні на {target_day}:"]
    for user_id, marked_by in rows:
        u = await db.get_user(user_id)
        m = await db.get_user(marked_by)
        lines.append(f"- {full_name(u) if u else user_id} (відмітив: {full_name(m) if m else marked_by})")
    await message.answer("\n".join(lines))


@router.message(Command("skip_duty"))
async def cmd_skip_duty(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_today_window_open():
        await message.answer(TIME_WINDOW_TEXT)
        return

    today = _today_str()
    current_duty = await db.get_duty_by_date(today)
    if not current_duty or user.tg_id not in current_duty:
        await message.answer("❌ Ти не можеш змінити чергування, оскільки тебе немає серед чергових на сьогодні!")
        return

    reason = (command.args or "").strip()
    if not reason:
        await state.set_state(SkipDutyForm.waiting_reason)
        await message.answer("Вкажи причину одним повідомленням.")
        return

    if user.role == "owner":
        await db.add_absence(user.tg_id, today, user.tg_id)  
        await db.clear_duty_for_date(today)                
        result = await assign_duty_and_notify(bot, user.tg_id, target_day=today) 
        await message.answer(f"✅ Чергування успішно пропущено овнером.\nПричина: {reason}\n\n{result}")
        return

    req_id = await db.create_request("skip_duty", user.tg_id, payload=reason)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"req:approve:{req_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"req:reject:{req_id}"),
    ]])
    
    text = f"⚠️ Запит /skip_duty від {full_name(user)} (Роль: {user.role})\nПричина: {reason}"
    await send_routed_request(bot, req_id, text, kb, user.role)
    await message.answer("Запит на заміну надіслано на розгляд керівництву.")


@router.message(ChangeDutyReasonForm.waiting_reason)
async def change_duty_reason_input(message: Message, state: FSMContext, bot: Bot) -> None:
    actor = await get_actor_or_deny(message)
    if not actor:
        return
    if not is_today_window_open():
        await state.clear()
        await message.answer(TIME_WINDOW_TEXT)
        return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Причина не може бути порожньою.")
        return
    data = await state.get_data()
    target_day = data.get("change_day", _today_str())
    existing = await db.get_duty_by_date(target_day)
    if not existing:
        await state.clear()
        await message.answer("На цю дату ще немає призначення. Можеш призначати без погодження owner.")
        return
    old_u1 = await db.get_user(existing[0])
    old_u2 = await db.get_user(existing[1])
    old_text = f"{full_name(old_u1)} та {full_name(old_u2)}" if old_u1 and old_u2 else f"{existing[0]} та {existing[1]}"
    req_id = await db.create_request(
        "change_duty_owner_approval",
        initiator_id=actor.tg_id,
        payload=f"{target_day}|{reason}",
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Дозволити", callback_data=f"ownerchg:approve:{req_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"ownerchg:reject:{req_id}"),
        ]]
    )
    actor_name = f"@{actor.username}" if actor.username else full_name(actor)
    await bot.send_message(
        OWNER_ID,
        f"⚠️ Адмін {actor_name} хоче змінити чергових на {target_day}.\n"
        f"Причина: {reason}\n"
        f"Поточні чергові: {old_text}\n"
        f"Дозволити зміну?",
        reply_markup=kb,
    )
    await state.clear()
    await message.answer("Запит на зміну надіслано власнику.")


@router.message(SkipDutyForm.waiting_reason)
async def skip_reason_input(message: Message, state: FSMContext, bot: Bot) -> None:
    user = await get_actor_or_deny(message)
    if not user:
        return
    if not is_today_window_open():
        await state.clear()
        await message.answer(TIME_WINDOW_TEXT)
        return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Причина не може бути порожньою. Напиши текстом.")
        return
        
    today = _today_str()
    if user.role == "owner":
        await state.clear()
        await db.add_absence(user.tg_id, today, user.tg_id)
        await db.clear_duty_for_date(today)
        result = await assign_duty_and_notify(bot, user.tg_id, target_day=today)
        await message.answer(f"✅ Чергування успішно пропущено овнером.\nПричина: {reason}\n\n{result}")
        return

    req_id = await db.create_request("skip_duty", user.tg_id, payload=reason)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"req:approve:{req_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"req:reject:{req_id}"),
    ]])
    
    text = f"⚠️ Запит /skip_duty від {full_name(user)} (Роль: {user.role})\nПричина: {reason}"
    await send_routed_request(bot, req_id, text, kb, user.role)
    await state.clear()
    await message.answer("Запит на заміну надіслано на розгляд керівництву.")


@router.callback_query(F.data.startswith("req:"))
async def cb_request_decision(callback: CallbackQuery, bot: Bot) -> None:
    user = await db.get_user(callback.from_user.id)
    if not user or not is_admin_or_owner(user.role):
        await callback.answer("Лише admin/owner", show_alert=True)
        return

    _, action, req_id_str = callback.data.split(":")
    req_id = int(req_id_str)
    req = await db.get_request(req_id)
    
    # Перевірка: чи не закрив цей запит хтось інший секунду тому
    if not req or req["status"] != "pending":
        await callback.answer("Цей запит уже оброблено іншим адміністратором!", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    status_text = "СХВАЛЕНО ✅" if action == "approve" else "ВІДХИЛЕНО ❌"
    actor_name = full_name(user)
    result_msg = ""

    if action == "reject":
        await db.close_request(req["id"], "rejected")
        result_msg = "Запит було відхилено."
        if req["request_type"] == "skip_duty":
            try:
                await bot.send_message(req["initiator_id"], "❌ Твій запит на заміну/пропуск чергування відхилено.")
            except TelegramForbiddenError:
                pass
    else:
        if req["request_type"] in {"assign_duty", "toggle_remote", "mark_absent", "skip_duty"} and not is_today_window_open():
            await callback.answer(TIME_WINDOW_TEXT, show_alert=True)
            return
        # approve логіка
        if req["request_type"] == "assign_duty":
            result_msg = await assign_duty_and_notify(bot, callback.from_user.id)
        elif req["request_type"] == "toggle_remote":
            await db.set_status(req["payload"] or "normal")
            result_msg = f"Статус дня змінено на: {req['payload']}"
        elif req["request_type"] == "mark_absent":
            day = req["payload"] or date.today().isoformat()
            if req["target_user_id"] is None:
                result_msg = "Помилка: target_user_id відсутній."
            else:
                await db.add_absence(req["target_user_id"], day, callback.from_user.id)
                result_msg = f"Користувача {req['target_user_id']} позначено як відсутнього на {day}."
        elif req["request_type"] == "skip_duty":
            today = date.today().isoformat()
            before = await db.get_duty_by_date(today)
            await db.add_absence(req["initiator_id"], today, callback.from_user.id)
            await db.clear_duty_for_date(today)
            assign_res = await assign_duty_and_notify(bot, callback.from_user.id, target_day=today)
            after = await db.get_duty_by_date(today)
            result_msg = f"Студента пропущено.\n🔄 Результат перевибору чергових: {assign_res}"

            try:
                await bot.send_message(req["initiator_id"], "✅ Твій запит на заміну/пропуск чергування схвалено.")
            except TelegramForbiddenError:
                pass

            group_chat_id, _ = await db.get_settings()
            if group_chat_id and before and after:
                before_set = set(before)
                after_set = set(after)
                removed = list(before_set - after_set)
                added = list(after_set - before_set)
                if removed and added:
                    old_u = await db.get_user(removed[0])
                    new_u = await db.get_user(added[0])
                    old_name = full_name(old_u) if old_u else str(removed[0])
                    new_name = full_name(new_u) if new_u else str(added[0])
                    try:
                        if old_u and new_u:
                            await bot.send_message(group_chat_id, f"🔁 По чергуванню: {mention_html(old_u)} змінено на {mention_html(new_u)}.")
                        else:
                            await bot.send_message(group_chat_id, f"🔁 По чергуванню: {old_name} змінено на {new_name}.")
                    except Exception:
                        pass

        await db.close_request(req["id"], "approved")

    final_text = (
        f"🏁 **Запит #{req_id} оброблено!**\n"
        f"Дія: {status_text}\n"
        f"Хто обробив: {actor_name}\n"
        f"Деталі: {result_msg}"
    )

    copied_messages = sent_requests_tracking.get(req_id, [])
    for chat_id, msg_id in copied_messages:
        try:
            await bot.edit_message_text(
                text=final_text,
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=None
            )
        except Exception as e:
            logging.warning(f"Не вдалося оновити копію повідомлення в chat_id {chat_id}: {e}")

    if req_id in sent_requests_tracking:
        del sent_requests_tracking[req_id]

    await callback.answer("Запит успішно оброблено")


async def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN env var before running the bot")

    await db.init()

    owner_row = await db.get_user(OWNER_ID)
    if not owner_row:
        await db.upsert_user(OWNER_ID, None, "Owner", "Owner", role="owner", has_pm=False)
    else:
        await db.set_role(OWNER_ID, "owner")

    dp = Dispatcher()
    dp.include_router(router)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запуск або перезапуск бота"),
            BotCommand(command="help", description="Список команд"),
            BotCommand(command="menu", description="Головне меню"),
            BotCommand(command="stats", description="Моя статистика"),
            BotCommand(command="stats_all", description="Загальна статистика"),
            BotCommand(command="students", description="Список черги"),
            BotCommand(command="staff_list", description="Список owner/admin/moderator"),
            BotCommand(command="skip_duty", description="Запит на пропуск чергування"),
            BotCommand(command="assign", description="Призначити чергових на сьогодні"),
            BotCommand(command="cancel", description="Скасувати поточну дію"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="set_group", description="Зберегти поточну групу"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )
    reminder_task = asyncio.create_task(owner_daily_reminder_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
