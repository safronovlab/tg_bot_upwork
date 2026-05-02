"""Тесты migrations.py — runner для schema.sql + numbered migrations.

Соответствие DATABASE.md §9.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src import migrations


class TestInitSchema:
    async def test_creates_schema_version_table(self, pool, monkeypatch, stub_log):
        # Имитируем conn.acquire().__aenter__()
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=True)  # таблица существует

        class _Acq:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False

        pool.acquire = MagicMock(return_value=_Acq())
        await migrations.init_schema(pool)
        executes = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "schema_version" in executes

    async def test_bootstrap_when_table_missing(self, pool, monkeypatch, stub_log, tmp_path):
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=False)  # upwork_jobs не существует

        class _Tx:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

        conn.transaction = MagicMock(return_value=_Tx())

        class _Acq:
            async def __aenter__(s):
                return conn

            async def __aexit__(s, *a):
                return False

        pool.acquire = MagicMock(return_value=_Acq())

        # Замокать SCHEMA_PATH чтобы не зависеть от ФС
        monkeypatch.setattr(migrations, "SCHEMA_PATH", tmp_path / "schema.sql", raising=False)
        (tmp_path / "schema.sql").write_text("CREATE TABLE upwork_jobs (id int);")
        monkeypatch.setattr(migrations, "MIGRATIONS", tmp_path / "migrations", raising=False)
        (tmp_path / "migrations").mkdir()

        await migrations.init_schema(pool)
        executes = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "upwork_jobs" in executes

    async def test_skips_already_applied_migrations(self, pool, monkeypatch, stub_log, tmp_path):
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"version": 1}])
        conn.fetchval = AsyncMock(return_value=True)

        class _Tx:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

        conn.transaction = MagicMock(return_value=_Tx())

        class _Acq:
            async def __aenter__(s):
                return conn

            async def __aexit__(s, *a):
                return False

        pool.acquire = MagicMock(return_value=_Acq())

        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        (mig_dir / "001_first.sql").write_text("ALTER TABLE x ADD COLUMN y int;")
        monkeypatch.setattr(migrations, "MIGRATIONS", mig_dir, raising=False)
        monkeypatch.setattr(migrations, "SCHEMA_PATH", tmp_path / "schema.sql", raising=False)

        await migrations.init_schema(pool)
        executes = " ".join(str(c) for c in conn.execute.call_args_list)
        # ALTER TABLE x не должен быть применён, т.к. v=1 уже в applied
        assert "ALTER TABLE x" not in executes

    async def test_applies_pending_migrations_in_order(self, pool, monkeypatch, stub_log, tmp_path):
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])  # ничего не применено
        conn.fetchval = AsyncMock(return_value=True)

        class _Tx:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

        conn.transaction = MagicMock(return_value=_Tx())

        class _Acq:
            async def __aenter__(s):
                return conn

            async def __aexit__(s, *a):
                return False

        pool.acquire = MagicMock(return_value=_Acq())

        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        (mig_dir / "002_second.sql").write_text("ALTER TABLE x ADD COLUMN b int;")
        (mig_dir / "001_first.sql").write_text("ALTER TABLE x ADD COLUMN a int;")
        monkeypatch.setattr(migrations, "MIGRATIONS", mig_dir, raising=False)
        monkeypatch.setattr(migrations, "SCHEMA_PATH", tmp_path / "schema.sql", raising=False)

        await migrations.init_schema(pool)
        executes = [str(c) for c in conn.execute.call_args_list]
        # 001 должен идти раньше 002
        idx_a = next(i for i, s in enumerate(executes) if "ADD COLUMN a" in s)
        idx_b = next(i for i, s in enumerate(executes) if "ADD COLUMN b" in s)
        assert idx_a < idx_b
