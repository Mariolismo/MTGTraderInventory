"""CardTrader HTTP client (export, marketplace, bulk_update + jobs)."""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cardtrader_inventory.config import API_BASE_URL, PricingPolicy
from cardtrader_inventory.models import Listing, MarketOffer
from cardtrader_inventory.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# CardTrader documents GET /jobs/:uuid at max 1 request/second.
_JOB_POLL_MIN_INTERVAL_S = 1.05


def _decode_http_body(raw: bytes, headers: Any) -> str:
    """Decode response bytes; honor Content-Encoding: gzip when present."""
    encoding = ""
    if headers is not None:
        encoding = str(headers.get("Content-Encoding") or "").strip().lower()
    if encoding == "gzip" and raw:
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


class CardTraderError(RuntimeError):
    """API or payload error from CardTrader."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class CardTraderClient:
    """Authenticated CardTrader API client with marketplace rate limiting."""

    def __init__(
        self,
        token: str,
        policy: PricingPolicy,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._token = token
        self._policy = policy
        self._limiter = limiter or RateLimiter(policy.marketplace_rps)
        self._last_job_poll_at = 0.0

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
        rate_limit: bool = True,
    ) -> Any:
        url = f"{API_BASE_URL}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "cardtrader-inventory/0.1",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=headers,
        )
        timeout = timeout if timeout is not None else self._policy.request_timeout_s

        raw, status = self._send(req, timeout=timeout, rate_limit=rate_limit)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CardTraderError(
                f"Invalid JSON for {method} {path}",
                status=status,
                body=raw[:500],
            ) from exc

    def _send(
        self,
        req: urllib.request.Request,
        *,
        timeout: float,
        rate_limit: bool,
        allow_429_retry: bool = True,
    ) -> tuple[str, int]:
        if rate_limit:
            self._limiter.acquire()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (
                    _decode_http_body(resp.read(), resp.headers),
                    resp.status,
                )
        except urllib.error.HTTPError as exc:
            raw = _decode_http_body(exc.read(), exc.headers)
            if exc.code == 429 and rate_limit and allow_429_retry:
                logger.warning("HTTP 429 from CardTrader; sleeping 1s and retrying once")
                time.sleep(1.0)
                return self._send(
                    req,
                    timeout=timeout,
                    rate_limit=rate_limit,
                    allow_429_retry=False,
                )
            raise CardTraderError(
                f"HTTP {exc.code} for {req.get_method()} {req.full_url}: {raw[:300]}",
                status=exc.code,
                body=raw,
            ) from exc
        except urllib.error.URLError as exc:
            raise CardTraderError(
                f"Network error for {req.get_method()} {req.full_url}: {exc.reason}"
            ) from exc

    def export_products(self) -> list[Listing]:
        """FETCH: full inventory export. Long timeout; counts toward global budget."""
        logger.info("Fetching /products/export (timeout=%ss)", self._policy.export_timeout_s)
        payload = self._request(
            "GET",
            "/products/export",
            timeout=self._policy.export_timeout_s,
            rate_limit=True,
        )
        return self._parse_export_payload(payload)

    def export_products_for_blueprint(self, blueprint_id: int) -> list[Listing]:
        """Export only your products for one blueprint (fast path for single-item tools)."""
        logger.info("Fetching /products/export?blueprint_id=%s", blueprint_id)
        payload = self._request(
            "GET",
            "/products/export",
            query={"blueprint_id": str(blueprint_id)},
            timeout=self._policy.export_timeout_s,
            rate_limit=True,
        )
        return self._parse_export_payload(payload)

    def list_expansions(self) -> dict[int, str]:
        """Map expansion id → CardTrader set code (uppercase, as returned by CT)."""
        payload = self._request(
            "GET",
            "/expansions",
            timeout=self._policy.request_timeout_s,
            rate_limit=True,
        )
        if not isinstance(payload, list):
            raise CardTraderError(
                f"Unexpected expansions shape: {type(payload).__name__}",
                body=str(payload)[:300],
            )
        out: dict[int, str] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                exp_id = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            code = str(item.get("code") or "").strip().upper()
            if code:
                out[exp_id] = code
        logger.info("Loaded %s expansions with set codes", len(out))
        return out

    def export_blueprints(self, expansion_id: int) -> list[dict[str, Any]]:
        """GET /blueprints/export?expansion_id=… (includes scryfall_id)."""
        payload = self._request(
            "GET",
            "/blueprints/export",
            query={"expansion_id": str(expansion_id)},
            timeout=self._policy.export_timeout_s,
            rate_limit=True,
        )
        if not isinstance(payload, list):
            raise CardTraderError(
                f"Unexpected blueprints/export shape: {type(payload).__name__}",
                body=str(payload)[:300],
            )
        return [item for item in payload if isinstance(item, dict)]

    def blueprint_uid_catalog(
        self,
        expansion_ids: list[int],
    ) -> dict[int, "BlueprintUids"]:
        """Load blueprint UIDs (scryfall / tcgplayer / cardmarket) for expansions."""
        from cardtrader_inventory.scryfall import BlueprintUids

        out: dict[int, BlueprintUids] = {}
        unique = sorted({int(i) for i in expansion_ids})
        logger.info(
            "Loading blueprint UIDs via blueprints/export for %s expansions",
            len(unique),
        )
        for index, exp_id in enumerate(unique, start=1):
            for item in self.export_blueprints(exp_id):
                try:
                    bp_id = int(item["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                scryfall = str(item.get("scryfall_id") or "").strip().lower() or None
                tcg_raw = item.get("tcg_player_id")
                try:
                    tcg_player_id = int(tcg_raw) if tcg_raw is not None else None
                except (TypeError, ValueError):
                    tcg_player_id = None
                cm_ids: list[int] = []
                for raw in item.get("card_market_ids") or []:
                    try:
                        cm_ids.append(int(raw))
                    except (TypeError, ValueError):
                        continue
                out[bp_id] = BlueprintUids(
                    blueprint_id=bp_id,
                    scryfall_id=scryfall,
                    tcg_player_id=tcg_player_id,
                    card_market_ids=cm_ids,
                    scryfall_source="ct" if scryfall else None,
                )
            if index == len(unique) or index % 25 == 0:
                logger.info(
                    "Blueprint UID catalog %s/%s (blueprints=%s)",
                    index,
                    len(unique),
                    len(out),
                )
        return out

    def blueprint_scryfall_map(
        self,
        expansion_ids: list[int],
    ) -> dict[int, str]:
        """Resolve blueprint_id → scryfall_id for the given expansions (CT only)."""
        from cardtrader_inventory.scryfall import blueprint_scryfall_map

        return blueprint_scryfall_map(self.blueprint_uid_catalog(expansion_ids))

    def _parse_export_payload(self, payload: object) -> list[Listing]:
        if not isinstance(payload, list):
            raise CardTraderError(
                f"Unexpected export shape: {type(payload).__name__}",
                body=str(payload)[:300],
            )
        listings: list[Listing] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            parsed = parse_listing(item)
            if parsed is not None:
                listings.append(parsed)
        logger.info(
            "Export returned %s products (%s parseable listings)",
            len(payload),
            len(listings),
        )
        return listings

    def marketplace_products(
        self,
        blueprint_id: int,
        *,
        language: str | None = None,
        foil: bool | None = None,
    ) -> list[MarketOffer]:
        """GET /marketplace/products for one blueprint.

        Pass ``language`` / ``foil`` so CT prefilters server-side (smaller payloads).
        Condition is not a CT query param — still filtered in ``filter_comparable_offers``.
        """
        query: dict[str, str] = {"blueprint_id": str(blueprint_id)}
        if language:
            query["language"] = language
        if foil is not None:
            query["foil"] = "true" if foil else "false"

        payload = self._request("GET", "/marketplace/products", query=query)
        offers: list[MarketOffer] = []
        if not isinstance(payload, dict):
            return offers

        # Response: { "<blueprint_id>": [ products... ], ... }
        for key, products in payload.items():
            try:
                bp = int(key)
            except (TypeError, ValueError):
                bp = blueprint_id
            if not isinstance(products, list):
                continue
            for product in products:
                if not isinstance(product, dict):
                    continue
                offer = parse_market_offer(product, blueprint_id=bp)
                if offer is not None:
                    offers.append(offer)
        return offers

    def bulk_update_products(self, products: list[dict[str, Any]]) -> str:
        """POST /products/bulk_update; returns job UUID."""
        if not products:
            raise CardTraderError("bulk_update called with empty products list")
        logger.info("Submitting bulk_update for %s products", len(products))
        payload = self._request(
            "POST",
            "/products/bulk_update",
            body={"products": products},
            timeout=self._policy.request_timeout_s,
            rate_limit=True,
        )
        if not isinstance(payload, dict) or not payload.get("job"):
            raise CardTraderError(
                f"Unexpected bulk_update response: {payload!r}",
                body=str(payload)[:500],
            )
        job_id = str(payload["job"])
        logger.info("bulk_update job=%s size=%s", job_id, len(products))
        return job_id

    def bulk_create_products(self, products: list[dict[str, Any]]) -> str:
        """POST /products/bulk_create; returns job UUID."""
        if not products:
            raise CardTraderError("bulk_create called with empty products list")
        logger.info("Submitting bulk_create for %s products", len(products))
        payload = self._request(
            "POST",
            "/products/bulk_create",
            body={"products": products},
            timeout=self._policy.request_timeout_s,
            rate_limit=True,
        )
        if not isinstance(payload, dict) or not payload.get("job"):
            raise CardTraderError(
                f"Unexpected bulk_create response: {payload!r}",
                body=str(payload)[:500],
            )
        job_id = str(payload["job"])
        logger.info("bulk_create job=%s size=%s", job_id, len(products))
        return job_id

    def get_job(self, job_uuid: str) -> dict[str, Any]:
        """GET /jobs/:uuid (enforces ~1 RPS poll spacing)."""
        elapsed = time.monotonic() - self._last_job_poll_at
        if elapsed < _JOB_POLL_MIN_INTERVAL_S:
            time.sleep(_JOB_POLL_MIN_INTERVAL_S - elapsed)
        payload = self._request(
            "GET",
            f"/jobs/{job_uuid}",
            timeout=self._policy.request_timeout_s,
            rate_limit=False,
        )
        self._last_job_poll_at = time.monotonic()
        if not isinstance(payload, dict):
            raise CardTraderError(
                f"Unexpected job payload for {job_uuid}: {type(payload).__name__}",
                body=str(payload)[:500],
            )
        return payload

    def wait_for_job(
        self,
        job_uuid: str,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Poll job until completed/unprocessable or timeout."""
        deadline = time.monotonic() + (
            timeout_s
            if timeout_s is not None
            else self._policy.bulk_job_timeout_s
        )
        while True:
            job = self.get_job(job_uuid)
            state = str(job.get("state") or "")
            if state in {"completed", "unprocessable"}:
                logger.info(
                    "Job %s finished state=%s stats=%s",
                    job_uuid,
                    state,
                    job.get("stats"),
                )
                return job
            if time.monotonic() >= deadline:
                raise CardTraderError(
                    f"Timed out waiting for job {job_uuid} (last state={state})"
                )
            logger.debug("Job %s still %s; polling…", job_uuid, state)


