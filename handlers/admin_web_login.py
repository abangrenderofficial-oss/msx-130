"""Owner-only /sessionlogin: issue temporary website login code via private chat."""
from __future__ import annotations

import os

from aiogram import Router, types
from aiogram.filters import Command

from config import ADMINS_UID

router = Router(name="musix_admin_web_login")


@router.message(Command("sessionlogin"))
async def session_login(message: types.Message) -> None:
    actor = message.from_user
    if not actor or int(actor.id) not in ADMINS_UID or message.chat.type != "private":
        return
    if os.getenv("MUSIX_DASHBOARD_TELEGRAM_AUTH") != "1":
        await message.answer("Website Musix admin belum diaktifkan.")
        return
    from admin.youtube_session.dashboard_server import issue_telegram_login_code

    code = issue_telegram_login_code(int(actor.id))
    origin = os.getenv("MUSIX_ADMIN_PUBLIC_ORIGIN", "").rstrip("/")
    await message.answer(
        "🔐 <b>MUSIX CONTROL — KOD LOGIN OWNER</b>\n\n"
        f"<code>{code}</code>\n\n"
        "Kod sekali guna, tamat tempoh selepas 3 minit. Jangan kongsi kod ini.\n"
        f"Buka: {origin}/admin/youtube-session\n\n"
        "Masukkan kod di website. Sesi dashboard berasingan daripada login Google."
    )
