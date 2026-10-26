"""User-facing texts: /help and the command menu (Russian)."""

from __future__ import annotations

from aiogram.types import BotCommand

# (command, description, admin_only)
GROUP_COMMANDS: list[tuple[str, str, bool]] = [
    ("help", "Справка", False),
    ("me", "Мой id и GitHub", False),
    ("hacks", "Активные хакатоны", False),
    ("hack", "<хак> — карточка хакатона", False),
    ("left", "<хак> — сколько осталось", False),
    ("tasks", "[<хак>] [@user] — задачи", False),
    ("issues", "<хак> [@user] [label:метка] — открытые issues", False),
    ("task", "<хак> [@user] <номер> — назначить issue", False),
    ("untask", "<хак> [@user] <номер> — снять задачу", False),
    ("issue_new", "<хак> [@кто] <название> — создать issue", False),
    ("links", "<хак> — сайт, чат организаторов, ТЗ", False),
    ("history", "[<хак>] — архив", False),
    ("stats", "Статистика участников", False),
    ("avail", "[@user] — когда можно пинговать", False),
    ("avail_set", "<дни> <да|нет|09:00-21:00> — изменить расписание", False),
    ("avail_reset", "Сбросить расписание", False),
    ("link_github", "<логин> — привязать GitHub", False),
    ("unlink_github", "Отвязать GitHub", False),
    ("tz", "Часовой пояс и время", False),
    ("chatid", "id чата", False),
    ("hack_new", "<хак> <рег> <начало> <конец> [название] — создать", True),
    ("hack_date", "<хак> reg|start|end ДД.ММ.ГГГГ ЧЧ:ММ — дата", True),
    ("hack_title", "<хак> <название>", True),
    ("hack_del", "<хак> — удалить хакатон", True),
    ("repo", "<хак> owner/repo — привязать репозиторий", True),
    ("add", "<хак> @user… — добавить участников", True),
    ("remove", "<хак> @user… — убрать участников", True),
    ("place", "<хак> <место|нет>", True),
    ("site", "<хак> <ссылка> — сайт", True),
    ("orgchat", "<хак> <ссылка> — чат организаторов", True),
    ("spec", "<хак> <ссылка> или файл — ТЗ", True),
    ("set_tz", "Europe/Moscow — часовой пояс", True),
]

PRIVATE_COMMANDS: list[tuple[str, str]] = [
    ("whoami", "Показать мой Telegram user_id"),
]


def group_bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command=name, description=("🔒 " if admin else "") + description)
        for name, description, admin in GROUP_COMMANDS
    ]


def private_bot_commands() -> list[BotCommand]:
    return [BotCommand(command=name, description=desc) for name, desc in PRIVATE_COMMANDS]


HELP_TEXT = """🔒 — только админ бота. &lt;хак&gt; — имя хакатона, например hackaton2026.
Человек: @username, ответ на его сообщение, упоминание из списка, me/я, 🔒 user_id.
Вставить команду из меню без отправки: Tab (ПК) или долгое нажатие (телефон).

<b>Общее</b>
/me — мой id и GitHub
/tz — часовой пояс и время
/chatid — id чата
/whoami — в личке: мой user_id

<b>Хакатоны</b>
/hacks — активные
/hack &lt;хак&gt; — карточка
/left &lt;хак&gt; — сколько осталось
/links &lt;хак&gt; — сайт, чат орг., ТЗ
🔒 /hack_new &lt;хак&gt; &lt;рег&gt; &lt;начало&gt; &lt;конец&gt; [название]
   /hack_new hackaton2026 01.11.2026 23:59 05.11.2026 10:00 07.11.2026 18:00
🔒 /hack_date &lt;хак&gt; reg|start|end ДД.ММ.ГГГГ ЧЧ:ММ
🔒 /hack_title &lt;хак&gt; &lt;название&gt;
🔒 /hack_del &lt;хак&gt;
🔒 /add, /remove &lt;хак&gt; @user1 @user2
🔒 /site, /orgchat &lt;хак&gt; &lt;ссылка|-&gt;
🔒 /spec &lt;хак&gt; &lt;ссылка|-&gt; или файл с подписью /spec &lt;хак&gt;

<b>GitHub и задачи</b>
🔒 /repo &lt;хак&gt; owner/repo
/issues &lt;хак&gt; [@user] [label:метка]
/task &lt;хак&gt; [@user🔒] &lt;номер&gt; — назначить
/untask &lt;хак&gt; [@user🔒] &lt;номер&gt; — снять
/tasks [&lt;хак&gt;] [@user] — задачи
/issue_new &lt;хак&gt; [@кто] &lt;название&gt; — описание со 2-й строки
/link_github [@user🔒] &lt;логин&gt;, /unlink_github [@user🔒]
Статусы: ⚪ не начато, 🟡 в работе (открытый PR или метка «{label}»), ✅ закрыто

<b>Когда можно пинговать</b>
/avail [@user]
/avail_set &lt;дни&gt; &lt;да|нет|09:00-21:00&gt; [@user🔒]
   дни: пн, пн-пт, сб,вс, будни, выходные, все; окна через запятую, можно 22:00-02:00
/avail_reset [@user🔒]

<b>Архив</b>
/history [&lt;хак&gt;], /stats
🔒 /place &lt;хак&gt; &lt;место|нет&gt;

🔒 /set_tz Europe/Moscow
Напоминания: за {offsets} до каждой даты."""


def format_offsets(minutes: list[int]) -> str:
    parts = []
    for value in minutes:
        if value % 60 == 0:
            parts.append(f"{value // 60} ч")
        else:
            parts.append(f"{value} мин")
    return ", ".join(parts)
