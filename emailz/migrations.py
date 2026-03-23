# migrations.py
from mautrix.util.async_db import UpgradeTable, Scheme, Connection

upgrade_table = UpgradeTable()

@upgrade_table.register(description="Initial email_sub table")
async def upgrade_v1(conn: Connection, scheme: Scheme) -> None:
    if scheme == Scheme.SQLITE:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS email_sub (
                room_id     TEXT    NOT NULL,
                alias       TEXT    NOT NULL,
                webhook     TEXT    NOT NULL,
                bearer_hint TEXT,
                created_at  BIGINT  NOT NULL,
                PRIMARY KEY (room_id, alias)
            )
        """)
    else:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS email_sub (
                room_id     TEXT    NOT NULL,
                alias       TEXT    NOT NULL,
                webhook     TEXT    NOT NULL,
                bearer_hint TEXT,
                created_at  BIGINT  NOT NULL,
                PRIMARY KEY (room_id, alias)
            )
        """)

@upgrade_table.register(description="Indexes on room_id and alias")
async def upgrade_v2(conn: Connection, scheme: Scheme) -> None:
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_email_sub_room ON email_sub (room_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_email_sub_alias ON email_sub (alias)")
