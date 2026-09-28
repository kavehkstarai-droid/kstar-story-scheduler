"""Fake HTTP only; these tests never contact HeyGen or read real credentials."""
import contextlib
from email.message import Message
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import heygen_preflight as h

KEY = "synthetic-fixture-key-do-not-log"
PRIVATE = "private-profile-must-not-be-printed"


def wallet_payload(balance=42.5, currency="usd", auto_reload=False):
    return {"data": {"username": PRIVATE, "email": PRIVATE, "first_name": PRIVATE,
                     "billing_type": "wallet", "wallet": {
                         "remaining_balance": balance, "currency": currency,
                         "auto_reload": {"enabled": auto_reload}},
                     "subscription": None, "usage_based": None}}


class FakeResponse(io.BytesIO):
    def __init__(self, payload=None, *, raw=None, status=200, final_url=h.ENDPOINT,
                 content_type="application/json"):
        super().__init__(raw if raw is not None else json.dumps(payload).encode())
        self.status, self.final_url = status, final_url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.read_sizes = []

    def getcode(self):
        return self.status

    def geturl(self):
        return self.final_url

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class FakeHTTP:
    def __init__(self, payload=None, **response_options):
        self.payload = wallet_payload() if payload is None else payload
        self.options = response_options
        self.calls = []
        self.response = None

    def __call__(self, request, **kwargs):
        self.calls.append((request, kwargs))
        self.response = FakeResponse(self.payload, **self.options)
        return self.response


