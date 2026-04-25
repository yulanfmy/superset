# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import os
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from superset.app import AppRootMiddleware, create_app, SupersetApp
from superset.constants import (
    CHANGE_ME_SECRET_KEY,
    SECRET_KEY_MIN_LENGTH,
)
from superset.initialization import SupersetAppInitializer


class TestSupersetApp:
    @patch("superset.app.logger")
    def test_sync_config_to_db_skips_when_no_tables(self, mock_logger):
        """Test that sync is skipped when database is not up-to-date."""
        # Setup
        app = SupersetApp(__name__)
        app.config = {"SQLALCHEMY_DATABASE_URI": "postgresql://user:pass@host:5432/db"}

        # Mock _is_database_up_to_date to return False
        with patch.object(app, "_is_database_up_to_date", return_value=False):
            # Execute
            app.sync_config_to_db()

        # Assert
        mock_logger.info.assert_called_once_with(
            "Pending database migrations: run 'superset db upgrade'"
        )

    @patch("superset.extensions.db")
    @patch("superset.app.logger")
    def test_sync_config_to_db_handles_operational_error(self, mock_logger, mock_db):
        """Test that OperationalError during migration check is handled gracefully."""
        # Setup
        app = SupersetApp(__name__)
        app.config = {"SQLALCHEMY_DATABASE_URI": "postgresql://user:pass@host:5432/db"}
        error_msg = "Cannot connect to database"

        # Mock db.engine.connect to raise an OperationalError
        mock_db.engine.connect.side_effect = OperationalError(error_msg, None, None)

        # Execute
        app.sync_config_to_db()

        # Assert - _is_database_up_to_date should catch the error and return False
        # which causes the info log about pending migrations
        mock_logger.info.assert_called_once_with(
            "Pending database migrations: run 'superset db upgrade'"
        )

    @patch("superset.extensions.feature_flag_manager")
    @patch("superset.app.logger")
    @patch("superset.commands.theme.seed.SeedSystemThemesCommand")
    def test_sync_config_to_db_initializes_when_tables_exist(
        self,
        mock_seed_themes_command,
        mock_logger,
        mock_feature_flag_manager,
    ):
        """Test that features are initialized when database is up-to-date."""
        # Setup
        app = SupersetApp(__name__)
        app.config = {"SQLALCHEMY_DATABASE_URI": "postgresql://user:pass@host:5432/db"}
        mock_feature_flag_manager.is_feature_enabled.return_value = True
        mock_seed_themes = MagicMock()
        mock_seed_themes_command.return_value = mock_seed_themes

        # Mock _is_database_up_to_date to return True
        with (
            patch.object(app, "_is_database_up_to_date", return_value=True),
            patch(
                "superset.tags.core.register_sqla_event_listeners"
            ) as mock_register_listeners,
        ):
            # Execute
            app.sync_config_to_db()

        # Assert
        mock_feature_flag_manager.is_feature_enabled.assert_called_with(
            "TAGGING_SYSTEM"
        )
        mock_register_listeners.assert_called_once()
        # Should seed themes
        mock_seed_themes_command.assert_called_once()
        mock_seed_themes.run.assert_called_once()
        # Should log successful completion
        mock_logger.info.assert_any_call("Syncing configuration to database...")
        mock_logger.info.assert_any_call(
            "Configuration sync to database completed successfully"
        )


