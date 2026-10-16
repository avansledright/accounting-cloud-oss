#!/usr/bin/env python3
"""Load obviously-fake demo data into an EMPTY database.

Every name, institution and number here is invented ("Demo Bank",
"Example Credit Union", "Alex & Sam Example"). Use it to try the app or take
screenshots without entering your own data. The same data set is behind the
"Load demo data" button on the first-run setup wizard (see demo_data.py).

Usage:
    MONGO_URI=mongodb://localhost:27017/ python scripts/seed_demo.py
    python scripts/seed_demo.py --force     # wipe the demo collections first

With Docker Compose:
    docker compose exec web python scripts/seed_demo.py

Refuses to run against a database that already has accounts unless --force
is given, and --force deletes ALL documents in the collections it seeds.
Never point it at a database holding real data.
"""
import argparse
import os
import sys

from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from demo_data import DemoDataRefused, load_demo_data  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--force', action='store_true', help='delete existing documents in the seeded collections first')
    args = ap.parse_args()

    client = MongoClient(os.environ.get('MONGO_URI', 'mongodb://localhost:27017/'))
    db = client[os.environ.get('MONGO_DB_NAME', 'accounting_cloud')]

    try:
        load_demo_data(db, force=args.force)
    except DemoDataRefused:
        sys.exit('Database already has accounts; refusing to seed. Re-run with --force '
                 'to DELETE the seeded collections first (never on real data).')
    print('Demo data loaded into database %r.' % db.name)


if __name__ == '__main__':
    main()
