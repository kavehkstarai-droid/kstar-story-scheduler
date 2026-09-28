"""Read-only HeyGen API wallet feasibility check; never creates or publishes media.

Use --check for local environment validation only, or --fetch for one account
read. A successful wallet observation is not permission or readiness to generate.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request

# Official schema verified 2026-09-28:
# https://developers.heygen.com/user-profile
# https://developers.heygen.com/reference/get-current-user
ENDPOINT = "https://api.heygen.com/v3/users/me"
TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 65536
KNOWN_BILLING_TYPES = {"wallet", "subscription", "usage_based"}
KNOWN_CURRENCIES = {"usd", "credits"}
SAFE_ERROR_CODES = {
    "secret_missing_or_invalid", "redirect_rejected", "request_failed",
    "response_http_error", "response_too_large", "response_not_json",
    "response_api_error", "response_schema_invalid", "interrupted",
    "unexpected_failure",
}


class PreflightError(Exception):
    def __init__(self, code):
        # Never allow response content, exception messages, URLs, or credentials
        # to become a printable exception even if a caller passes the wrong value.
        self.code = code if code in SAFE_ERROR_CODES else "unexpected_failure"
        super().__init__(self.code)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "BLOCKED: Choose --check or --fetch; provide the secret only through the environment.\n")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise PreflightError("redirect_rejected")


def no_redirect_open(request, *, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def checked_secret(value):
    # HeyGen documents an opaque key, not a stable prefix/length format. Check
    # header-safe syntax here; authentication is verified only by the live GET.
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(not 33 <= ord(character) <= 126 for character in value)):
        raise PreflightError("secret_missing_or_invalid")
    return value


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PreflightError("response_schema_invalid")
        value[key] = item
    return value


def reject_nonfinite(value):
    raise PreflightError("response_schema_invalid")


def fetch_account(secret, *, opener=no_redirect_open):
    secret = checked_secret(secret)
    request = urllib.request.Request(
        ENDPOINT, method="GET",
        headers={"X-Api-Key": secret, "Accept": "application/json", "Accept-Encoding": "identity"},
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            if response.geturl() != ENDPOINT:
                raise PreflightError("redirect_rejected")
            if response.getcode() != 200:
                raise PreflightError("response_http_error")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise PreflightError("response_not_json")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise PreflightError("response_too_large")
        return json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=reject_nonfinite)
    except PreflightError:
        raise
    except Exception:
        # Do not print urllib exceptions, bodies, headers, profile data, or URLs.
        raise PreflightError("request_failed") from None


def nonnegative_finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (OverflowError, TypeError, ValueError):
        return False


def assess_account(payload):
    """Return only explicitly whitelisted billing fields and a conservative result."""
    if not isinstance(payload, dict):
        raise PreflightError("response_schema_invalid")
    if payload.get("error") is not None:
        raise PreflightError("response_api_error")
    data = payload.get("data")
    if not isinstance(data, dict) or "billing_type" not in data:
        raise PreflightError("response_schema_invalid")
    billing_type = data["billing_type"]
    if billing_type is not None and (not isinstance(billing_type, str) or billing_type not in KNOWN_BILLING_TYPES):
        raise PreflightError("response_schema_invalid")
    wallet = data.get("wallet")
    balance = currency = auto_reload = None
    if wallet is not None:
        if not isinstance(wallet, dict) or "remaining_balance" not in wallet or "currency" not in wallet:
            raise PreflightError("response_schema_invalid")
        balance, currency = wallet["remaining_balance"], wallet["currency"]
        if balance is not None and not nonnegative_finite_number(balance):
            raise PreflightError("response_schema_invalid")
        if not isinstance(currency, str) or currency not in KNOWN_CURRENCIES:
            raise PreflightError("response_schema_invalid")
        reload_details = wallet.get("auto_reload")
        if reload_details is not None:
            if not isinstance(reload_details, dict):
                raise PreflightError("response_schema_invalid")
            auto_reload = reload_details.get("enabled")
            if auto_reload is not None and type(auto_reload) is not bool:
                raise PreflightError("response_schema_invalid")
    if billing_type != "wallet":
        reason = "api_wallet_billing_not_confirmed"
    elif wallet is None:
        reason = "api_wallet_unavailable"
    elif auto_reload is not False:
        reason = "auto_reload_enabled" if auto_reload is True else "auto_reload_unknown"
    elif balance is None:
        reason = "api_wallet_balance_unknown"
    elif balance <= 0:
        reason = "no_api_wallet_funds"
    else:
        reason = "positive_api_wallet_observed_generation_not_assessed"
    observed = reason == "positive_api_wallet_observed_generation_not_assessed"
    return {
        "mode": "read_only_feasibility", "status": "observed" if observed else "blocked",
        "billing_type": billing_type,
        "wallet": {"remaining_balance": balance, "currency": currency, "auto_reload_enabled": auto_reload},
        "reason": reason, "generation_ready": False,
    }, observed


def preflight(secret, *, opener=no_redirect_open):
    return assess_account(fetch_account(secret, opener=opener))


def public_diagnostics(snapshot):
    """Public Actions logs must not expose account type, currency, or balance."""
    wallet = snapshot["wallet"]
    balance = wallet["remaining_balance"]
    return {
        "mode": "read_only_feasibility", "status": snapshot["status"],
        "api_funds_present": bool(snapshot["billing_type"] == "wallet" and balance is not None and balance > 0),
        "auto_reload_off": wallet["auto_reload_enabled"] is False,
        "reason": snapshot["reason"], "generation_ready": False,
    }


def main(argv=None, *, env=None, opener=no_redirect_open):
    parser = SafeParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Validate environment syntax only; never use the network")
    mode.add_argument("--fetch", action="store_true", help="Perform one read-only API wallet request")
    args = parser.parse_args(argv)
    try:
        secret = checked_secret((os.environ if env is None else env).get("HEYGEN_API_KEY"))
        if args.check:
            result = {"mode": "local_configuration_only", "secret_syntax_valid": True,
                      "authentication_checked": False, "network_checked": False, "generation_ready": False}
            code = 0
        else:
            snapshot, observed = preflight(secret, opener=opener)
            result = public_diagnostics(snapshot)
            code = 0 if observed else 1
    except PreflightError as error:
        result = {"status": "blocked", "reason": error.code, "generation_ready": False}
        code = 1
    except (Exception, KeyboardInterrupt):
        result = {"status": "blocked", "reason": "unexpected_failure", "generation_ready": False}
        code = 1
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