class HeyGenPreflightTests(unittest.TestCase):
    def run_cli(self, http=None, args=None, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = h.main(args or ["--fetch"], env={"HEYGEN_API_KEY": KEY} if env is None else env,
                          opener=http or FakeHTTP())
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn(KEY, out.getvalue())
        self.assertNotIn(PRIVATE, out.getvalue())
        self.assertNotIn("https://", out.getvalue())
        for field in ('"billing_type"', '"currency"', '"remaining_balance"', '"wallet"', '"subscription"'):
            self.assertNotIn(field, out.getvalue())
        return code, json.loads(out.getvalue())

    def test_check_only_validates_environment_without_network_or_auth_claim(self):
        http = FakeHTTP()
        code, result = self.run_cli(http, ["--check"])
        self.assertEqual(code, 0)
        self.assertEqual(http.calls, [])
        self.assertFalse(result["network_checked"])
        self.assertFalse(result["authentication_checked"])
        self.assertFalse(result["generation_ready"])

    def test_missing_invalid_or_header_unsafe_secret_never_uses_network(self):
        for value in (None, "", " ", "bad\nheader", "bad\rheader", "bad\x00header", "غیرلاتین", "x" * 4097, False):
            http = FakeHTTP()
            with self.subTest():
                code, result = self.run_cli(http, env={"HEYGEN_API_KEY": value})
                self.assertEqual(code, 1)
                self.assertEqual(result["reason"], "secret_missing_or_invalid")
                self.assertEqual(http.calls, [])

    def test_one_get_uses_header_only_and_twenty_second_timeout(self):
        http = FakeHTTP()
        self.run_cli(http)
        self.assertEqual(len(http.calls), 1)
        request, options = http.calls[0]
        self.assertEqual(request.full_url, h.ENDPOINT)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("X-api-key"), KEY)
        self.assertEqual(options, {"timeout": 20})
        self.assertEqual(http.response.read_sizes, [h.MAX_RESPONSE_BYTES + 1])

    def test_positive_wallet_is_observation_never_generation_ready(self):
        for currency in ("usd", "credits"):
            with self.subTest(currency=currency):
                code, result = self.run_cli(FakeHTTP(wallet_payload(currency=currency)))
                self.assertEqual(code, 0)
                self.assertTrue(result["api_funds_present"])
                self.assertTrue(result["auto_reload_off"])
                self.assertEqual(result["status"], "observed")
                self.assertFalse(result["generation_ready"])
                self.assertNotIn("42.5", json.dumps(result))

    def test_subscription_with_null_wallet_does_not_count_web_credits(self):
        payload = {"data": {"billing_type": "subscription", "wallet": None,
                            "subscription": {"credits": {"premium_credits": {"remaining": 999999}}}}}
        code, result = self.run_cli(FakeHTTP(payload))
        self.assertEqual(code, 1)
        self.assertFalse(result["api_funds_present"])
        self.assertEqual(result["reason"], "api_wallet_billing_not_confirmed")

    def test_validated_financial_fields_are_available_internally_only(self):
        snapshot, observed = h.preflight(KEY, opener=FakeHTTP(wallet_payload(balance=123.456)))
        self.assertTrue(observed)
        self.assertEqual(snapshot["billing_type"], "wallet")
        self.assertEqual(snapshot["wallet"], {"remaining_balance": 123.456, "currency": "usd",
                                            "auto_reload_enabled": False})
        code, diagnostic = self.run_cli(FakeHTTP(wallet_payload(balance=123.456)))
        self.assertEqual(code, 0)
        self.assertNotIn("123.456", json.dumps(diagnostic))

    def test_usage_based_and_unknown_billing_are_blocked(self):
        for billing_type in ("usage_based", None):
            payload = wallet_payload()
            payload["data"]["billing_type"] = billing_type
            with self.subTest():
                code, result = self.run_cli(FakeHTTP(payload))
                self.assertEqual(code, 1)
                self.assertFalse(result["generation_ready"])

    def test_auto_reload_true_null_missing_and_unknown_enabled_are_blocked(self):
        for settings in ({"enabled": True}, None, {}, {"enabled": None}):
            payload = wallet_payload()
            payload["data"]["wallet"]["auto_reload"] = settings
            with self.subTest():
                code, result = self.run_cli(FakeHTTP(payload))
                self.assertEqual(code, 1)
                self.assertIn(result["reason"], {"auto_reload_enabled", "auto_reload_unknown"})
        payload = wallet_payload()
        del payload["data"]["wallet"]["auto_reload"]
        self.assertEqual(self.run_cli(FakeHTTP(payload))[0], 1)

    def test_zero_null_or_missing_wallet_funds_are_blocked(self):
        for balance in (0, 0.0, None):
            with self.subTest():
                code, result = self.run_cli(FakeHTTP(wallet_payload(balance=balance)))
                self.assertEqual(code, 1)
                self.assertIn(result["reason"], {"no_api_wallet_funds", "api_wallet_balance_unknown"})
        payload = wallet_payload()
        payload["data"]["wallet"] = None
        self.assertEqual(self.run_cli(FakeHTTP(payload))[1]["reason"], "api_wallet_unavailable")

    def test_malformed_schema_unsafe_fields_and_nonfinite_balances_are_rejected(self):
        variants = [[], {}, {"data": []}, {"data": {"billing_type": KEY}},
                    wallet_payload(currency=KEY), wallet_payload(auto_reload="false")]
        for value in (-1, True, "42.5", [], float("inf"), float("nan")):
            variants.append(wallet_payload(balance=value))
        missing = wallet_payload()
        del missing["data"]["wallet"]["remaining_balance"]
        variants.append(missing)
        for payload in variants:
            with self.subTest():
                code, result = self.run_cli(FakeHTTP(payload))
                self.assertEqual(code, 1)
                self.assertEqual(result["reason"], "response_schema_invalid")

    def test_api_error_messages_and_raw_profiles_never_reach_output(self):
        payload = wallet_payload()
        payload["error"] = {"message": KEY + PRIVATE, "doc_url": "https://private.invalid/"}
        code, result = self.run_cli(FakeHTTP(payload))
        self.assertEqual(code, 1)
        self.assertEqual(result["reason"], "response_api_error")

    def test_network_failure_suppresses_secret_url_and_error_details(self):
        def failed(*args, **kwargs):
            raise RuntimeError("https://private.invalid/" + KEY + PRIVATE)
        code, result = self.run_cli(failed)
        self.assertEqual((code, result["reason"]), (1, "request_failed"))

    def test_redirect_handler_and_changed_response_location_are_rejected(self):
        with self.assertRaises(h.PreflightError) as caught:
            h.NoRedirect().redirect_request(None, None, 302, KEY, {}, "https://private.invalid/" + KEY)
        self.assertEqual(str(caught.exception), "redirect_rejected")
        http = FakeHTTP(final_url="https://private.invalid/" + KEY)
        code, result = self.run_cli(http)
        self.assertEqual((code, result["reason"]), (1, "redirect_rejected"))
        self.assertEqual(http.response.read_sizes, [])

    def test_oversized_response_is_bounded_and_safely_rejected(self):
        http = FakeHTTP(raw=b"x" * (h.MAX_RESPONSE_BYTES + 2000))
        code, result = self.run_cli(http)
        self.assertEqual((code, result["reason"]), (1, "response_too_large"))
        self.assertEqual(http.response.read_sizes, [h.MAX_RESPONSE_BYTES + 1])

    def test_http_content_type_invalid_json_and_duplicate_keys_are_rejected(self):
        for options, reason in (({"status": 401}, "response_http_error"),
                                ({"content_type": "text/html"}, "response_not_json"),
                                ({"raw": KEY.encode()}, "request_failed"),
                                ({"raw": b'{"data":{},"data":{}}'}, "response_schema_invalid")):
            with self.subTest():
                code, result = self.run_cli(FakeHTTP(**options))
                self.assertEqual((code, result["reason"]), (1, reason))

    def test_cli_errors_do_not_echo_misplaced_secret(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            h.main(["--fetch", "--key", KEY], env={}, opener=FakeHTTP())
        self.assertNotIn(KEY, err.getvalue())


if __name__ == "__main__":
    unittest.main()
