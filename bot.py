"""
Group Manager Bot  |  Made by Bobby Maharaj
python-telegram-bot v21 + MongoDB (motor) | Render free web service ready
Aesthetic cards + selectable fonts (small caps / bold / script / plain)
"""
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from html import escape

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from telegram import (BotCommand, ChatPermissions, InlineKeyboardButton,
                      InlineKeyboardMarkup, Update)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("groupbot")

TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
CREDIT = "✦ ʙᴏᴛ ʙʏ <b>Bobby Maharaj</b> ✦"
HTML = ParseMode.HTML

db = None
CACHE: dict = {}
ADMIN_CACHE: dict = {}
flood = defaultdict(lambda: deque(maxlen=6))

DEFAULTS = {
    "antilink": False, "antiflood": True, "antibadword": False,
    "welcome": True, "goodbye": False, "locked": False,
    "warn_limit": 3, "warn_action": "mute", "badwords": [],
    "welcome_text": None, "font": "smallcaps",
    "rules": "No rules set yet. Admins can use /setrules.",
}
LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|\b[\w-]+\.(com|in|net|org|io|me|xyz|co)\b)", re.I)
MUTED = ChatPermissions(can_send_messages=False)
FREE = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True)


# ───────────── fonts & aesthetic cards ─────────────
def _mk(up, lo, dg=None):
    m = {}
    for i in range(26):
        m[chr(65 + i)] = chr(up + i)
        m[chr(97 + i)] = chr(lo + i)
    if dg:
        for i in range(10):
            m[str(i)] = chr(dg + i)
    return m


SC = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
FONTS = {
    "smallcaps": {**{chr(97 + i): SC[i] for i in range(26)},
                  **{chr(65 + i): SC[i] for i in range(26)}},
    "bold": _mk(0x1D5D4, 0x1D5EE, 0x1D7EC),
    "script": _mk(0x1D4D0, 0x1D4EA),
    "plain": {},
}
FONT_NAMES = {"smallcaps": "ꜱᴍᴀʟʟ ᴄᴀᴘꜱ", "bold": "𝗕𝗼𝗹𝗱", "script": "𝓢𝓬𝓻𝓲𝓹𝓽", "plain": "Plain"}
FONT_ORDER = list(FONT_NAMES)
# tags, entities, /commands and {placeholders} are never restyled
TOKEN_RE = re.compile(r"(<[^>]+>|&\w+;|/\w+|\{\w+\})")


def fx(text, font="smallcaps"):
    mp = FONTS.get(font) or {}
    if not mp:
        return text
    out, skip = [], False
    for part in TOKEN_RE.split(text):
        if not part:
            continue
        if part.startswith("<"):
            low = part.lower()
            if low.startswith(("<a ", "<code")):
                skip = True
            elif low.startswith(("</a", "</code")):
                skip = False
            out.append(part)
        elif skip or TOKEN_RE.fullmatch(part):
            out.append(part)
        else:
            out.append("".join(mp.get(c, c) for c in part))
    return "".join(out)


def card(title, *lines):
    body = "\n".join(f"│ {l}" if l else "│" for l in lines)
    return f"╭─── ✦ <b>{title}</b> ✦ ───╮\n{body}\n╰────────────────────╯"


# ───────────── helpers ─────────────
def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{escape(name)}</a>'


async def get_cfg(chat_id):
    if chat_id not in CACHE:
        doc = await db.chats.find_one({"_id": chat_id}) or {}
        CACHE[chat_id] = {**DEFAULTS, **doc}
    return CACHE[chat_id]


async def set_cfg(chat_id, **kw):
    await db.chats.update_one({"_id": chat_id}, {"$set": kw}, upsert=True)
    CACHE.pop(chat_id, None)


async def render(chat_id, text, credit=False):
    cfg = await get_cfg(chat_id)
    return fx(text, cfg["font"]) + (f"\n\n{CREDIT}" if credit else "")


async def say(update, text, credit=False, **kw):
    t = await render(update.effective_chat.id, text, credit)
    return await update.effective_message.reply_text(t, parse_mode=HTML, **kw)


