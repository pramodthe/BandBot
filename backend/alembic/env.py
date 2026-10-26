from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import app.persistence.models  # noqa: F401  (register every table)
from app.config import settings, sqlalchemy_url

config = context.config
target_metadata = SQLModel.metadata


def _url() -> str:
    # The alembic CLI migrates the configured DATABASE_URL, as API startup does.
    # app.persistence.migrate passes its own connection instead.
    return sqlalchemy_url(settings.database_url)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    section = {**config.get_section(config.config_ini_section, {}), "sqlalchemy.url": _url()}
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
