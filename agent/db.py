# Shared db reference — set by agent_db.init(app_db) from app.py
# Uses a proxy so that `from agent.db import db` captures a stable
# reference that works even after init() sets the real database.


class _DBProxy:
    """Proxy that forwards attribute access to the real pymongo database."""

    def __init__(self):
        self._db = None

    def __getattr__(self, name):
        if self._db is None:
            raise RuntimeError("agent.db not initialized — call agent.db.init() first")
        return getattr(self._db, name)


db = _DBProxy()


def init(app_db):
    db._db = app_db
