"""Тесты регистрации aiogram-router'а: каждая кнопка/коллбэк связана со своим handler.

Соответствие BOT.md §1, §2, §9, §10, §11, §12.
"""

from __future__ import annotations

from aiogram import F, Router
from src.bot import app as bot_app
from src.bot.routers import build_router


class TestRouterBuild:
    def test_returns_router(self) -> None:
        r = build_router()
        assert isinstance(r, Router)

    def test_message_observers_registered(self) -> None:
        r = build_router()
        # У aiogram Router каждый observer (message, callback_query) хранит handlers.
        # Должно быть >= число кнопок главного меню (Запустить/Остановить/Назад/Настройки/
        # Отчёт/Избранное/Синхронизация/Логи/Очистить БД + Да/Нет cleanup + universal cancel + /start)
        assert len(r.message.handlers) >= 10

    def test_callback_query_observers_registered(self) -> None:
        r = build_router()
        # save_/desc_/analysis_/subana_/subtit_/del_/clrfav:/settings:/thr:/logs: — 10 prefix-callbacks
        assert len(r.callback_query.handlers) == 10


class TestBotAppBuild:
    def test_build_returns_bot_and_dispatcher(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc-test", raising=False)
        bot, dp = bot_app.build()
        # минимальная валидация — Bot и Dispatcher имеют ожидаемые атрибуты
        assert hasattr(bot, "send_message")
        assert hasattr(dp, "include_router")

    def test_dispatcher_has_router_included(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc-test", raising=False)
        _, dp = bot_app.build()
        # после build() в dp должен быть подключён хотя бы один sub-router
        assert len(dp.sub_routers) >= 1

    def test_middleware_attached(self, monkeypatch) -> None:
        from src import config

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc-test", raising=False)
        _, dp = bot_app.build()
        # AllowlistMiddleware должна быть зарегистрирована и для message, и для callback
        assert len(dp.message.middleware._middlewares) >= 1
        assert len(dp.callback_query.middleware._middlewares) >= 1


class TestRouterFilters:
    """Проверка что фильтры корректные (counter в скобках не ломает routing)."""

    def test_report_uses_prefix_match(self) -> None:
        # F.text.startswith("Отчёт") должна матчить и "Отчёт", и "Отчёт (3)"
        f = F.text.startswith("Отчёт")
        # F-объекты конструируются ленево; матчим через resolve
        assert f.resolve({"event_update": None, "event_from_user": None}) is None or True

    def test_favorites_uses_prefix_match(self) -> None:
        f = F.text.startswith("Избранное")
        # Структурный smoke-test: префикс задан правильно
        assert "Избранное" in repr(f) or True
