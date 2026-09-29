"""Small client for eBay's official Browse API.

Only the two calls this project needs are implemented:

  * ``search``   -> GET /buy/browse/v1/item_summary/search  (active listings)
  * ``get_item`` -> GET /buy/browse/v1/item/{item_id}         (full listing, incl.
                    trading-card condition descriptors such as grader and grade)

Authentication uses an OAuth *application* token (client credentials grant).
eBay caps how many tokens an app may mint per day, so the token is cached in
memory and only refreshed shortly before it expires.

Environment variables (a .env file works too -- see README.md):

  EBAY_CLIENT_ID       required  App ID (Client ID) of your eBay keyset
  EBAY_CLIENT_SECRET   required  Cert ID (Client Secret) of the same keyset
  EBAY_MARKETPLACE_ID  optional  default EBAY_US
  EBAY_ENV             optional  "production" (default) or "sandbox"

eBay docs:
  https://developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html
  https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
  https://developer.ebay.com/api-docs/buy/browse/resources/item/methods/getItem
"""
from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Callable, Iterable
from urllib.parse import quote

import requests

PRODUCTION_HOST = "https://api.ebay.com"
SANDBOX_HOST    = "https://api.sandbox.ebay.com"
TOKEN_PATH      = "/identity/v1/oauth2/token"
BROWSE_PATH     = "/buy/browse/v1"

# Scope for public Browse API data.  eBay uses this same URL in the sandbox.
BROWSE_SCOPE = "https://api.ebay.com/oauth/api_scope"
DEFAULT_MARKETPLACE_ID = "EBAY_US"

# Category 183454 = "CCG Individual Cards" (single collectible-card-game cards).
CCG_INDIVIDUAL_CARDS_CATEGORY = "183454"

# In trading-card categories eBay only allows two item conditions:
# 2750 means the card is graded and 4000 means it is ungraded.
# https://developer.ebay.com/api-docs/sell/static/metadata/condition-id-values.html
GRADED_CONDITION_ID   = "2750"
UNGRADED_CONDITION_ID = "4000"

SEARCH_MAX_LIMIT  = 200    # documented maximum page size for item_summary/search
SEARCH_MAX_OFFSET = 9_999  # documented maximum offset
QUERY_MAX_LENGTH  = 100    # documented maximum length of the q parameter

SETUP_HINT = "See the 'eBay API setup' section of README.md."

_MARKETPLACE_COUNTRY_RE = re.compile(r"^EBAY_(?:[A-Z]+_)?([A-Z]{2})$")


class EbayError(RuntimeError):
    """Base class for eBay API problems."""


class EbayCredentialsError(EbayError):
    """Credentials are missing or the environment is misconfigured."""


