"""User-requested minimal Telegram slash menus and matching active handlers.

Regression guard: removed commands must neither appear in the Telegram
command menus nor have message handlers that execute when typed manually.
Owner-only admin commands remain available but hidden.
"""

from config import BOT_COMMANDS, GROUP_MUSIC_COMMANDS
from handlers import admin, group_music, private_music


def test_private_telegram_menu_is_minimal():
    assert [item["command"] for item in BOT_COMMANDS] == [
        "start", "help", "clearall"
    ]


def test_group_telegram_menu_is_minimal():
    assert [item["command"] for item in GROUP_MUSIC_COMMANDS] == [
        "connectmusic", "clearall"
    ]


def test_private_removed_handlers_are_not_registered():
    callbacks = {
        handler.callback.__name__ for handler in private_music.router.message.handlers
    }
    assert callbacks == {"clear_all_private_music"}
    assert not hasattr(private_music, "play_all_private_music")
    assert not hasattr(private_music, "search_private_music")


def test_group_removed_handlers_are_not_registered():
    callbacks = {
        handler.callback.__name__ for handler in group_music.router.message.handlers
    }
    assert callbacks == {
        "connect_music_group", "clear_all_group_text", "connect_admin_music_monitor"
    }
    for removed in (
        "show_group_playlist",
        "clear_music_links",
        "play_all_group_music",
        "search_music",
        "play_sync_info",
        "stop_sync_info",
    ):
        assert not hasattr(group_music, removed)


def test_admin_owner_commands_remain_registered_and_hidden():
    handlers = {
        handler.callback.__name__ for handler in admin.router.message.handlers
    }
    assert "admin_command_menu" in handlers
    assert "connect_admin_music_monitor" in {
        handler.callback.__name__ for handler in group_music.router.message.handlers
    }
    assert all(
        item["command"] not in {"connectadminmusic", "menuadmin"}
        for item in BOT_COMMANDS + GROUP_MUSIC_COMMANDS
    )
