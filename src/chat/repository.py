"""Изолированные SQL-запросы для chat-подсистемы. См. CHAT.md §3.

Phase 0: основные хелперы уже в src/db.py (insert_*, drain_*, get_thread_history,
has_recent_human_outbound). Этот модуль зарезервирован под Phase 1+ для
chat-специфичных запросов которые не имеет смысла класть в общий db.py
(thread aggregations, статистика по escalate-причинам, audit-логи).

В Phase 0 — пустой модуль (placeholder).
"""

from __future__ import annotations
