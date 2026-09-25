"""Build small historical databases through the real forward migration chain."""

from unittest.mock import patch

from flask import Flask

from cuoti_app import database
from cuoti_app.migrations import migration_schema_issues


def create_historical_database(path, version):
    app = Flask(__name__)
    app.config["DATABASE"] = str(path)
    # Current schema_issues requires newer tables. Fixture construction instead
    # checks the registered contract for its actual historical target version.
    with (
        app.app_context(),
        patch.object(database, "DB_SCHEMA_VERSION", version),
        patch.object(database, "schema_issues", side_effect=lambda db: migration_schema_issues(db, version)),
    ):
        database.init_db()
