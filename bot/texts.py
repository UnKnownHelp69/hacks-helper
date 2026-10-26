"""User-facing texts: /help and the command menu (Russian)."""

from __future__ import annotations

from aiogram.types import BotCommand

# (command, description, admin_only)
GROUP_COMMANDS: list[tuple[str, str, bool]] = [
    ("help", "Справка по командам", False),
    ("me", "Зарегистрироваться и показать мой id/username/GitHub", False),
    ("hacks", "Активные хакатоны, участники и даты", False),
    ("hack", "Карточка хакатона: /hack <хакатон>", False),
    ("left", "Сколько осталось до ближайшей даты", False),
    ("tasks", "Задачи: мои, человека или всего хакатона", False),
    ("issues", "Открытые issues репозитория хакатона", False),
    ("task", "Назначить issue: /task <хакатон> [@кто] <номер>", False),
    ("untask", "Снять задачу: /untask <хакатон> [@кто] <номер>", False),
    ("issue_new", "Создать issue в репозитории хакатона", False),
    ("links", "Ссылки и ТЗ хакатона", False),
    ("history", "Архив завершённых хакатонов", False),
    ("stats", "Статистика по участникам", False),
    ("avail", "Расписание доступности (когда можно пинговать)", False),
    ("avail_set", "Изменить расписание: /avail_set <дни> <когда>", False),
    ("avail_reset", "Сбросить расписание на «можно всегда»", False),
    ("link_github", "Привязать GitHub-логин", False),
    ("unlink_github", "Отвязать GitHub-логин", False),
    ("tz", "Часовой пояс чата и текущее время", False),
    ("chatid", "id этого чата", False),
    ("hack_new", "Создать хакатон", True),
    ("hack_date", "Изменить дату хакатона", True),
    ("hack_title", "Изменить название хакатона", True),
    ("hack_del", "Удалить хакатон", True),
    ("repo", "Привязать репозиторий к хакатону", True),
    ("add", "Добавить участников в хакатон", True),
    ("remove", "Убрать участников из хакатона", True),
    ("place", "Указать занятое место", True),
    ("site", "Ссылка на сайт хакатона", True),
    ("orgchat", "Ссылка на чат организаторов", True),
    ("spec", "ТЗ: ссылка или файл", True),
    ("set_tz", "Сменить часовой пояс чата", True),
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


HELP_TEXT = """<b>Бот координации команды на хакатонах</b>
🔒 — только для администраторов бота. &lt;хак&gt; — короткое имя хакатона, например <code>hackaton2026</code>.

<b>Как указать человека</b> (в любой команде, где нужен пользователь):
• <code>@username</code> — если бот уже видел этого человека в чате;
• ответить командой на сообщение человека;
• упомянуть человека без username, выбрав его из списка участников;
• <code>me</code> или <code>я</code> — вы сами;
• 🔒 числовой user_id.
Если бот кого-то не знает — пусть человек напишет в чат что угодно или /me.

<b>Общее</b>
/me — зарегистрироваться, показать свой id, username и GitHub
/tz — часовой пояс чата и текущее время
/chatid — id этого чата
/whoami — в личке с ботом: ваш user_id (для BOT_ADMIN_IDS)

<b>Хакатоны</b>
/hacks — активные хакатоны с участниками и датами
/hack &lt;хак&gt; — даты, репозиторий, участники, ссылки
/left &lt;хак&gt; — сколько осталось до ближайшей даты и какой это этап
/links &lt;хак&gt; — сайт, чат организаторов и ТЗ (файл пришлю в чат)
🔒 /hack_new &lt;хак&gt; &lt;конец регистрации&gt; &lt;начало&gt; &lt;конец&gt; [название]
   <code>/hack_new hackaton2026 01.11.2026 23:59 05.11.2026 10:00 07.11.2026 18:00 Цифровой прорыв</code>
🔒 /hack_date &lt;хак&gt; reg|start|end &lt;ДД.ММ.ГГГГ ЧЧ:ММ&gt;
   <code>/hack_date hackaton2026 start 05.11.2026 12:00</code>
🔒 /hack_title &lt;хак&gt; &lt;название&gt;
🔒 /hack_del &lt;хак&gt; — удалить (с подтверждением)
🔒 /add &lt;хак&gt; @user1 @user2 … — добавить участников (или ответом: /add &lt;хак&gt;)
🔒 /remove &lt;хак&gt; @user … — убрать участников
🔒 /site, /orgchat &lt;хак&gt; &lt;ссылка&gt; — сайт и чат организаторов («-» — удалить)
🔒 /spec &lt;хак&gt; &lt;ссылка&gt; — ТЗ ссылкой; или отправьте файл с подписью <code>/spec &lt;хак&gt;</code>, или ответьте <code>/spec &lt;хак&gt;</code> на сообщение с файлом

<b>GitHub и задачи</b>
🔒 /repo &lt;хак&gt; owner/repo — привязать репозиторий
/issues &lt;хак&gt; [@user] [label:метка] — открытые issues
   <code>/issues hackaton2026 label:"in progress"</code>
/task &lt;хак&gt; &lt;номер&gt; — взять issue себе
🔒 /task &lt;хак&gt; @user &lt;номер&gt; — назначить другому (или ответом: /task &lt;хак&gt; &lt;номер&gt;)
/untask &lt;хак&gt; &lt;номер&gt; — снять свою задачу; 🔒 /untask &lt;хак&gt; @user &lt;номер&gt;
/tasks — мои задачи; /tasks @user — задачи человека; /tasks &lt;хак&gt; — все задачи хакатона по людям; /tasks &lt;хак&gt; @user
/issue_new &lt;хак&gt; [@исполнитель] &lt;название&gt; — создать issue (описание — со следующей строки). Участник может назначить только себя, 🔒 — кого угодно
/link_github &lt;логин&gt; — привязать свой GitHub; 🔒 /link_github @user &lt;логин&gt;
/unlink_github — отвязать свой GitHub; 🔒 /unlink_github @user
Статусы обновляются автоматически: ⚪ не начато, 🟡 в работе (есть открытый PR со ссылкой на issue или метка «{label}»), ✅ закрыто.

<b>Доступность (когда можно пинговать)</b>
/avail [@user] — посмотреть расписание
/avail_set &lt;дни&gt; &lt;когда&gt; — дни: пн…вс, пн-пт, сб,вс, будни, выходные, все; когда: да / нет / 09:00-21:00 (несколько окон через запятую, можно через полночь: 22:00-02:00)
   <code>/avail_set ср 09:00-21:00</code>  <code>/avail_set чт нет</code>  <code>/avail_set пт 17:00-21:00</code>
   🔒 <code>/avail_set пн-пт 10:00-22:00 @user</code>
/avail_reset — снова «можно всегда»; 🔒 /avail_reset @user
Если человеку сейчас нельзя, напоминание придёт ему в начале ближайшего разрешённого окна (если оно до дедлайна).

<b>Архив и статистика</b>
/history — завершённые хакатоны; /history &lt;хак&gt; — подробно
/stats — сколько хакатонов и закрытых issues у каждого
🔒 /place &lt;хак&gt; &lt;место&gt; или /place &lt;хак&gt; нет

<b>Часовой пояс</b>
/tz — текущий; 🔒 /set_tz Europe/Moscow (любое IANA-название, например Asia/Yekaterinburg)
Даты вводятся и показываются в часовом поясе чата в формате ДД.ММ.ГГГГ ЧЧ:ММ.

Напоминания приходят перед окончанием регистрации, началом и концом хакатона: за {offsets}."""


def format_offsets(minutes: list[int]) -> str:
    parts = []
    for value in minutes:
        if value % 60 == 0:
            parts.append(f"{value // 60} ч")
        else:
            parts.append(f"{value} мин")
    return ", ".join(parts)