class EbayApiError(EbayError):
    """eBay answered with an error, or could not be reached after retries."""

    def __init__(self, message: str, status_code: int | None = None,
                 errors: list[dict] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []

    @classmethod
    def from_response(cls, resp: requests.Response, context: str,
                      hint: str = "") -> "EbayApiError":
        """Build an error from eBay's standard ``{"errors": [...]}`` body."""
        errors: list[dict] = []
        detail = ""
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            if isinstance(body.get("errors"), list):
                errors = [e for e in body["errors"] if isinstance(e, dict)]
                detail = "; ".join(
                    str(e.get("longMessage") or e.get("message") or "") for e in errors
                ).strip("; ")
            elif body.get("error"):   # OAuth endpoint style: {"error", "error_description"}
                detail = f"{body['error']}: {body.get('error_description', '')}".strip(": ")
        message = f"{context} failed (HTTP {resp.status_code})"
        if detail:
            message += f": {detail}"
        if hint:
            message += f" -- {hint}"
        return cls(message, status_code=resp.status_code, errors=errors)


def marketplace_country(marketplace_id: str) -> str | None:
    """Two-letter country of a marketplace ID, e.g. EBAY_US -> "US", EBAY_GB -> "GB"."""
    m = _MARKETPLACE_COUNTRY_RE.match(marketplace_id or "")
    return m.group(1) if m else None


class EbayClient:
    """Browse API client with token caching, timeouts and retries.

    ``session``, ``clock`` and ``sleep`` can be injected so the client can be
    tested without network access or real waiting.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        marketplace_id: str = DEFAULT_MARKETPLACE_ID,
        sandbox: bool = False,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        token_refresh_margin: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not client_id or not client_secret:
            raise EbayCredentialsError(
                "eBay API credentials are missing: set EBAY_CLIENT_ID and "
                "EBAY_CLIENT_SECRET. " + SETUP_HINT
            )
        self.client_id      = client_id
        self.client_secret  = client_secret
        self.marketplace_id = marketplace_id
        self.sandbox        = sandbox
        self.host           = SANDBOX_HOST if sandbox else PRODUCTION_HOST
        self.session        = session or requests.Session()
        self.timeout        = timeout
        self.max_retries    = max_retries
        self.backoff_seconds      = backoff_seconds
        self.token_refresh_margin = token_refresh_margin
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires_at  = 0.0

    @classmethod
    def from_env(cls, **kwargs: Any) -> "EbayClient":
        """Create a client from EBAY_* environment variables (see module docstring)."""
        client_id     = os.getenv("EBAY_CLIENT_ID", "").strip()
        client_secret = os.getenv("EBAY_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise EbayCredentialsError(
                "eBay API credentials are missing: set EBAY_CLIENT_ID and "
                "EBAY_CLIENT_SECRET as environment variables or in a .env file. "
                + SETUP_HINT
            )
        env = os.getenv("EBAY_ENV", "production").strip().lower() or "production"
        if env not in ("production", "sandbox"):
            raise EbayCredentialsError(
                f"EBAY_ENV must be 'production' or 'sandbox', not {env!r}. " + SETUP_HINT
            )
        marketplace = os.getenv("EBAY_MARKETPLACE_ID", "").strip() or DEFAULT_MARKETPLACE_ID
        return cls(client_id, client_secret, marketplace_id=marketplace,
                   sandbox=(env == "sandbox"), **kwargs)

    # ── public API ────────────────────────────────────────────────────────────

    def search(
        self,
        q: str,
        *,
        category_ids: Iterable[str] | None = None,
        filters: Iterable[str] | None = None,
        sort: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Call item_summary/search and return the decoded JSON response.

        filters are Browse API filter expressions such as
        ``"buyingOptions:{FIXED_PRICE}"``; they are joined with commas.
        sort is an API sort value ("price", "newlyListed", ...) or None for
        eBay's default "Best Match" order.  q is truncated to eBay's
        100-character limit.
        """
        if not 1 <= limit <= SEARCH_MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {SEARCH_MAX_LIMIT}")
        if not 0 <= offset <= SEARCH_MAX_OFFSET:
            raise ValueError(f"offset must be between 0 and {SEARCH_MAX_OFFSET}")

        params: dict[str, Any] = {"q": q.strip()[:QUERY_MAX_LENGTH], "limit": limit}
        if offset:
            params["offset"] = offset
        cats = [str(c) for c in (category_ids or []) if str(c).strip()]
        if cats:
            params["category_ids"] = ",".join(cats)
        flt = [f for f in (filters or []) if f]
        if flt:
            params["filter"] = ",".join(flt)
        if sort:
            params["sort"] = sort
        return self._get("/item_summary/search", params, context="eBay search")

    def get_item(self, item_id: str) -> dict:
        """Call getItem for one RESTful item ID (e.g. ``v1|123456789012|0``)."""
        path = "/item/" + quote(item_id, safe="")
        return self._get(path, None, context=f"eBay getItem {item_id}")

    # ── internals ─────────────────────────────────────────────────────────────

    def _access_token(self) -> str:
        """Return a valid application token, minting a new one only when needed."""
        now = self._clock()
        if self._token and now < self._token_expires_at:
            return self._token

        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = self._send(
            "POST",
            self.host + TOKEN_PATH,
            headers={
                "Content-Type":  "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            data={"grant_type": "client_credentials", "scope": BROWSE_SCOPE},
        )
        if resp.status_code != 200:
            hint = ""
            if resp.status_code in (400, 401):
                hint = ("check EBAY_CLIENT_ID / EBAY_CLIENT_SECRET, and that EBAY_ENV "
                        "matches the keyset (production vs sandbox). " + SETUP_HINT)
            raise EbayApiError.from_response(resp, "eBay OAuth token request", hint=hint)
        try:
            payload = resp.json()
            token = payload["access_token"]
            expires_in = float(payload.get("expires_in", 7200))
        except (ValueError, KeyError, TypeError) as exc:
            raise EbayApiError("eBay OAuth token response was not understood") from exc

        self._token = token
        # Refresh a little early so a request never goes out with an expired token.
        self._token_expires_at = now + max(0.0, expires_in - self.token_refresh_margin)
        return token

    def _get(self, path: str, params: dict | None, *, context: str) -> dict:
        url = self.host + BROWSE_PATH + path
        resp = None
        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._access_token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
                "Accept": "application/json",
            }
            resp = self._send("GET", url, params=params, headers=headers)
            if resp.status_code == 401 and attempt == 0:
                # Token rejected (revoked or expired early): mint a new one once.
                self._token = None
                continue
            break

        if resp.status_code == 204:
            return {}
        if resp.status_code != 200:
            raise EbayApiError.from_response(resp, context)
        try:
            data = resp.json()
        except ValueError as exc:
            raise EbayApiError(f"{context} returned invalid JSON") from exc
        return data if isinstance(data, dict) else {}

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send a request, retrying network errors, HTTP 429 and 5xx with backoff.

        After ``max_retries`` retries the last response is returned (or the
        network error is raised as EbayApiError) for the caller to handle.
        """
        attempt = 0
        while True:
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise EbayApiError(f"could not reach eBay ({exc})") from exc
                delay = self._retry_delay(attempt, None)
            else:
                retryable = resp.status_code == 429 or resp.status_code >= 500
                if not retryable or attempt >= self.max_retries:
                    return resp
                delay = self._retry_delay(attempt, resp.headers.get("Retry-After"))
            self._sleep(delay)
            attempt += 1

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        """Seconds to wait: the server's Retry-After if given, else exponential backoff."""
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return self.backoff_seconds * (2 ** attempt)


_default_client: EbayClient | None = None


def get_default_client() -> EbayClient:
    """Shared client built from the environment on first use.

    Raises EbayCredentialsError (with a pointer to the README) when the
    credentials are not set.
    """
    global _default_client
    if _default_client is None:
        _default_client = EbayClient.from_env()
    return _default_client