class TestSupersetAppInitializer:
    @patch("superset.initialization.logger")
    def test_init_app_in_ctx_calls_sync_config_to_db(self, mock_logger):
        """Test that initialization calls app.sync_config_to_db()."""
        # Setup
        mock_app = MagicMock()
        mock_app.config = {
            "SQLALCHEMY_DATABASE_URI": "postgresql://user:pass@host:5432/db",
            "FLASK_APP_MUTATOR": None,
        }
        app_initializer = SupersetAppInitializer(mock_app)

        # Execute init_app_in_ctx which calls sync_config_to_db
        with (
            patch.object(app_initializer, "configure_fab"),
            patch.object(app_initializer, "configure_url_map_converters"),
            patch.object(app_initializer, "configure_data_sources"),
            patch.object(app_initializer, "configure_auth_provider"),
            patch.object(app_initializer, "configure_async_queries"),
            patch.object(app_initializer, "configure_ssh_manager"),
            patch.object(app_initializer, "configure_stats_manager"),
            patch.object(app_initializer, "init_views"),
        ):
            app_initializer.init_app_in_ctx()

        # Assert that sync_config_to_db was called on the app
        mock_app.sync_config_to_db.assert_called_once()

    def test_database_uri_lazy_property(self):
        """Test database_uri property uses lazy initialization with smart caching."""
        # Setup
        mock_app = MagicMock()
        test_uri = "postgresql://user:pass@host:5432/testdb"
        mock_app.config = {"SQLALCHEMY_DATABASE_URI": test_uri}
        app_initializer = SupersetAppInitializer(mock_app)

        # Ensure cache is None initially
        assert app_initializer._db_uri_cache is None

        # First access should set the cache (valid URI)
        uri = app_initializer.database_uri
        assert uri == test_uri
        assert app_initializer._db_uri_cache is not None
        assert app_initializer._db_uri_cache == test_uri

        # Second access should use cache (not call config.get again)
        # Change the config to verify cache is being used
        mock_app.config["SQLALCHEMY_DATABASE_URI"] = "different_uri"
        uri2 = app_initializer.database_uri
        assert (
            uri2 == test_uri
        )  # Should still return cached value (not "different_uri")

    def test_database_uri_doesnt_cache_fallback_values(self):
        """Test that fallback values like 'nouser' are not cached."""
        # Setup
        mock_app = MagicMock()

        # Initially return the fallback nouser URI
        config_dict = {
            "SQLALCHEMY_DATABASE_URI": "postgresql://nouser:nopassword@nohost:5432/nodb"
        }
        mock_app.config = config_dict
        app_initializer = SupersetAppInitializer(mock_app)

        # First access returns fallback but shouldn't cache it
        uri1 = app_initializer.database_uri
        assert uri1 == "postgresql://nouser:nopassword@nohost:5432/nodb"
        assert app_initializer._db_uri_cache is None  # Should NOT be cached

        # Now config is properly loaded - update the same dict
        config_dict["SQLALCHEMY_DATABASE_URI"] = (
            "postgresql://realuser:realpass@realhost:5432/realdb"
        )

        # Second access should get the new value since fallback wasn't cached
        uri2 = app_initializer.database_uri
        assert uri2 == "postgresql://realuser:realpass@realhost:5432/realdb"
        assert app_initializer._db_uri_cache is not None  # Now it should be cached
        assert (
            app_initializer._db_uri_cache
            == "postgresql://realuser:realpass@realhost:5432/realdb"
        )


class TestCreateAppRoot:
    """Test app root resolution precedence in create_app."""

    @patch("superset.initialization.SupersetAppInitializer.init_app")
    def test_default_app_root_no_middleware(self, mock_init_app):
        """No param, no config, no env var: app_root is '/', no middleware."""
        env = os.environ.copy()
        env.pop("SUPERSET_APP_ROOT", None)
        env.pop("SUPERSET_CONFIG", None)
        with patch.dict(os.environ, env, clear=True):
            app = create_app()

        assert not isinstance(app.wsgi_app, AppRootMiddleware)

    @patch("superset.initialization.SupersetAppInitializer.init_app")
    def test_application_root_config_activates_middleware(self, mock_init_app):
        """APPLICATION_ROOT in config activates AppRootMiddleware."""
        env = os.environ.copy()
        env.pop("SUPERSET_APP_ROOT", None)
        env.pop("SUPERSET_CONFIG", None)
        with (
            patch.dict(os.environ, env, clear=True),
            patch("superset.config.APPLICATION_ROOT", "/from-config", create=True),
        ):
            app = create_app()

        assert isinstance(app.wsgi_app, AppRootMiddleware)
        assert app.wsgi_app.app_root == "/from-config"

    @patch("superset.initialization.SupersetAppInitializer.init_app")
    def test_env_var_activates_middleware(self, mock_init_app):
        """SUPERSET_APP_ROOT env var activates AppRootMiddleware."""
        env = os.environ.copy()
        env.pop("SUPERSET_CONFIG", None)
        env["SUPERSET_APP_ROOT"] = "/from-env"
        with patch.dict(os.environ, env, clear=True):
            app = create_app()

        assert isinstance(app.wsgi_app, AppRootMiddleware)
        assert app.wsgi_app.app_root == "/from-env"

    @patch("superset.initialization.SupersetAppInitializer.init_app")
    def test_env_var_takes_precedence_over_config(self, mock_init_app):
        """SUPERSET_APP_ROOT env var wins over APPLICATION_ROOT config."""
        env = os.environ.copy()
        env.pop("SUPERSET_CONFIG", None)
        env["SUPERSET_APP_ROOT"] = "/from-env"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("superset.config.APPLICATION_ROOT", "/from-config", create=True),
        ):
            app = create_app()

        assert isinstance(app.wsgi_app, AppRootMiddleware)
        assert app.wsgi_app.app_root == "/from-env"

    @patch("superset.initialization.SupersetAppInitializer.init_app")
    def test_param_takes_precedence_over_env_var(self, mock_init_app):
        """superset_app_root param wins over SUPERSET_APP_ROOT env var."""
        env = os.environ.copy()
        env.pop("SUPERSET_CONFIG", None)
        env["SUPERSET_APP_ROOT"] = "/from-env"
        with patch.dict(os.environ, env, clear=True):
            app = create_app(superset_app_root="/from-param")

        assert isinstance(app.wsgi_app, AppRootMiddleware)
        assert app.wsgi_app.app_root == "/from-param"


