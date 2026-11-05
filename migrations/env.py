# migrations/env.py
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# this is the Alembic Config object
config = context.config

# Make the application package importable regardless of the working directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Base  # noqa: E402

# Database URL precedence: value passed programmatically (migrate.run_migrations /
# the integration tests) > DATABASE_URL environment variable > alembic.ini
sqlalchemy_url = config.attributes.get("database_url") or os.getenv("DATABASE_URL")
if sqlalchemy_url:
    # Main options go through ConfigParser interpolation: a URL-encoded password such as
    # p%40ss must be escaped or Alembic raises "invalid interpolation syntax".
    config.set_main_option("sqlalchemy.url", sqlalchemy_url.replace("%", "%%"))

# Interpret the config file for Python logging without disabling the application's loggers
# (migrations run in-process from migrate.py and the tests)
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Model metadata for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
