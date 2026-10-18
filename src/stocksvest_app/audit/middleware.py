"""Records every API request that changes something (POST, PUT, PATCH, DELETE under /v1), success or failure,
and sets the correlation id that links the entries the request causes."""

import hashlib
import json
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..db import new_id
from . import store

# Methods that get an entry (reads are not logged). Used by: install
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# First path word -> entity type. Used by: describe
ENTITIES = {
    "filters": "FILTER", "filter-groups": "FILTER_GROUP", "automations": "AUTOMATION", "portfolios": "PORTFOLIO",
    "broker-accounts": "BROKER_ACCOUNT", "orders": "ORDER", "notifications": "NOTIFICATION", "runs": "RUN",
}
# Clearer names where the generic "<ENTITY>_<path words>" would be awkward. Used by: describe
ACTIONS = {
    ("POST", "/v1/portfolios/{portfolio_id}/orders"): ("ORDER_PLACE_MANUAL", "PORTFOLIO"),
    ("POST", "/v1/orders/{order_id}/cancel"): ("ORDER_CANCEL", "ORDER"),
    ("PUT", "/v1/automations/switch"): ("AUTOMATIONS_SWITCH_SET", None),
    ("POST", "/v1/notifications/read-all"): ("NOTIFICATIONS_READ_ALL", None),
}


def fingerprint(api_key: str | None) -> str:
    """Who made the request, without storing the key: "key:" + 10 hex chars of its SHA-256, or "anonymous".

    Args:
        api_key: the X-API-Key header.

    Used by: install.activity_log.
    """
    if not api_key:
        return "anonymous"
    return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:10]


def describe(method: str, template: str) -> tuple[str, str | None]:
    """(action, entity type) for a route template, e.g. POST /v1/automations/{automation_id}/pause ->
    (AUTOMATION_PAUSE, AUTOMATION).

    Args:
        method: HTTP method.
        template: the route's path template.

    Used by: install.activity_log.
    """
    if (method, template) in ACTIONS:
        return ACTIONS[(method, template)]
    parts = [p for p in template.removeprefix("/v1/").split("/") if p]
    entity = ENTITIES.get(parts[0], parts[0].upper().replace("-", "_"))
    words = [p for p in parts[1:] if not p.startswith("{")]
    if words:
        verb = "_".join(words).upper().replace("-", "_")
    elif method == "POST":
        verb = "CREATE"
    elif method == "PUT":
        verb = "UPDATE"
    elif method == "DELETE":
        verb = "DELETE"
    else:
        verb = method
    return f"{entity}_{verb}", entity


def _related(path_params: dict, body) -> dict:
    """Ids the request mentions: path parameters (automation_id -> automationId) and body fields ending in Id / Ids.

    Args:
        path_params: the route's path parameters.
        body: the parsed JSON body.

    Used by: install.activity_log.
    """
    out = {re.sub(r"_id$", "Id", re.sub(r"_(\w)", lambda m: m.group(1).upper(), k)): v
           for k, v in path_params.items()}
    if isinstance(body, dict):
        for k, v in body.items():
            if k.endswith("Id") and isinstance(v, str):
                out[k] = v
            elif k.endswith("Ids") and isinstance(v, list):
                out[k] = ",".join(str(x) for x in v)
    return out


def install(app: FastAPI, db_path) -> None:
    """Add the activity log middleware to the app.

    Args:
        app: the FastAPI app.
        db_path: app DB file (each entry is written on its own connection).

    Used by: api.create_app.
    """
    @app.middleware("http")
    async def activity_log(request: Request, call_next):
        """Run the request with a new correlation id (also returned as the x-correlation-id header), then record
        the request, its ids, its outcome and how long it took.

        Args:
            request: the incoming request.
            call_next: the rest of the app.
        """
        if request.method not in MUTATING or not request.url.path.startswith("/v1/"):
            return await call_next(request)
        started = time.monotonic()
        cid = new_id("req")
        tokens = (store.correlation_id.set(cid), store.current_source.set("API"),
                  store.current_actor.set(fingerprint(request.headers.get("x-api-key"))))
        raw = await request.body()
        try:
            response = await call_next(request)
            content = b"".join([chunk async for chunk in response.body_iterator])
        finally:
            for var, token in zip((store.correlation_id, store.current_source, store.current_actor), tokens):
                var.reset(token)

        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)
        action, entity = describe(request.method, template)
        params = dict(request.scope.get("path_params") or {})
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = {"unparsed": raw[:500].decode(errors="replace")}
        try:
            answer = json.loads(content) if content else None
        except ValueError:
            answer = None
        entity_id = next((v for k, v in params.items() if k.endswith("_id")), None)
        if entity_id is None and isinstance(answer, dict) and response.status_code < 300:
            entity_id = answer.get("id")
        error = None
        if response.status_code >= 400:
            detail = answer.get("detail") if isinstance(answer, dict) else None
            error = detail if isinstance(detail, str) else json.dumps(detail)[:2000] if detail else f"HTTP {response.status_code}"
        details = {"request": body}
        if request.query_params:
            details["query"] = dict(request.query_params)
        await run_in_threadpool(
            store.record, None, action, entity, entity_id, related=_related(params, body), details=details,
            outcome="OK" if response.status_code < 400 else "FAILED", error=error, source="API",
            actor=fingerprint(request.headers.get("x-api-key")), method=request.method, path=request.url.path,
            status=response.status_code, duration_ms=int((time.monotonic() - started) * 1000), db_path=db_path,
            correlation=cid)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        headers["x-correlation-id"] = cid
        return Response(content=content, status_code=response.status_code, headers=headers,
                        media_type=response.media_type)
