import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from werkzeug.datastructures import MultiDict

import app as warehouse_app


class PasswordSecurityTests(unittest.TestCase):
    def test_password_policy_requires_all_security_classes(self):
        invalid = (
            "Short1!",
            "NOLOWERCASE123!",
            "nouppercase123!",
            "NoNumberHere!",
            "NoSpecial12345",
        )
        for password in invalid:
            with self.subTest(password=password):
                self.assertIsNotNone(
                    warehouse_app.password_validation_error(password)
                )
        self.assertIsNone(
            warehouse_app.password_validation_error("Bezpieczne123!")
        )

    def test_admin_creates_user_with_temporary_firebase_password_only(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (77,)
        connection = MagicMock()
        connection.cursor.return_value = cursor
        firebase_user = SimpleNamespace(uid="firebase-77")
        form = MultiDict(
            [
                ("first_name", "Anna"),
                ("last_name", "Testowa"),
                ("email", "anna@example.com"),
                ("phone", "500600700"),
                ("role", "sales"),
                ("temporary_password", "Bezpieczne123!"),
                ("active", "on"),
            ]
        )
        with warehouse_app.app.test_request_context(
            "/add_user", method="POST", data=form
        ):
            warehouse_app.session["user"] = "admin@example.com"
            warehouse_app.session["role"] = "admin"
            with (
                patch.object(warehouse_app, "FIREBASE_ADMIN_READY", True),
                patch.object(
                    warehouse_app.auth,
                    "get_user_by_email",
                    side_effect=warehouse_app.auth.UserNotFoundError("missing"),
                ),
                patch.object(
                    warehouse_app.auth,
                    "create_user",
                    return_value=firebase_user,
                ) as create_user,
                patch.object(warehouse_app, "db", return_value=connection),
            ):
                response = warehouse_app.add_user()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            create_user.call_args.kwargs["password"], "Bezpieczne123!"
        )
        sql_calls = " ".join(call.args[0] for call in cursor.execute.call_args_list)
        sql_parameters = repr(
            [call.args[1] for call in cursor.execute.call_args_list if len(call.args) > 1]
        )
        self.assertIn("must_change_password", sql_calls)
        self.assertNotIn("Bezpieczne123!", sql_parameters)

    def test_first_login_redirects_to_required_password_change(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "sales",
            "active",
            False,
            False,
            True,
            77,
            False,
        )
        connection = MagicMock()
        connection.cursor.return_value = cursor
        firebase_user = SimpleNamespace(disabled=False, display_name="Anna")
        with warehouse_app.app.test_request_context(
            "/auth/session", method="POST", json={"idToken": "token"}
        ):
            with (
                patch.object(warehouse_app, "FIREBASE_ADMIN_READY", True),
                patch.object(warehouse_app, "ensure_db_initialized"),
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(
                    warehouse_app.auth,
                    "verify_id_token",
                    return_value={"uid": "uid-77", "email": "anna@example.com"},
                ),
                patch.object(
                    warehouse_app.auth, "get_user", return_value=firebase_user
                ),
                patch.object(
                    warehouse_app.auth,
                    "create_session_cookie",
                    return_value="cookie",
                ),
            ):
                response = warehouse_app.create_session()
        result = response.get_json()
        self.assertTrue(result["mustChangePassword"])
        self.assertIn("/change-password", result["redirect"])

    def test_required_password_change_blocks_application_routes(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "sales",
            "active",
            False,
            False,
            True,
            False,
        )
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with warehouse_app.app.test_request_context(
            "/", headers={"Cookie": "firebase_session=cookie"}
        ):
            with (
                patch.object(warehouse_app, "FIREBASE_ADMIN_READY", True),
                patch.object(
                    warehouse_app.auth,
                    "verify_session_cookie",
                    return_value={"uid": "uid-77", "email": "anna@example.com"},
                ),
                patch.object(warehouse_app, "ensure_db_initialized"),
                patch.object(warehouse_app, "db", return_value=connection),
            ):
                response = warehouse_app.require_login_for_private_app()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/change-password?required=1", response.location)

    def test_user_changes_password_without_database_password_storage(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (77, "anna@example.com")
        connection = MagicMock()
        connection.cursor.return_value = cursor
        form = MultiDict(
            [
                ("current_password", "Tymczasowe123!"),
                ("new_password", "WlasneHaslo123!"),
                ("confirm_password", "WlasneHaslo123!"),
            ]
        )
        with warehouse_app.app.test_request_context(
            "/auth/change-password", method="POST", data=form
        ):
            warehouse_app.session.update(
                user="anna@example.com",
                uid="uid-77",
                role="sales",
                must_change_password=True,
            )
            with (
                patch.object(
                    warehouse_app,
                    "firebase_password_sign_in",
                    side_effect=[
                        {"idToken": "old-token"},
                        {"idToken": "new-token"},
                    ],
                ),
                patch.object(warehouse_app.auth, "update_user") as update_user,
                patch.object(
                    warehouse_app.auth,
                    "create_session_cookie",
                    return_value="new-cookie",
                ),
                patch.object(warehouse_app, "db", return_value=connection),
            ):
                response = warehouse_app.change_password()
            must_change_after = warehouse_app.session["must_change_password"]
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            update_user.call_args.kwargs["password"], "WlasneHaslo123!"
        )
        sql = " ".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertNotIn(" password=", sql.lower())
        self.assertFalse(must_change_after)

    def test_fifth_failed_login_temporarily_locks_account(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (77, "uid-77", "active", 4, False)
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with warehouse_app.app.test_request_context(
            "/auth/password-login",
            method="POST",
            json={"email": "anna@example.com", "password": "wrong"},
        ):
            with (
                patch.object(warehouse_app, "ensure_db_initialized"),
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(
                    warehouse_app,
                    "firebase_password_sign_in",
                    side_effect=ValueError("invalid"),
                ),
                patch.object(warehouse_app, "LOGIN_LOCK_MODE", "temporary"),
            ):
                response, status = warehouse_app.password_login()
        self.assertEqual(status, 423)
        self.assertIn("zablokowane", response.get_json()["error"])
        sql = " ".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("locked_until", sql)
        connection.commit.assert_called_once()

    def test_admin_sets_temporary_password_and_forces_change(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("uid-77", "anna@example.com")
        connection = MagicMock()
        connection.cursor.return_value = cursor
        form = MultiDict([("temporary_password", "NoweTymczasowe123!")])
        with warehouse_app.app.test_request_context(
            "/users/77/temporary-password", method="POST", data=form
        ):
            warehouse_app.session.update(
                user="admin@example.com", role="admin", uid="admin-uid"
            )
            with (
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(warehouse_app.auth, "update_user") as update_user,
                patch.object(warehouse_app.auth, "revoke_refresh_tokens"),
            ):
                response = warehouse_app.set_temporary_password(77)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            update_user.call_args.kwargs["password"], "NoweTymczasowe123!"
        )
        update_sql = " ".join(
            call.args[0] for call in cursor.execute.call_args_list
        )
        self.assertIn("must_change_password=TRUE", update_sql)

    def test_admin_unlocks_account_and_resets_attempt_counter(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "uid-77",
            "anna@example.com",
            "blocked",
        )
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with warehouse_app.app.test_request_context(
            "/users/77/unlock", method="POST"
        ):
            warehouse_app.session.update(
                user="admin@example.com", role="admin", uid="admin-uid"
            )
            with (
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(warehouse_app.auth, "update_user") as update_user,
            ):
                response = warehouse_app.unlock_user(77)
        self.assertEqual(response.status_code, 302)
        update_user.assert_called_once_with("uid-77", disabled=False)
        sql = " ".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("failed_login_attempts=0", sql)

    def test_forgot_password_sends_generic_one_time_reset_request(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (77, "active")
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with warehouse_app.app.test_request_context(
            "/auth/forgot-password",
            method="POST",
            json={"email": "anna@example.com"},
        ):
            with (
                patch.object(warehouse_app, "ensure_db_initialized"),
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(
                    warehouse_app, "send_firebase_password_reset"
                ) as send_reset,
            ):
                response = warehouse_app.forgot_password()
        self.assertTrue(response.get_json()["ok"])
        send_reset.assert_called_once_with("anna@example.com")
        connection.commit.assert_called_once()

    def test_admin_deactivates_account_in_firebase_and_database(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "uid-77",
            "anna@example.com",
            "sales",
            "active",
        )
        connection = MagicMock()
        connection.cursor.return_value = cursor
        with warehouse_app.app.test_request_context(
            "/users/77/toggle-active", method="POST"
        ):
            warehouse_app.session.update(
                user="admin@example.com", role="admin", uid="admin-uid"
            )
            with (
                patch.object(warehouse_app, "db", return_value=connection),
                patch.object(warehouse_app.auth, "update_user") as update_user,
            ):
                response = warehouse_app.toggle_user_active(77)
        self.assertEqual(response.status_code, 302)
        update_user.assert_called_once_with("uid-77", disabled=True)
        sql = " ".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("status=%s", sql)

    def test_users_table_never_defines_plaintext_password_column(self):
        source = Path(warehouse_app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("password TEXT", source)
        self.assertNotIn("password VARCHAR", source)


if __name__ == "__main__":
    unittest.main()
