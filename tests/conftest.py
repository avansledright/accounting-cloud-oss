"""Shared test setup.

The pure-module tests only need the project root on sys.path. The app-level
fixtures below import app.py against an in-memory mongomock database: app.py
connects to Mongo and starts the report scheduler at import time, so both are
patched out before the first import. The agent/MCP tools share the same
database through agent.db, as they do in a real deployment.
"""
import os
import sys
from unittest import mock

import pytest

# Make the project root importable without installing anything.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


@pytest.fixture(scope='session')
def app_module():
    mongomock = pytest.importorskip('mongomock')
    import agent.scheduler
    with mock.patch('pymongo.MongoClient', mongomock.MongoClient), \
            mock.patch.object(agent.scheduler, 'schedule_reports', lambda: None):
        import app as app_mod
    app_mod.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app_mod


@pytest.fixture
def db(app_module):
    """The app's (mongomock) database, emptied before each test."""
    database = app_module.db
    for name in database.list_collection_names():
        database[name].delete_many({})
    return database


@pytest.fixture
def client(app_module, db):
    return app_module.app.test_client()