async def post(ctx, chat_id, text, credit=False):
    t = await render(chat_id, text, credit)
    return await ctx.bot.send_message(chat_id, t, parse_mode=HTML)


async def is_admin(chat, uid):
    if uid == OWNER_ID:
        return True
    ts, ids = ADMIN_CACHE.get(chat.id, (0, set()))
    if time.time() - ts > 60:
        ids = {a.user.id for a in await chat.get_administrators()}
        ADMIN_CACHE[chat.id] = (time.time(), ids)
    return uid in ids


def admin_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat, user = update.effective_chat, update.effective_user
        if chat.type == "private":
            return await say(update, "This command works only in groups.")
        if not await is_admin(chat, user.id):
            return await say(update, "⛔ Admins only.")
        return await fn(update, ctx)
    return wrapper


async def get_target(update, ctx):
    """Returns (uid, name, remaining_args)."""
    m = update.effective_message
    args = list(ctx.args)
    if m.reply_to_message and m.reply_to_message.from_user:
        u = m.reply_to_message.from_user
        return u.id, u.full_name, args
    if args and args[0].lstrip("-").isdigit():
        return int(args[0]), args[0], args[1:]
    await say(update, "Reply to a message or give a user ID.")
    return None, None, []


async def protected(update, ctx, uid):
    if uid == ctx.bot.id or await is_admin(update.effective_chat, uid):
        await say(update, "😅 I can't take action on admins or myself.")
        return True
    return False