def parse_price_cents(value: Any) -> int | None:
    """Normalize CT price fields (int cents or money object) to int cents."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, dict):
        for key in ("cents", "price_cents", "value"):
            if key in value and value[key] is not None:
                return parse_price_cents(value[key])
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _props(item: dict[str, Any]) -> dict[str, Any]:
    props = item.get("properties_hash") or item.get("properties") or {}
    return props if isinstance(props, dict) else {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def parse_listing(item: dict[str, Any]) -> Listing | None:
    try:
        listing_id = int(item["id"])
        blueprint_id = int(item["blueprint_id"])
    except (KeyError, TypeError, ValueError):
        return None

    price = parse_price_cents(item.get("price_cents"))
    if price is None:
        return None

    props = _props(item)
    condition = str(props.get("condition") or item.get("condition") or "").strip()
    language = str(
        props.get("mtg_language") or props.get("language") or item.get("language") or ""
    ).strip()
    foil = _as_bool(props.get("mtg_foil", props.get("foil", False)))
    rarity = str(props.get("mtg_rarity") or props.get("rarity") or "").strip()
    quantity = int(item.get("quantity") or 0)
    game_id = int(item.get("game_id") or 0)
    user_id_raw = item.get("user_id")
    user_id = int(user_id_raw) if user_id_raw is not None else None
    name_en = str(item.get("name_en") or item.get("name") or "")

    return Listing(
        id=listing_id,
        blueprint_id=blueprint_id,
        quantity=quantity,
        price_cents=price,
        condition=condition,
        language=language,
        foil=foil,
        game_id=game_id,
        user_id=user_id,
        name_en=name_en,
        rarity=rarity,
        raw=item,
    )


def parse_market_offer(item: dict[str, Any], *, blueprint_id: int) -> MarketOffer | None:
    try:
        product_id = int(item["id"])
    except (KeyError, TypeError, ValueError):
        return None

    price = parse_price_cents(item.get("price") or item.get("price_cents"))
    if price is None:
        return None

    props = _props(item)
    condition = str(props.get("condition") or "").strip()
    language = str(props.get("mtg_language") or props.get("language") or "").strip()
    foil = _as_bool(props.get("mtg_foil", props.get("foil", False)))
    seller_user_id: int | None = None
    if item.get("user_id") is not None:
        try:
            seller_user_id = int(item["user_id"])
        except (TypeError, ValueError):
            seller_user_id = None
    elif isinstance(item.get("user"), dict) and item["user"].get("id") is not None:
        try:
            seller_user_id = int(item["user"]["id"])
        except (TypeError, ValueError):
            seller_user_id = None
    bp = int(item.get("blueprint_id") or blueprint_id)
    quantity = int(item.get("quantity") or 1)
    ct_zero = False
    user = item.get("user")
    if isinstance(user, dict):
        ct_zero = _as_bool(user.get("can_sell_via_hub", False))
    elif "can_sell_via_hub" in item:
        ct_zero = _as_bool(item.get("can_sell_via_hub"))

    return MarketOffer(
        product_id=product_id,
        blueprint_id=bp,
        price_cents=price,
        condition=condition,
        language=language,
        foil=foil,
        seller_user_id=seller_user_id,
        quantity=quantity,
        ct_zero=ct_zero,
    )