def _make_initializer(
    secret_key: str,
    debug: bool = False,
    testing: bool = False,
) -> SupersetAppInitializer:
    """Build a ``SupersetAppInitializer`` with a mocked Flask app."""
    mock_app = MagicMock()
    mock_app.debug = debug
    mock_app.config = {"SECRET_KEY": secret_key, "TESTING": testing}
    init = SupersetAppInitializer(mock_app)
    init.config = mock_app.config
    return init


class TestCheckSecretKey:
    """Tests for hardened SECRET_KEY validation (issue #5, CVE-2023-27524)."""

    @patch("superset.initialization.is_test", return_value=False)
    @patch("superset.initialization.logger")
    def test_production_default_key_exits(
        self, mock_logger: MagicMock, mock_is_test: MagicMock
    ) -> None:
        """Production with the upstream default key must exit."""
        init = _make_initializer(CHANGE_ME_SECRET_KEY)
        with pytest.raises(SystemExit):
            init.check_secret_key()
        mock_logger.error.assert_called_once()
        assert "insecure SECRET_KEY" in mock_logger.error.call_args[0][0]

    @patch("superset.initialization.is_test", return_value=False)
    @patch("superset.initialization.logger")
    def test_production_weak_key_exits(
        self, mock_logger: MagicMock, mock_is_test: MagicMock
    ) -> None:
        """Production with a well-known weak key like 'secret' must exit."""
        init = _make_initializer("secret")
        with pytest.raises(SystemExit):
            init.check_secret_key()
        mock_logger.error.assert_called_once()

    @patch("superset.initialization.is_test", return_value=False)
    @patch("superset.initialization.logger")
    def test_production_short_key_exits(
        self, mock_logger: MagicMock, mock_is_test: MagicMock
    ) -> None:
        """Production with a key shorter than SECRET_KEY_MIN_LENGTH must exit."""
        short_key = "a" * (SECRET_KEY_MIN_LENGTH - 1)
        init = _make_initializer(short_key)
        with pytest.raises(SystemExit):
            init.check_secret_key()
        mock_logger.error.assert_called_once()
        assert "too short" in mock_logger.error.call_args[0][1]

    @patch("superset.initialization.is_test", return_value=False)
    @patch("superset.initialization.logger")
    def test_production_strong_key_starts_normally(
        self, mock_logger: MagicMock, mock_is_test: MagicMock
    ) -> None:
        """Production with a sufficiently long, non-weak key must not exit."""
        strong_key = "x" * SECRET_KEY_MIN_LENGTH
        init = _make_initializer(strong_key)
        init.check_secret_key()  # should return without error
        mock_logger.error.assert_not_called()

    @patch("superset.initialization.is_test", return_value=False)
    @patch("superset.initialization.logger")
    def test_dev_weak_key_warns_only(
        self, mock_logger: MagicMock, mock_is_test: MagicMock
    ) -> None:
        """Debug mode with a weak key must warn but not exit."""
        init = _make_initializer("secret", debug=True)
        init.check_secret_key()  # should not raise
        mock_logger.warning.assert_called()
        mock_logger.error.assert_not_called()

    @patch("superset.initialization.is_test", return_value=False)
    @patch("superset.initialization.logger")
    def test_structured_log_emitted(
        self, mock_logger: MagicMock, mock_is_test: MagicMock
    ) -> None:
        """A structured log with event_type must be emitted for weak keys."""
        init = _make_initializer("secret", debug=True)
        init.check_secret_key()
        # Find the structured warning call with extra kwarg
        structured_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.kwargs.get("extra", {}).get("event_type") == "insecure_secret_key"
        ]
        assert len(structured_calls) == 1
        extra = structured_calls[0].kwargs["extra"]
        assert extra["cve"] == "CVE-2023-27524"
        assert extra["reason"] == "matches a known weak key"

    @patch("superset.initialization.is_test", return_value=False)
    @patch("superset.initialization.logger")
    def test_remediation_hint_in_error(
        self, mock_logger: MagicMock, mock_is_test: MagicMock
    ) -> None:
        """The error message must include the openssl remediation command."""
        init = _make_initializer("secret")
        with pytest.raises(SystemExit):
            init.check_secret_key()
        error_msg = " ".join(str(a) for a in mock_logger.error.call_args[0])
        assert "openssl rand -hex 32" in error_msg