async def do_warn(ctx, chat, uid, name, reason):
    cfg = await get_cfg(chat.id)
    key = f"{chat.id}:{uid}"
    doc = await db.warns.find_one_and_update(
        {"_id": key}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    n, lim = doc["n"], cfg["warn_limit"]
    lines = [f"👤 {mention(uid, name)}", f"⚠️ Warns: {n}/{lim}", f"📝 {escape(reason)}"]
    if n >= lim:
        await db.warns.delete_one({"_id": key})
        act = cfg["warn_action"]
        if act == "ban":
            await chat.ban_member(uid)
        elif act == "kick":
            await chat.ban_member(uid)
            await chat.unban_member(uid)
        else:
            await chat.restrict_member(uid, MUTED)
        lines.append(f"🚫 Limit reached → {act.upper()}")
    await post(ctx, chat.id, card("Warning", *lines))


# ───────────── general commands ─────────────
async def start(update, ctx):
    await say(update, card(
        "Group Manager", "🛡 Add me to your group",
        "👮 Make me admin (delete, restrict, ban)", "⚙️ Then open /settings", "",
        "📖 All commands: /help"), credit=True)


async def help_cmd(update, ctx):
    await say(update, card(
        "Commands",
        "<b>Admin</b>",
        "⚙️ /settings — control panel",
        "⚠️ /warn /unwarn /warns",
        "🔇 /mute [min] · /unmute",
        "🔨 /ban · /unban · /kick",
        "🔒 /lock · /unlock",
        "🧹 /purge — reply to start point",
        "👋 /setwelcome text (use {name} {group})",
        "📜 /setrules text",
        "🤬 /addbadword · /rmbadword · /badwords",
        "",
        "<b>Everyone</b>",
        "📜 /rules · 🆔 /id · ℹ️ /about"), credit=True)


async def about(update, ctx):
    await say(update, card(
        "About", "🛡 Anti-link · Anti-flood", "🤬 Bad words · ⚠️ Warns",
        "👋 Welcome · ⚙️ Admin panel", "🔤 Custom fonts"), credit=True)


async def rules(update, ctx):
    cfg = await get_cfg(update.effective_chat.id)
    await say(update, card("Rules", escape(cfg["rules"])))


async def my_id(update, ctx):
    m = update.effective_message
    u = m.reply_to_message.from_user if m.reply_to_message else update.effective_user
    await say(update, card("ID Info", f"👤 {escape(u.full_name)}",
                           f"🆔 <code>{u.id}</code>", f"💬 <code>{update.effective_chat.id}</code>"))


async def stats(update, ctx):
    if update.effective_user.id != OWNER_ID:
        return
    g = await db.chats.count_documents({})
    w = await db.warns.count_documents({})
    await say(update, card("Stats", f"👥 Groups: {g}", f"⚠️ Active warns: {w}"), credit=True)


# ───────────── moderation commands ─────────────
@admin_only
async def warn_cmd(update, ctx):
    uid, name, rest = await get_target(update, ctx)
    if uid is None or await protected(update, ctx, uid):
        return
    await do_warn(ctx, update.effective_chat, uid, name, " ".join(rest) or "No reason")


@admin_only
async def unwarn_cmd(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    key = f"{update.effective_chat.id}:{uid}"
    doc = await db.warns.find_one({"_id": key})
    if not doc or doc["n"] <= 1:
        await db.warns.delete_one({"_id": key})
    else:
        await db.warns.update_one({"_id": key}, {"$inc": {"n": -1}})
    await say(update, f"✅ One warn removed for {escape(name)}.")


@admin_only
async def warns_cmd(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    cfg = await get_cfg(update.effective_chat.id)
    doc = await db.warns.find_one({"_id": f"{update.effective_chat.id}:{uid}"})
    await say(update, f"⚠️ {escape(name)}: {doc['n'] if doc else 0}/{cfg['warn_limit']} warns")


@admin_only
async def mute_cmd(update, ctx):
    uid, name, rest = await get_target(update, ctx)
    if uid is None or await protected(update, ctx, uid):
        return
    until, label = None, "permanently"
    if rest and rest[0].isdigit():
        mins = int(rest[0])
        until = datetime.now(timezone.utc) + timedelta(minutes=mins)
        label = f"for {mins} min"
    await update.effective_chat.restrict_member(uid, MUTED, until_date=until)
    await say(update, f"🔇 {mention(uid, name)} muted {label}.")


@admin_only
async def unmute_cmd(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    await update.effective_chat.restrict_member(uid, FREE)
    await say(update, f"🔊 {mention(uid, name)} unmuted.")


@admin_only
async def ban_cmd(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None or await protected(update, ctx, uid):
        return
    await update.effective_chat.ban_member(uid)
    await say(update, f"🔨 {mention(uid, name)} banned.")


@admin_only
async def unban_cmd(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None:
        return
    await update.effective_chat.unban_member(uid, only_if_banned=True)
    await say(update, f"✅ {escape(name)} unbanned.")


@admin_only
async def kick_cmd(update, ctx):
    uid, name, _ = await get_target(update, ctx)
    if uid is None or await protected(update, ctx, uid):
        return
    await update.effective_chat.ban_member(uid)
    await update.effective_chat.unban_member(uid)
    await say(update, f"👢 {mention(uid, name)} kicked.")


@admin_only
async def lock_cmd(update, ctx):
    await set_cfg(update.effective_chat.id, locked=True)
    await say(update, "🔒 Chat locked — only admins can talk.")


@admin_only
async def unlock_cmd(update, ctx):
    await set_cfg(update.effective_chat.id, locked=False)
    await say(update, "🔓 Chat unlocked.")


@admin_only
async def purge_cmd(update, ctx):
    m = update.effective_message
    if not m.reply_to_message:
        return await say(update, "Reply to the message you want to purge from.")
    for i in range(m.reply_to_message.message_id, m.message_id + 1):
        try:
            await ctx.bot.delete_message(update.effective_chat.id, i)
        except Exception:
            pass


def _text_arg(update):
    parts = update.effective_message.text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


@admin_only
async def setwelcome(update, ctx):
    t = _text_arg(update)
    if not t:
        return await say(update, "Use: /setwelcome Welcome {name} to {group}!\n"
                                 "Use /setwelcome reset for the default style.")
    await set_cfg(update.effective_chat.id, welcome_text=None if t.lower() == "reset" else t)
    await say(update, "✅ Welcome message updated.")


@admin_only
async def setrules(update, ctx):
    t = _text_arg(update)
    if not t:
        return await say(update, "Use: /setrules your rules here")
    await set_cfg(update.effective_chat.id, rules=t)
    await say(update, "✅ Rules saved.")


@admin_only
async def addbadword(update, ctx):
    w = _text_arg(update).lower()
    if not w:
        return await say(update, "Use: /addbadword word")
    cfg = await get_cfg(update.effective_chat.id)
    await set_cfg(update.effective_chat.id, badwords=sorted(set(cfg["badwords"]) | {w}))
    await say(update, f"✅ Added: {escape(w)}")


@admin_only
async def rmbadword(update, ctx):
    w = _text_arg(update).lower()
    cfg = await get_cfg(update.effective_chat.id)
    await set_cfg(update.effective_chat.id, badwords=[x for x in cfg["badwords"] if x != w])
    await say(update, f"✅ Removed: {escape(w)}")


@admin_only
async def badwords_cmd(update, ctx):
    cfg = await get_cfg(update.effective_chat.id)
    await say(update, "🤬 " + (escape(", ".join(cfg["badwords"])) or "No bad words set."))


# ───────────── admin panel ─────────────
TOGGLES = [("antilink", "🔗 Anti-Link"), ("antiflood", "🌊 Anti-Flood"),
           ("antibadword", "🤬 Bad Words"), ("welcome", "👋 Welcome"),
           ("goodbye", "🚪 Goodbye"), ("locked", "🔒 Lock Chat")]
ACTIONS = ["mute", "kick", "ban"]


def panel_text(chat, cfg):
    t = card("Admin Panel", f"💬 {escape(chat.title or '')}", "Tap a button to change a setting")
    return fx(t, cfg["font"]) + f"\n\n{CREDIT}"


def panel_kb(cfg):
    f = cfg["font"]
    rows = [[InlineKeyboardButton(f"{fx(l, f)}: {'✅' if cfg[k] else '❌'}", callback_data=f"t:{k}")]
            for k, l in TOGGLES]
    rows.append([InlineKeyboardButton("➖", callback_data="w:-"),
                 InlineKeyboardButton(fx(f"Warn limit: {cfg['warn_limit']}", f), callback_data="noop"),
                 InlineKeyboardButton("➕", callback_data="w:+")])
    rows.append([InlineKeyboardButton(fx(f"At limit: {cfg['warn_action'].upper()}", f) + " 🔁",
                                      callback_data="a:cycle")])
    rows.append([InlineKeyboardButton(f"🔤 Font: {FONT_NAMES[f]} 🔁", callback_data="f:cycle")])
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="close")])
    return InlineKeyboardMarkup(rows)


@admin_only
async def settings(update, ctx):
    chat = update.effective_chat
    cfg = await get_cfg(chat.id)
    await update.effective_message.reply_text(
        panel_text(chat, cfg), reply_markup=panel_kb(cfg), parse_mode=HTML)


async def panel_cb(update, ctx):
    q = update.callback_query
    chat = q.message.chat
    if not await is_admin(chat, q.from_user.id):
        return await q.answer("⛔ Admins only", show_alert=True)
    data = q.data
    if data == "noop":
        return await q.answer()
    if data == "close":
        await q.answer()
        return await q.message.delete()
    cfg = await get_cfg(chat.id)
    if data.startswith("t:"):
        k = data[2:]
        await set_cfg(chat.id, **{k: not cfg[k]})
    elif data.startswith("w:"):
        n = cfg["warn_limit"] + (1 if data[2] == "+" else -1)
        await set_cfg(chat.id, warn_limit=max(1, min(10, n)))
    elif data == "a:cycle":
        await set_cfg(chat.id, warn_action=ACTIONS[(ACTIONS.index(cfg["warn_action"]) + 1) % len(ACTIONS)])
    elif data == "f:cycle":
        await set_cfg(chat.id, font=FONT_ORDER[(FONT_ORDER.index(cfg["font"]) + 1) % len(FONT_ORDER)])
    cfg = await get_cfg(chat.id)
    await q.answer("Updated ✅")
    try:
        await q.edit_message_text(panel_text(chat, cfg), reply_markup=panel_kb(cfg), parse_mode=HTML)
    except Exception as e:
        log.debug("edit skipped: %s", e)


# ───────────── events ─────────────
async def on_join(update, ctx):
    chat = update.effective_chat
    cfg = await get_cfg(chat.id)
    if not cfg["welcome"]:
        return
    for u in update.effective_message.new_chat_members:
        if u.is_bot:
            continue
        if cfg["welcome_text"]:
            txt = (escape(cfg["welcome_text"]).replace("{name}", mention(u.id, u.full_name))
                   .replace("{group}", escape(chat.title or "")))
        else:
            txt = fx(card("Welcome", f"👤 {mention(u.id, u.full_name)}",
                          f"🏠 to <b>{escape(chat.title or '')}</b>", "",
                          "📜 Read the /rules"), cfg["font"])
        try:
            await chat.send_message(txt, parse_mode=HTML)
        except Exception:
            await chat.send_message(f"👋 Welcome {mention(u.id, u.full_name)}!", parse_mode=HTML)


async def on_leave(update, ctx):
    chat = update.effective_chat
    cfg = await get_cfg(chat.id)
    u = update.effective_message.left_chat_member
    if cfg["goodbye"] and u and not u.is_bot:
        await post(ctx, chat.id, f"👋 {escape(u.full_name)} left the group.")


async def watcher(update, ctx):
    m, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not m or not user or m.sender_chat:
        return
    try:
        if await is_admin(chat, user.id):
            return
        cfg = await get_cfg(chat.id)
        text = (m.text or m.caption or "")
        if cfg["locked"]:
            return await m.delete()
        if cfg["antiflood"]:
            q, now = flood[(chat.id, user.id)], time.time()
            q.append(now)
            if len(q) == 6 and now - q[0] < 6:
                q.clear()
                await m.delete()
                await chat.restrict_member(
                    user.id, MUTED, until_date=datetime.now(timezone.utc) + timedelta(minutes=5))
                return await post(ctx, chat.id, f"🌊 Flood! {mention(user.id, user.full_name)} muted for 5 min.")
        if cfg["antilink"] and LINK_RE.search(text):
            await m.delete()
            return await do_warn(ctx, chat, user.id, user.full_name, "Links are not allowed")
        if cfg["antibadword"] and any(w in text.lower() for w in cfg["badwords"]):
            await m.delete()
            return await do_warn(ctx, chat, user.id, user.full_name, "Bad word")
    except Exception as e:
        log.warning("watcher error in %s: %s (bot needs admin rights)", chat.id, e)


async def on_error(update, ctx):
    log.error("Error: %s", ctx.error)


async def post_init(app):
    global db
    db = AsyncIOMotorClient(MONGO_URI)["groupbot"]
    await app.bot.set_my_commands([
        BotCommand("settings", "Admin panel"), BotCommand("help", "Commands"),
        BotCommand("rules", "Group rules"), BotCommand("warn", "Warn user"),
        BotCommand("mute", "Mute user"), BotCommand("ban", "Ban user"),
        BotCommand("about", "About bot")])


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    cmds = {
        "start": start, "help": help_cmd, "about": about, "rules": rules, "id": my_id,
        "stats": stats, "settings": settings, "warn": warn_cmd, "unwarn": unwarn_cmd,
        "warns": warns_cmd, "mute": mute_cmd, "unmute": unmute_cmd, "ban": ban_cmd,
        "unban": unban_cmd, "kick": kick_cmd, "lock": lock_cmd, "unlock": unlock_cmd,
        "purge": purge_cmd, "setwelcome": setwelcome, "setrules": setrules,
        "addbadword": addbadword, "rmbadword": rmbadword, "badwords": badwords_cmd,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(panel_cb))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_join))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_leave))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL, watcher), group=1)
    app.add_error_handler(on_error)

    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:  # Render: webhook mode
        app.run_webhook(listen="0.0.0.0", port=int(os.environ.get("PORT", 10000)),
                        url_path=TOKEN, webhook_url=f"{url}/{TOKEN}",
                        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    else:  # local testing
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
