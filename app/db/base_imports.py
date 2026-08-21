"""Import all model modules so Base.metadata is fully populated.

Alembic's env.py imports this module; importing ``app.models`` triggers the
import of every model module (User, QAAssignment, Review).
"""

import app.models  # noqa: F401  (side-effect import: registers all tables)
