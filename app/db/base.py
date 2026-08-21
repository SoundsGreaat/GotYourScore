"""SQLAlchemy 2.0 declarative base.

NOTE: For Alembic autogenerate to see all tables, the model modules must be
imported somewhere before ``Base.metadata`` is inspected. The ``app.models``
package ``__init__`` imports every model, and ``app/db/base_imports.py``
imports ``app.models`` — Alembic's ``env.py`` imports this module chain.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
