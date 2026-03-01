#!/usr/bin/env python3
import asyncio
import base64
import html
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "merchant_pay_demo.db"


def load_env_file(path: Path) -> None:
  if not path.exists():
    return

  for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue

    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip().strip("\"").strip("'")
    if key and key not in os.environ:
      os.environ[key] = value


load_env_file(BASE_DIR / ".env")

REQUEST_TTL_SECONDS = int(os.environ.get("REQUEST_TTL_SECONDS", "120"))
DEFAULT_CUSTOMER_BALANCE = float(os.environ.get("DEFAULT_CUSTOMER_BALANCE", "80"))
MOCK_FEE_RATE = float(os.environ.get("MOCK_FEE_RATE", "0.01"))

WALLET_PROVIDER = str(os.environ.get("WALLET_PROVIDER", "mock")).strip().lower()
CDP_NETWORK = str(os.environ.get("CDP_NETWORK", "base")).strip()
CDP_TOKEN = str(os.environ.get("CDP_TOKEN", "usdc")).strip().lower()
CDP_TOKEN_DECIMALS = int(os.environ.get("CDP_TOKEN_DECIMALS", "6"))
CDP_ACCOUNT_PREFIX = str(os.environ.get("CDP_ACCOUNT_PREFIX", "whatsapp-pay-demo")).strip()
CDP_TOKEN_CONTRACT = str(os.environ.get("CDP_TOKEN_CONTRACT", "")).strip().lower()

HUBSPOT_ACCESS_TOKEN = str(os.environ.get("HUBSPOT_ACCESS_TOKEN", "")).strip()
HUBSPOT_API_BASE = str(os.environ.get("HUBSPOT_API_BASE", "https://api.hubapi.com")).strip().rstrip("/")
HUBSPOT_TIMEOUT_SECONDS = int(os.environ.get("HUBSPOT_TIMEOUT_SECONDS", "20"))
HUBSPOT_CACHE_TTL_SECONDS = int(os.environ.get("HUBSPOT_CACHE_TTL_SECONDS", "120"))
HUBSPOT_DETAIL_CACHE_TTL_SECONDS = 30
HUBSPOT_MAX_RETRIES = 3
HUBSPOT_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
HUBSPOT_CONNECTION_MODE = str(os.environ.get("HUBSPOT_CONNECTION_MODE", "composio")).strip().lower()

COMPOSIO_API_BASE = str(os.environ.get("COMPOSIO_API_BASE", "https://backend.composio.dev")).strip().rstrip("/")
COMPOSIO_API_KEY = str(os.environ.get("COMPOSIO_API_KEY", "")).strip()
COMPOSIO_USER_ID = str(os.environ.get("COMPOSIO_USER_ID", "")).strip()
COMPOSIO_CONNECTED_ACCOUNT_ID = str(os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")).strip()
COMPOSIO_TOOLKIT_SLUG = str(os.environ.get("COMPOSIO_TOOLKIT_SLUG", "hubspot")).strip().lower()
COMPOSIO_ENFORCE_READ_ONLY_RAW = os.environ.get("COMPOSIO_ENFORCE_READ_ONLY")
COMPOSIO_CONNECTED_ACCOUNT_CACHE_TTL_SECONDS = int(
  os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_CACHE_TTL_SECONDS", "300")
)
WA_MERCHANT_PHONE = str(os.environ.get("WA_MERCHANT_PHONE", "")).strip()
WA_CUSTOMER_PHONE = str(os.environ.get("WA_CUSTOMER_PHONE", "")).strip()
TWILIO_ACCOUNT_SID = str(os.environ.get("TWILIO_ACCOUNT_SID", "")).strip()
TWILIO_AUTH_TOKEN = str(os.environ.get("TWILIO_AUTH_TOKEN", "")).strip()
TWILIO_WHATSAPP_FROM = str(os.environ.get("TWILIO_WHATSAPP_FROM", "")).strip()

BOT_NAME = "PayBot"

CURRENCY_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:usdc|usd|\$)", re.IGNORECASE)
PLAIN_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)")
ALLOWED_WALLET_PROVIDERS = {"mock", "cdp"}

DB_LOCK = threading.Lock()
HUBSPOT_CACHE_LOCK = threading.Lock()
HUBSPOT_CACHE = {}
COMPOSIO_ACCOUNT_CACHE_LOCK = threading.Lock()
COMPOSIO_ACCOUNT_CACHE = {"value": "", "expires_at": 0.0}


def parse_bool(value: str, default: bool = True) -> bool:
  if value is None:
    return default
  normalized = str(value).strip().lower()
  return normalized not in {"0", "false", "no", "off"}


COMPOSIO_ENFORCE_READ_ONLY = parse_bool(COMPOSIO_ENFORCE_READ_ONLY_RAW, True)
PAYMENTS_ENABLED = parse_bool(os.environ.get("PAYMENTS_ENABLED"), True)


def utc_now() -> datetime:
  return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
  return value.astimezone(timezone.utc).isoformat()


def normalize_text(value: str) -> str:
  lowered = str(value or "").strip().casefold()
  deaccented = unicodedata.normalize("NFKD", lowered)
  return "".join(ch for ch in deaccented if not unicodedata.combining(ch))


def normalize_phone(value: str) -> str:
  raw = str(value or "").strip()
  if raw.lower().startswith("whatsapp:"):
    raw = raw.split(":", 1)[1]
  return "".join(ch for ch in raw if ch.isdigit())


def as_float(value) -> float:
  return round(float(value), 2)


def format_amount(value: float) -> str:
  decimal_value = Decimal(str(value)).quantize(Decimal("0.01"))
  raw = format(decimal_value, "f")
  trimmed = raw.rstrip("0").rstrip(".")
  return trimmed or "0"


def connect_db() -> sqlite3.Connection:
  connection = sqlite3.connect(DB_PATH)
  connection.row_factory = sqlite3.Row
  return connection


def run_async(coro):
  try:
    asyncio.get_running_loop()
  except RuntimeError:
    return asyncio.run(coro)

  loop = asyncio.new_event_loop()
  try:
    return loop.run_until_complete(coro)
  finally:
    loop.close()


def require_cdp_sdk():
  try:
    from cdp import CdpClient, parse_units
  except ImportError as exc:
    raise RuntimeError(
      "No se encontro 'cdp-sdk'. Instala con Python >= 3.10: pip install cdp-sdk"
    ) from exc

  return CdpClient, parse_units


class HubSpotAPIError(Exception):
  def __init__(self, message: str, status_code: int, category: str, details=None):
    super().__init__(message)
    self.message = str(message)
    self.status_code = int(status_code)
    self.category = str(category)
    self.details = details or {}

  def to_payload(self):
    payload = {
      "error": self.message,
      "category": self.category,
      "statusCode": self.status_code,
    }
    if self.details:
      payload["details"] = self.details
    return payload


HUBSPOT_OBJECT_PROPERTIES = {
  "deals": [
    "dealname",
    "amount",
    "dealstage",
    "pipeline",
    "closedate",
    "hubspot_owner_id",
    "createdate",
    "hs_lastmodifieddate",
  ],
  "contacts": [
    "firstname",
    "lastname",
    "email",
    "phone",
    "company",
    "createdate",
    "lastmodifieddate",
  ],
  "companies": [
    "name",
    "domain",
    "phone",
    "city",
    "country",
    "industry",
    "createdate",
    "hs_lastmodifieddate",
  ],
}

HUBSPOT_ASSOCIATION_TARGETS = {
  "deals": ("contacts", "companies"),
  "contacts": ("companies", "deals"),
  "companies": ("contacts", "deals"),
}


def first_query_value(query: dict, key: str, default: str = "") -> str:
  values = query.get(key, [])
  if not values:
    return default
  return str(values[0] or default).strip()


def parse_limit(raw_value: str, default: int = 25) -> int:
  try:
    value = int(str(raw_value).strip())
  except (TypeError, ValueError):
    value = default
  return max(1, min(value, 100))


def to_money(value):
  if value in (None, ""):
    return None
  try:
    return as_float(value)
  except (TypeError, ValueError, InvalidOperation):
    return None


def hubspot_cache_key(path: str, query: dict) -> str:
  parts = []
  for key in sorted(query):
    value = query[key]
    parts.append(f"{key}={value}")
  return f"{path}?{'&'.join(parts)}"


def hubspot_cache_get(cache_key: str):
  now_ts = time.time()
  with HUBSPOT_CACHE_LOCK:
    entry = HUBSPOT_CACHE.get(cache_key)
    if entry is None:
      return None
    if now_ts >= entry["expires_at"]:
      HUBSPOT_CACHE.pop(cache_key, None)
      return None
    return json.loads(json.dumps(entry["payload"], ensure_ascii=False))


def hubspot_cache_set(cache_key: str, payload, ttl_seconds: int):
  with HUBSPOT_CACHE_LOCK:
    HUBSPOT_CACHE[cache_key] = {
      "payload": payload,
      "expires_at": time.time() + max(1, int(ttl_seconds)),
    }


def cached_hubspot_read(cache_key: str, force_refresh: bool, ttl_seconds: int, loader):
  if not force_refresh:
    cached = hubspot_cache_get(cache_key)
    if cached is not None:
      return cached
  payload = loader()
  hubspot_cache_set(cache_key, payload, ttl_seconds)
  return payload


def hubspot_is_configured() -> bool:
  return bool(HUBSPOT_ACCESS_TOKEN)


def hubspot_health():
  if hubspot_is_configured():
    return {"ok": True, "auth": "configured", "message": "HubSpot access token configurado."}
  return {
    "ok": False,
    "auth": "missing",
    "message": "Falta HUBSPOT_ACCESS_TOKEN en variables de entorno.",
  }


def ensure_hubspot_configured() -> None:
  if not hubspot_is_configured():
    raise HubSpotAPIError(
      "HubSpot no configurado. Define HUBSPOT_ACCESS_TOKEN en .env.",
      503,
      "HUBSPOT_NOT_CONFIGURED",
    )


def hubspot_error_from_response(status_code: int, payload: dict) -> HubSpotAPIError:
  category = str(payload.get("category") or "HUBSPOT_API_ERROR")
  raw_message = str(payload.get("message") or "").strip()
  correlation_id = str(payload.get("correlationId") or "").strip()

  details = {}
  if correlation_id:
    details["correlationId"] = correlation_id
  if raw_message:
    details["hubspotMessage"] = raw_message
  if payload.get("context"):
    details["context"] = payload.get("context")

  if status_code == 401:
    message = "Token de HubSpot invalido, expirado o desactivado."
  elif status_code == 403:
    message = "Token de HubSpot sin permisos suficientes para este endpoint."
  elif status_code == 404:
    message = "Recurso no encontrado en HubSpot."
  elif status_code == 429:
    message = "HubSpot rate limit alcanzado. Intenta de nuevo en unos segundos."
  elif status_code >= 500:
    message = "HubSpot no disponible temporalmente."
  else:
    message = raw_message or "Error consultando la API de HubSpot."

  return HubSpotAPIError(message, status_code, category, details)


def hubspot_request(method: str, path: str, query=None, body=None):
  ensure_hubspot_configured()

  clean_query = {}
  for key, value in (query or {}).items():
    if value is None:
      continue
    text = str(value).strip()
    if text == "":
      continue
    clean_query[key] = text

  query_suffix = urlencode(clean_query)
  url = f"{HUBSPOT_API_BASE}{path}"
  if query_suffix:
    url = f"{url}?{query_suffix}"

  payload_raw = None
  headers = {
    "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
    "Accept": "application/json",
  }
  if body is not None:
    payload_raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers["Content-Type"] = "application/json"

  for attempt in range(HUBSPOT_MAX_RETRIES):
    request = Request(url=url, data=payload_raw, method=method.upper(), headers=headers)
    try:
      with urlopen(request, timeout=HUBSPOT_TIMEOUT_SECONDS) as response:
        raw = response.read()
        if not raw:
          return {}
        return json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
      status_code = int(exc.code)
      raw_error = exc.read().decode("utf-8", errors="replace")
      parsed_error = {}
      if raw_error:
        try:
          parsed_error = json.loads(raw_error)
        except json.JSONDecodeError:
          parsed_error = {"message": raw_error}

      should_retry = (
        status_code in HUBSPOT_RETRYABLE_STATUS_CODES and attempt < HUBSPOT_MAX_RETRIES - 1
      )
      if should_retry:
        time.sleep(0.35 * (2 ** attempt))
        continue
      raise hubspot_error_from_response(status_code, parsed_error) from exc
    except URLError as exc:
      if attempt < HUBSPOT_MAX_RETRIES - 1:
        time.sleep(0.35 * (2 ** attempt))
        continue
      reason = str(getattr(exc, "reason", exc))
      raise HubSpotAPIError(
        "No se pudo conectar con HubSpot.",
        502,
        "HUBSPOT_CONNECTION_ERROR",
        {"reason": reason},
      ) from exc
    except TimeoutError as exc:
      if attempt < HUBSPOT_MAX_RETRIES - 1:
        time.sleep(0.35 * (2 ** attempt))
        continue
      raise HubSpotAPIError(
        "Timeout consultando HubSpot.",
        504,
        "HUBSPOT_TIMEOUT",
      ) from exc

  raise HubSpotAPIError(
    "No fue posible consultar HubSpot tras varios intentos.",
    502,
    "HUBSPOT_RETRY_EXHAUSTED",
  )


def hubspot_next_after(payload: dict):
  paging = payload.get("paging") or {}
  next_page = paging.get("next") or {}
  after = next_page.get("after")
  if after in (None, ""):
    return None
  return str(after)


def normalize_deal(record: dict, include_properties: bool = False):
  properties = record.get("properties") or {}
  normalized = {
    "id": str(record.get("id") or ""),
    "name": str(properties.get("dealname") or ""),
    "amount": to_money(properties.get("amount")),
    "stageId": str(properties.get("dealstage") or ""),
    "pipelineId": str(properties.get("pipeline") or ""),
    "closeDate": str(properties.get("closedate") or ""),
    "createdAt": str(properties.get("createdate") or ""),
    "updatedAt": str(properties.get("hs_lastmodifieddate") or ""),
    "ownerId": str(properties.get("hubspot_owner_id") or ""),
  }
  if include_properties:
    normalized["properties"] = properties
  return normalized


def normalize_contact(record: dict, include_properties: bool = False):
  properties = record.get("properties") or {}
  normalized = {
    "id": str(record.get("id") or ""),
    "firstname": str(properties.get("firstname") or ""),
    "lastname": str(properties.get("lastname") or ""),
    "email": str(properties.get("email") or ""),
    "phone": str(properties.get("phone") or ""),
    "company": str(properties.get("company") or ""),
    "createdAt": str(properties.get("createdate") or ""),
    "updatedAt": str(properties.get("lastmodifieddate") or ""),
  }
  if include_properties:
    normalized["properties"] = properties
  return normalized


def normalize_company(record: dict, include_properties: bool = False):
  properties = record.get("properties") or {}
  normalized = {
    "id": str(record.get("id") or ""),
    "name": str(properties.get("name") or ""),
    "domain": str(properties.get("domain") or ""),
    "phone": str(properties.get("phone") or ""),
    "city": str(properties.get("city") or ""),
    "country": str(properties.get("country") or ""),
    "industry": str(properties.get("industry") or ""),
    "createdAt": str(properties.get("createdate") or ""),
    "updatedAt": str(properties.get("hs_lastmodifieddate") or ""),
  }
  if include_properties:
    normalized["properties"] = properties
  return normalized


def normalize_pipeline(record: dict):
  stages = [
    {
      "id": str(stage.get("id") or ""),
      "label": str(stage.get("label") or ""),
      "displayOrder": int(stage.get("displayOrder") or 0),
    }
    for stage in (record.get("stages") or [])
  ]
  stages.sort(key=lambda item: item["displayOrder"])
  return {
    "id": str(record.get("id") or ""),
    "label": str(record.get("label") or ""),
    "stages": stages,
  }


def hubspot_fetch_pipelines(object_type: str):
  payload = hubspot_request("GET", f"/crm/v3/pipelines/{object_type}")
  pipelines = [normalize_pipeline(item) for item in (payload.get("results") or [])]
  return {
    "objectType": object_type,
    "items": pipelines,
  }


def hubspot_search_objects(
  object_type: str,
  properties: list,
  limit: int,
  after: str = "",
  query_text: str = "",
  filter_groups=None,
):
  body = {
    "properties": properties,
    "limit": limit,
  }
  if after:
    body["after"] = after
  if query_text:
    body["query"] = query_text
  if filter_groups:
    body["filterGroups"] = filter_groups
  return hubspot_request("POST", f"/crm/v3/objects/{object_type}/search", body=body)


def hubspot_fetch_deals(limit: int, after: str = "", pipeline_id: str = "", stage_id: str = "", q: str = ""):
  filters = []
  if pipeline_id:
    filters.append({"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id})
  if stage_id:
    filters.append({"propertyName": "dealstage", "operator": "EQ", "value": stage_id})

  filter_groups = [{"filters": filters}] if filters else None
  payload = hubspot_search_objects(
    "deals",
    HUBSPOT_OBJECT_PROPERTIES["deals"],
    limit=limit,
    after=after,
    query_text=q,
    filter_groups=filter_groups,
  )
  items = [normalize_deal(item) for item in (payload.get("results") or [])]
  return {
    "items": items,
    "paging": {"nextAfter": hubspot_next_after(payload)},
  }


def hubspot_fetch_contacts(limit: int, after: str = "", q: str = ""):
  payload = hubspot_search_objects(
    "contacts",
    HUBSPOT_OBJECT_PROPERTIES["contacts"],
    limit=limit,
    after=after,
    query_text=q,
  )
  items = [normalize_contact(item) for item in (payload.get("results") or [])]
  return {
    "items": items,
    "paging": {"nextAfter": hubspot_next_after(payload)},
  }


def hubspot_fetch_companies(limit: int, after: str = "", q: str = ""):
  payload = hubspot_search_objects(
    "companies",
    HUBSPOT_OBJECT_PROPERTIES["companies"],
    limit=limit,
    after=after,
    query_text=q,
  )
  items = [normalize_company(item) for item in (payload.get("results") or [])]
  return {
    "items": items,
    "paging": {"nextAfter": hubspot_next_after(payload)},
  }


def hubspot_fetch_association_ids(from_object: str, object_id: str, to_object: str, max_items: int = 100):
  collected = []
  seen = set()
  after = None
  pages = 0

  while pages < 5 and len(collected) < max_items:
    query = {"limit": 100}
    if after:
      query["after"] = after

    payload = hubspot_request(
      "GET",
      f"/crm/v4/objects/{from_object}/{object_id}/associations/{to_object}",
      query=query,
    )
    for row in (payload.get("results") or []):
      raw_id = row.get("toObjectId") or row.get("id")
      if raw_id in (None, ""):
        continue
      candidate = str(raw_id)
      if candidate in seen:
        continue
      seen.add(candidate)
      collected.append(candidate)
      if len(collected) >= max_items:
        break

    after = hubspot_next_after(payload)
    if not after:
      break
    pages += 1

  return collected


def contact_label(properties: dict, fallback_id: str):
  first = str(properties.get("firstname") or "").strip()
  last = str(properties.get("lastname") or "").strip()
  full_name = " ".join(part for part in (first, last) if part)
  if full_name:
    return full_name
  email = str(properties.get("email") or "").strip()
  if email:
    return email
  return f"Contacto {fallback_id}"


def company_label(properties: dict, fallback_id: str):
  name = str(properties.get("name") or "").strip()
  if name:
    return name
  domain = str(properties.get("domain") or "").strip()
  if domain:
    return domain
  return f"Empresa {fallback_id}"


def deal_label(properties: dict, fallback_id: str):
  dealname = str(properties.get("dealname") or "").strip()
  if dealname:
    return dealname
  amount = str(properties.get("amount") or "").strip()
  if amount:
    return f"Deal {fallback_id} ({amount})"
  return f"Deal {fallback_id}"


def hubspot_batch_fetch_labels(object_type: str, ids: list):
  if not ids:
    return []

  properties = {
    "contacts": ["firstname", "lastname", "email"],
    "companies": ["name", "domain"],
    "deals": ["dealname", "amount"],
  }.get(object_type, [])

  body = {
    "properties": properties,
    "inputs": [{"id": item_id} for item_id in ids[:100]],
  }
  payload = hubspot_request("POST", f"/crm/v3/objects/{object_type}/batch/read", body=body)
  by_id = {}
  for row in (payload.get("results") or []):
    row_id = str(row.get("id") or "")
    props = row.get("properties") or {}
    if object_type == "contacts":
      by_id[row_id] = contact_label(props, row_id)
    elif object_type == "companies":
      by_id[row_id] = company_label(props, row_id)
    else:
      by_id[row_id] = deal_label(props, row_id)

  items = []
  for item_id in ids:
    items.append(
      {
        "id": item_id,
        "label": by_id.get(item_id) or f"{object_type[:-1].capitalize()} {item_id}",
      }
    )
  return items


def hubspot_fetch_associations(object_type: str, object_id: str):
  associations = {"contacts": [], "companies": [], "deals": []}
  targets = HUBSPOT_ASSOCIATION_TARGETS.get(object_type, ())

  for target_type in targets:
    ids = hubspot_fetch_association_ids(object_type, object_id, target_type)
    associations[target_type] = hubspot_batch_fetch_labels(target_type, ids)
  return associations


def hubspot_fetch_detail(object_type: str, object_id: str):
  if object_type not in HUBSPOT_OBJECT_PROPERTIES:
    raise HubSpotAPIError("Tipo de objeto HubSpot no soportado.", 400, "INVALID_OBJECT_TYPE")

  payload = hubspot_request(
    "GET",
    f"/crm/v3/objects/{object_type}/{object_id}",
    query={"properties": ",".join(HUBSPOT_OBJECT_PROPERTIES[object_type])},
  )

  if object_type == "deals":
    item = normalize_deal(payload, include_properties=True)
  elif object_type == "contacts":
    item = normalize_contact(payload, include_properties=True)
  else:
    item = normalize_company(payload, include_properties=True)

  return {
    "item": item,
    "associations": hubspot_fetch_associations(object_type, object_id),
  }


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
  existing_columns = {
    row["name"]
    for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
  }
  if column_name not in existing_columns:
    conn.execute(ddl)


def initialize_db() -> None:
  with connect_db() as conn:
    conn.execute(
      """
      CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        role TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        balance_usdc REAL NOT NULL DEFAULT 0
      )
      """
    )
    conn.execute(
      """
      CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_role TEXT NOT NULL,
        sender_name TEXT NOT NULL,
        body TEXT NOT NULL,
        message_type TEXT NOT NULL DEFAULT 'text',
        created_at TEXT NOT NULL
      )
      """
    )
    conn.execute(
      """
      CREATE TABLE IF NOT EXISTS payment_requests (
        id TEXT PRIMARY KEY,
        amount_usdc REAL NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USDC',
        created_by_role TEXT NOT NULL,
        status TEXT NOT NULL,
        token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        confirmed_at TEXT,
        canceled_at TEXT
      )
      """
    )
    conn.execute(
      """
      CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY,
        payment_request_id TEXT NOT NULL,
        gross_amount_usdc REAL NOT NULL,
        fee_usdc REAL NOT NULL,
        net_amount_usdc REAL NOT NULL,
        from_user_id TEXT NOT NULL,
        to_user_id TEXT NOT NULL,
        provider_tx_hash TEXT,
        created_at TEXT NOT NULL
      )
      """
    )
    ensure_column(
      conn,
      "transactions",
      "provider_tx_hash",
      "ALTER TABLE transactions ADD COLUMN provider_tx_hash TEXT",
    )

    conn.execute(
      """
      CREATE TABLE IF NOT EXISTS wallet_accounts (
        role TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        account_name TEXT NOT NULL,
        address TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      )
      """
    )

    user_count = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
    if user_count == 0:
      seed_users(conn)
      seed_welcome_messages(conn)

    sync_whatsapp_role_phones(conn)


def seed_users(conn: sqlite3.Connection) -> None:
  conn.execute(
    """
    INSERT INTO users (id, role, display_name, phone, balance_usdc)
    VALUES (?, ?, ?, ?, ?)
    """,
    ("merchant_1", "merchant", "Pizzeria Napoli", "+56 9 5555 1111", 0.0),
  )
  conn.execute(
    """
    INSERT INTO users (id, role, display_name, phone, balance_usdc)
    VALUES (?, ?, ?, ?, ?)
    """,
    ("customer_1", "customer", "Bruno", "+56 9 7777 3333", DEFAULT_CUSTOMER_BALANCE),
  )


def sync_whatsapp_role_phones(conn: sqlite3.Connection) -> None:
  updates = []
  merchant_phone = normalize_phone(WA_MERCHANT_PHONE)
  customer_phone = normalize_phone(WA_CUSTOMER_PHONE)
  if merchant_phone:
    updates.append(("+" + merchant_phone, "merchant"))
  if customer_phone:
    updates.append(("+" + customer_phone, "customer"))

  for phone, role in updates:
    conn.execute(
      """
      UPDATE users
      SET phone = ?
      WHERE role = ?
      """,
      (phone, role),
    )


def current_fee_rate() -> float:
  if WALLET_PROVIDER == "mock":
    return MOCK_FEE_RATE
  return 0.0


def wallet_status_text() -> str:
  if WALLET_PROVIDER == "mock":
    return "Wallet demo local activa (sin blockchain)."
  return f"Wallet real CDP activa en red {CDP_NETWORK}."


def seed_welcome_messages(conn: sqlite3.Connection) -> None:
  append_message(
    conn,
    "bot",
    BOT_NAME,
    (
      "Demo lista. Puedes escribir frases naturales como 'cobrar 10 usd', "
      "'pagar 10 usd' o 'ventas hoy'."
    ),
  )
  append_message(conn, "bot", BOT_NAME, wallet_status_text())


def append_message(
  conn: sqlite3.Connection,
  sender_role: str,
  sender_name: str,
  body: str,
  message_type: str = "text",
) -> None:
  conn.execute(
    """
    INSERT INTO chat_messages (sender_role, sender_name, body, message_type, created_at)
    VALUES (?, ?, ?, ?, ?)
    """,
    (sender_role, sender_name, str(body), message_type, to_iso(utc_now())),
  )


def get_user(conn: sqlite3.Connection, role: str) -> sqlite3.Row:
  row = conn.execute(
    "SELECT id, role, display_name, phone, balance_usdc FROM users WHERE role = ?",
    (role,),
  ).fetchone()
  if row is None:
    raise RuntimeError(f"No existe el usuario para role={role}")
  return row


def expire_overdue_requests(conn: sqlite3.Connection) -> None:
  now = to_iso(utc_now())
  conn.execute(
    """
    UPDATE payment_requests
    SET status = 'EXPIRED'
    WHERE status = 'PENDING' AND expires_at < ?
    """,
    (now,),
  )


def get_pending_request(conn: sqlite3.Connection):
  row = conn.execute(
    """
    SELECT *
    FROM payment_requests
    WHERE status = 'PENDING'
    ORDER BY created_at DESC
    LIMIT 1
    """
  ).fetchone()
  return row


def parse_amount(text: str):
  currency_match = CURRENCY_AMOUNT_RE.search(text)
  if currency_match:
    return as_float(currency_match.group(1).replace(",", "."))

  all_numbers = [as_float(token.replace(",", ".")) for token in PLAIN_NUMBER_RE.findall(text)]
  positive = [value for value in all_numbers if value > 0]
  if len(positive) == 1:
    return positive[0]
  return None


def detect_intent(actor: str, original_text: str):
  normalized = normalize_text(original_text)
  amount = parse_amount(normalized)

  if "ventas hoy" in normalized:
    return "sales_today", None

  if normalized in {"ayuda", "help", "menu"}:
    return "help", None

  has_charge_words = any(word in normalized for word in ("cobrar", "cobro", "total", "son"))
  has_pay_words = any(word in normalized for word in ("pagar", "pago", "pagarte"))

  if amount is not None:
    if actor == "merchant" and has_charge_words:
      return "create_charge", amount
    if has_pay_words:
      return "create_charge", amount

  return "unknown", None


def create_charge_request(conn: sqlite3.Connection, actor: str, amount_usdc: float):
  now = utc_now()
  expires_at = now + timedelta(seconds=REQUEST_TTL_SECONDS)
  request_id = f"REQ_{uuid4().hex[:10].upper()}"
  token = str(1000 + (uuid4().int % 9000))

  conn.execute(
    """
    INSERT INTO payment_requests
      (id, amount_usdc, currency, created_by_role, status, token, created_at, expires_at)
    VALUES (?, ?, 'USDC', ?, 'PENDING', ?, ?, ?)
    """,
    (request_id, amount_usdc, actor, token, to_iso(now), to_iso(expires_at)),
  )

  return conn.execute(
    """
    SELECT *
    FROM payment_requests
    WHERE id = ?
    """,
    (request_id,),
  ).fetchone()


def sales_today(conn: sqlite3.Connection):
  today_key = utc_now().date().isoformat()
  totals = conn.execute(
    """
    SELECT
      COALESCE(SUM(gross_amount_usdc), 0) AS gross,
      COALESCE(COUNT(*), 0) AS tx_count
    FROM transactions
    WHERE substr(created_at, 1, 10) = ?
    """,
    (today_key,),
  ).fetchone()
  return as_float(totals["gross"]), int(totals["tx_count"])


def upsert_wallet_account(
  conn: sqlite3.Connection,
  role: str,
  provider: str,
  account_name: str,
  address: str,
  metadata: dict = None,
):
  metadata = metadata or {}
  now_iso = to_iso(utc_now())
  conn.execute(
    """
    INSERT INTO wallet_accounts (role, provider, account_name, address, metadata_json, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(role) DO UPDATE SET
      provider = excluded.provider,
      account_name = excluded.account_name,
      address = excluded.address,
      metadata_json = excluded.metadata_json,
      updated_at = excluded.updated_at
    """,
    (
      role,
      provider,
      account_name,
      address,
      json.dumps(metadata, ensure_ascii=False),
      now_iso,
      now_iso,
    ),
  )


def get_wallet_account(conn: sqlite3.Connection, role: str):
  return conn.execute(
    """
    SELECT role, provider, account_name, address, metadata_json
    FROM wallet_accounts
    WHERE role = ?
    """,
    (role,),
  ).fetchone()


def cdp_account_name_for_role(role: str) -> str:
  suffix = "".join(ch for ch in role if ch.isalnum() or ch in {"-", "_"})
  return f"{CDP_ACCOUNT_PREFIX}-{suffix}"


def cdp_get_or_create_account(account_name: str):
  CdpClient, _ = require_cdp_sdk()

  async def _task():
    async with CdpClient() as cdp:
      account = await cdp.evm.get_or_create_account(name=account_name)
      return account.name or account_name, account.address

  return run_async(_task())


def cdp_get_usdc_balance(account_name: str) -> float:
  CdpClient, _ = require_cdp_sdk()

  async def _task():
    async with CdpClient() as cdp:
      account = await cdp.evm.get_or_create_account(name=account_name)
      balances = await account.list_token_balances(network=CDP_NETWORK, page_size=50)
      return balances

  balances = run_async(_task())
  selected = None

  for token_balance in balances.balances:
    symbol = (token_balance.token.symbol or "").strip().lower()
    contract = (token_balance.token.contract_address or "").strip().lower()

    if CDP_TOKEN_CONTRACT and contract == CDP_TOKEN_CONTRACT:
      selected = token_balance
      break

    if symbol == CDP_TOKEN:
      selected = token_balance

  if selected is None:
    return 0.0

  decimals = int(selected.amount.decimals)
  amount_atomic = int(selected.amount.amount)
  amount = Decimal(amount_atomic) / (Decimal(10) ** decimals)
  return as_float(amount)


def cdp_transfer_usdc(from_account_name: str, to_address: str, amount_usdc: float) -> str:
  CdpClient, parse_units = require_cdp_sdk()

  try:
    amount_atomic = parse_units(format_amount(amount_usdc), CDP_TOKEN_DECIMALS)
  except (InvalidOperation, ValueError) as exc:
    raise RuntimeError(f"Monto invalido para transferir: {amount_usdc}") from exc

  async def _task():
    async with CdpClient() as cdp:
      sender = await cdp.evm.get_or_create_account(name=from_account_name)
      tx_hash = await sender.transfer(
        to=to_address,
        amount=amount_atomic,
        token=CDP_TOKEN,
        network=CDP_NETWORK,
      )
      return str(tx_hash)

  return run_async(_task())


def explorer_tx_url(tx_hash: str) -> str:
  if not tx_hash:
    return ""
  if CDP_NETWORK == "base":
    return f"https://basescan.org/tx/{tx_hash}"
  if CDP_NETWORK == "base-sepolia":
    return f"https://sepolia.basescan.org/tx/{tx_hash}"
  return ""


def ensure_wallet_accounts(conn: sqlite3.Connection) -> None:
  roles = ("merchant", "customer")

  if WALLET_PROVIDER == "mock":
    for role in roles:
      upsert_wallet_account(
        conn,
        role,
        "mock",
        f"mock-{role}",
        f"mock_{role}_wallet",
        metadata={"network": "demo-local"},
      )
    return

  if WALLET_PROVIDER != "cdp":
    raise RuntimeError(f"Proveedor de wallet no soportado: {WALLET_PROVIDER}")

  for role in roles:
    account_name = cdp_account_name_for_role(role)
    resolved_name, address = cdp_get_or_create_account(account_name)
    upsert_wallet_account(
      conn,
      role,
      "cdp",
      resolved_name,
      address,
      metadata={"network": CDP_NETWORK},
    )


def refresh_wallet_balances(conn: sqlite3.Connection) -> str:
  if WALLET_PROVIDER != "cdp":
    return ""

  error_messages = []
  for role in ("merchant", "customer"):
    wallet = get_wallet_account(conn, role)
    if wallet is None:
      error_messages.append(f"No hay wallet configurada para {role}.")
      continue

    try:
      balance = cdp_get_usdc_balance(wallet["account_name"])
      conn.execute(
        """
        UPDATE users
        SET balance_usdc = ?
        WHERE role = ?
        """,
        (as_float(balance), role),
      )
    except Exception as exc:
      error_messages.append(f"{role}: {exc}")

  return " | ".join(error_messages)


def process_chat_message(actor: str, text: str):
  with DB_LOCK:
    with connect_db() as conn:
      expire_overdue_requests(conn)

      sender = get_user(conn, actor)
      append_message(conn, actor, sender["display_name"], text)

      intent, amount = detect_intent(actor, text)
      pending = get_pending_request(conn)

      if intent == "sales_today":
        total, tx_count = sales_today(conn)
        append_message(
          conn,
          "bot",
          BOT_NAME,
          f"Ventas hoy: {total:.2f} USDC en {tx_count} transacciones.",
        )
      elif intent == "create_charge":
        if pending is not None:
          append_message(
            conn,
            "bot",
            BOT_NAME,
            (
              "Ya existe un cobro pendiente. Usa 'Confirmar pago' o 'Cancelar' "
              "antes de crear otro."
            ),
          )
        else:
          request = create_charge_request(conn, actor, amount)
          append_message(
            conn,
            "bot",
            BOT_NAME,
            (
              f"Solicitud de pago creada: {request['amount_usdc']:.2f} USDC. "
              f"Token {request['token']}. Expira en 2 minutos."
            ),
            message_type="payment_request",
          )
      elif intent == "help":
        append_message(
          conn,
          "bot",
          BOT_NAME,
          "Escribe: 'cobrar 10 usd', 'pagar 10 usd' o 'ventas hoy'.",
        )
      else:
        append_message(
          conn,
          "bot",
          BOT_NAME,
          (
            "No entendi ese mensaje. Prueba con 'cobrar 10 usd', "
            "'pagar 10 usd' o 'ventas hoy'."
          ),
        )

      conn.commit()
      return build_state(conn)


def confirm_pending_payment():
  with DB_LOCK:
    with connect_db() as conn:
      expire_overdue_requests(conn)
      pending = get_pending_request(conn)
      if pending is None:
        return False, "No hay una solicitud pendiente para confirmar.", build_state(conn)

      if not PAYMENTS_ENABLED:
        append_message(conn, "bot", BOT_NAME, "Pagos deshabilitados temporalmente por el operador.")
        conn.commit()
        return False, "Pagos deshabilitados.", build_state(conn)

      customer = get_user(conn, "customer")
      merchant = get_user(conn, "merchant")
      amount = as_float(pending["amount_usdc"])

      provider_tx_hash = ""
      fee = as_float(amount * current_fee_rate())
      net = as_float(amount - fee)

      if WALLET_PROVIDER == "mock":
        if customer["balance_usdc"] < amount:
          append_message(
            conn,
            "bot",
            BOT_NAME,
            "Saldo insuficiente para completar el pago.",
          )
          conn.commit()
          return False, "Saldo insuficiente.", build_state(conn)

        conn.execute(
          """
          UPDATE users
          SET balance_usdc = ?
          WHERE role = 'customer'
          """,
          (as_float(customer["balance_usdc"] - amount),),
        )
        conn.execute(
          """
          UPDATE users
          SET balance_usdc = ?
          WHERE role = 'merchant'
          """,
          (as_float(merchant["balance_usdc"] + net),),
        )
      else:
        customer_wallet = get_wallet_account(conn, "customer")
        merchant_wallet = get_wallet_account(conn, "merchant")
        if customer_wallet is None or merchant_wallet is None:
          append_message(conn, "bot", BOT_NAME, "Wallet no configurada. Reinicia el servidor.")
          conn.commit()
          return False, "Wallet no configurada.", build_state(conn)

        try:
          onchain_balance = cdp_get_usdc_balance(customer_wallet["account_name"])
        except Exception as exc:
          append_message(conn, "bot", BOT_NAME, f"No pude validar saldo on-chain: {exc}")
          conn.commit()
          return False, "Error validando saldo on-chain.", build_state(conn)

        if onchain_balance < amount:
          append_message(
            conn,
            "bot",
            BOT_NAME,
            "Saldo USDC insuficiente en wallet para completar el pago.",
          )
          conn.commit()
          return False, "Saldo insuficiente en wallet.", build_state(conn)

        try:
          provider_tx_hash = cdp_transfer_usdc(
            customer_wallet["account_name"],
            merchant_wallet["address"],
            amount,
          )
        except Exception as exc:
          append_message(conn, "bot", BOT_NAME, f"Error enviando pago on-chain: {exc}")
          conn.commit()
          return False, "No se pudo enviar la transaccion on-chain.", build_state(conn)

        fee = 0.0
        net = amount
        conn.execute(
          """
          UPDATE users
          SET balance_usdc = ?
          WHERE role = 'customer'
          """,
          (as_float(onchain_balance - amount),),
        )
        conn.execute(
          """
          UPDATE users
          SET balance_usdc = ?
          WHERE role = 'merchant'
          """,
          (as_float(merchant["balance_usdc"] + net),),
        )

      tx_id = f"TX_{uuid4().hex[:8].upper()}"
      now_iso = to_iso(utc_now())

      conn.execute(
        """
        INSERT INTO transactions
          (id, payment_request_id, gross_amount_usdc, fee_usdc, net_amount_usdc,
           from_user_id, to_user_id, provider_tx_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          tx_id,
          pending["id"],
          amount,
          fee,
          net,
          customer["id"],
          merchant["id"],
          provider_tx_hash,
          now_iso,
        ),
      )
      conn.execute(
        """
        UPDATE payment_requests
        SET status = 'CONFIRMED', confirmed_at = ?
        WHERE id = ?
        """,
        (now_iso, pending["id"]),
      )

      if provider_tx_hash:
        explorer_url = explorer_tx_url(provider_tx_hash)
        tx_msg = f"Pago on-chain enviado: {amount:.2f} USDC. TX {provider_tx_hash}."
        if explorer_url:
          tx_msg += f" Explorer: {explorer_url}"
        append_message(conn, "bot", BOT_NAME, tx_msg, message_type="payment_success")
      else:
        append_message(
          conn,
          "bot",
          BOT_NAME,
          f"Pago exitoso: {amount:.2f} USDC. ID {tx_id}. Fee aplicado {fee:.2f} USDC.",
          message_type="payment_success",
        )

      total, _ = sales_today(conn)
      append_message(conn, "bot", BOT_NAME, f"Comercio, venta registrada. Total hoy: {total:.2f} USDC.")

      conn.commit()
      return True, "Pago confirmado.", build_state(conn)


def cancel_pending_payment():
  with DB_LOCK:
    with connect_db() as conn:
      expire_overdue_requests(conn)
      pending = get_pending_request(conn)
      if pending is None:
        return False, "No hay una solicitud pendiente para cancelar.", build_state(conn)

      conn.execute(
        """
        UPDATE payment_requests
        SET status = 'CANCELED', canceled_at = ?
        WHERE id = ?
        """,
        (to_iso(utc_now()), pending["id"]),
      )
      append_message(conn, "bot", BOT_NAME, "Solicitud cancelada.")
      conn.commit()
      return True, "Solicitud cancelada.", build_state(conn)


def reset_demo():
  with DB_LOCK:
    with connect_db() as conn:
      conn.execute("DELETE FROM chat_messages")
      conn.execute("DELETE FROM payment_requests")
      conn.execute("DELETE FROM transactions")

      if WALLET_PROVIDER == "mock":
        conn.execute("UPDATE users SET balance_usdc = 0 WHERE role = 'merchant'")
        conn.execute(
          "UPDATE users SET balance_usdc = ? WHERE role = 'customer'",
          (DEFAULT_CUSTOMER_BALANCE,),
        )
      else:
        refresh_wallet_balances(conn)

      seed_welcome_messages(conn)
      conn.commit()
      return build_state(conn)


def latest_bot_message_from_state(state: dict) -> str:
  for message in reversed(state.get("chat", [])):
    if message.get("senderRole") == "bot":
      return str(message.get("body") or "").strip()
  return "Listo."


def resolve_actor_from_whatsapp(conn: sqlite3.Connection, sender_phone: str):
  normalized_sender = normalize_phone(sender_phone)
  if not normalized_sender:
    return "customer"

  merchant_override = normalize_phone(WA_MERCHANT_PHONE)
  customer_override = normalize_phone(WA_CUSTOMER_PHONE)
  if merchant_override and normalized_sender == merchant_override:
    return "merchant"
  if customer_override and normalized_sender == customer_override:
    return "customer"

  for role in ("merchant", "customer"):
    user = get_user(conn, role)
    if normalize_phone(user["phone"]) == normalized_sender:
      return role

  # P2B default: any unknown sender is treated as customer, so no prior enrollment is required.
  return "customer"


def detect_whatsapp_action(text: str):
  normalized = normalize_text(text)
  if normalized in {"confirmar", "confirmo", "pagar ahora", "si", "sí", "ok"}:
    return "confirm"
  if normalized in {"cancelar", "anular", "cancel"}:
    return "cancel"
  return "chat"


def process_whatsapp_message(sender_phone: str, body: str, sender_name: str = ""):
  actor = ""
  state = {}
  with DB_LOCK:
    with connect_db() as conn:
      actor = resolve_actor_from_whatsapp(conn, sender_phone)
      if actor == "customer":
        normalized_sender = normalize_phone(sender_phone)
        if normalized_sender:
          customer = get_user(conn, "customer")
          if normalize_phone(customer["phone"]) != normalized_sender:
            conn.execute(
              """
              UPDATE users
              SET phone = ?
              WHERE role = 'customer'
              """,
              (f"+{normalized_sender}",),
            )
          if sender_name and str(sender_name).strip():
            conn.execute(
              """
              UPDATE users
              SET display_name = ?
              WHERE role = 'customer'
              """,
              (str(sender_name).strip(),),
            )
          conn.commit()

  action = detect_whatsapp_action(body)
  if action == "confirm":
    success, message, state = confirm_pending_payment()
    reply = message if success else f"No se pudo confirmar: {message}"
  elif action == "cancel":
    success, message, state = cancel_pending_payment()
    reply = message if success else f"No se pudo cancelar: {message}"
  else:
    state = process_chat_message(actor, body)
    reply = latest_bot_message_from_state(state)

  pending = state.get("pendingRequest")
  if pending:
    reply += (
      f"\nPendiente: {pending['amountUsdc']:.2f} USDC."
      " Responde CONFIRMAR o CANCELAR."
    )

  return reply, state


def twilio_send_whatsapp_message(to_phone: str, body: str):
  account_sid = TWILIO_ACCOUNT_SID
  auth_token = TWILIO_AUTH_TOKEN
  sender_from = TWILIO_WHATSAPP_FROM

  if not account_sid or not auth_token or not sender_from:
    missing = []
    if not account_sid:
      missing.append("TWILIO_ACCOUNT_SID")
    if not auth_token:
      missing.append("TWILIO_AUTH_TOKEN")
    if not sender_from:
      missing.append("TWILIO_WHATSAPP_FROM")
    raise RuntimeError(f"Faltan variables de entorno Twilio: {', '.join(missing)}")

  normalized_to = normalize_phone(to_phone)
  if not normalized_to:
    raise ValueError("Numero destino invalido.")

  message_body = str(body or "").strip()
  if not message_body:
    raise ValueError("Mensaje vacio.")

  from_value = sender_from if sender_from.lower().startswith("whatsapp:") else f"whatsapp:{sender_from}"
  to_value = f"whatsapp:+{normalized_to}"
  form = urlencode(
    {
      "From": from_value,
      "To": to_value,
      "Body": message_body,
    }
  ).encode("utf-8")

  auth_pair = f"{account_sid}:{auth_token}".encode("utf-8")
  auth_header = f"Basic {base64.b64encode(auth_pair).decode('ascii')}"
  url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
  request = Request(
    url,
    data=form,
    headers={
      "Authorization": auth_header,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    method="POST",
  )

  try:
    with urlopen(request, timeout=20) as response:
      raw = response.read()
      parsed = json.loads(raw.decode("utf-8")) if raw else {}
      return {
        "sid": str(parsed.get("sid", "")),
        "status": str(parsed.get("status", "")),
        "to": str(parsed.get("to", "")),
        "from": str(parsed.get("from", "")),
      }
  except HTTPError as exc:
    raw = exc.read().decode("utf-8", errors="replace")
    message = raw
    try:
      payload = json.loads(raw)
      message = str(payload.get("message") or payload.get("error_message") or raw)
    except Exception:
      pass
    raise RuntimeError(f"Twilio API error ({exc.code}): {message}") from exc
  except URLError as exc:
    raise RuntimeError(f"No se pudo conectar con Twilio: {exc}") from exc


def twilio_get_message(account_sid: str, auth_token: str, message_sid: str):
  if not account_sid or not auth_token or not message_sid:
    return {}

  auth_pair = f"{account_sid}:{auth_token}".encode("utf-8")
  auth_header = f"Basic {base64.b64encode(auth_pair).decode('ascii')}"
  url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages/{message_sid}.json"
  request = Request(url, headers={"Authorization": auth_header}, method="GET")

  with urlopen(request, timeout=20) as response:
    raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def build_state(conn: sqlite3.Connection, refresh_balances: bool = False):
  expire_overdue_requests(conn)

  provider_error = ""
  if refresh_balances:
    provider_error = refresh_wallet_balances(conn)

  merchant = get_user(conn, "merchant")
  customer = get_user(conn, "customer")
  pending = get_pending_request(conn)
  gross_today, tx_count = sales_today(conn)

  merchant_wallet = get_wallet_account(conn, "merchant")
  customer_wallet = get_wallet_account(conn, "customer")

  messages = conn.execute(
    """
    SELECT id, sender_role, sender_name, body, message_type, created_at
    FROM chat_messages
    ORDER BY id ASC
    """
  ).fetchall()

  response = {
    "wallet": {
      "provider": WALLET_PROVIDER,
      "network": CDP_NETWORK if WALLET_PROVIDER == "cdp" else "demo-local",
      "token": CDP_TOKEN.upper(),
      "paymentsEnabled": PAYMENTS_ENABLED,
      "error": provider_error,
    },
    "participants": {
      "merchant": {
        "name": merchant["display_name"],
        "phone": merchant["phone"],
        "balanceUsdc": as_float(merchant["balance_usdc"]),
        "walletAddress": merchant_wallet["address"] if merchant_wallet else "",
      },
      "customer": {
        "name": customer["display_name"],
        "phone": customer["phone"],
        "balanceUsdc": as_float(customer["balance_usdc"]),
        "walletAddress": customer_wallet["address"] if customer_wallet else "",
      },
    },
    "chat": [
      {
        "id": row["id"],
        "senderRole": row["sender_role"],
        "senderName": row["sender_name"],
        "body": row["body"],
        "messageType": row["message_type"],
        "createdAt": row["created_at"],
      }
      for row in messages
    ],
    "stats": {
      "salesTodayUsdc": gross_today,
      "transactionsToday": tx_count,
      "feeRate": current_fee_rate(),
    },
    "pendingRequest": None,
  }

  if pending is not None:
    response["pendingRequest"] = {
      "id": pending["id"],
      "amountUsdc": as_float(pending["amount_usdc"]),
      "token": pending["token"],
      "createdByRole": pending["created_by_role"],
      "expiresAt": pending["expires_at"],
      "status": pending["status"],
    }

  return response


def read_json(handler: SimpleHTTPRequestHandler):
  length = int(handler.headers.get("Content-Length", "0"))
  raw = handler.rfile.read(length) if length > 0 else b"{}"
  if not raw:
    return {}
  return json.loads(raw.decode("utf-8"))


def read_form(handler: SimpleHTTPRequestHandler):
  length = int(handler.headers.get("Content-Length", "0"))
  raw = handler.rfile.read(length) if length > 0 else b""
  if not raw:
    return {}
  parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
  return {key: values[-1] if values else "" for key, values in parsed.items()}


class MerchantPayHandler(SimpleHTTPRequestHandler):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, directory=str(BASE_DIR), **kwargs)

  def do_GET(self):
    parsed = urlparse(self.path)
    if parsed.path.startswith("/api/hubspot/"):
      self.handle_hubspot_get(parsed)
      return

    if parsed.path == "/api/state":
      with DB_LOCK:
        with connect_db() as conn:
          self.send_json(build_state(conn, refresh_balances=True), 200)
      return

    if parsed.path == "/health":
      self.send_json({"status": "ok", "time": to_iso(utc_now())}, 200)
      return

    if parsed.path == "/":
      self.path = "/merchant_pay.html"
    elif parsed.path in {"/inbox", "/inbox/"}:
      self.path = "/wa_inbox.html"
    elif parsed.path in {"/hubspot", "/hubspot/"}:
      self.path = "/hubspot.html"

    return super().do_GET()

  def do_POST(self):
    parsed = urlparse(self.path)

    if parsed.path == "/webhook/twilio/whatsapp":
      self.handle_twilio_whatsapp()
      return

    if parsed.path == "/api/chat":
      self.handle_chat()
      return

    if parsed.path == "/api/payment/confirm":
      success, message, state = confirm_pending_payment()
      self.send_json({"success": success, "message": message, "state": state}, 200 if success else 400)
      return

    if parsed.path == "/api/payment/cancel":
      success, message, state = cancel_pending_payment()
      self.send_json({"success": success, "message": message, "state": state}, 200 if success else 400)
      return

    if parsed.path == "/api/reset":
      state = reset_demo()
      self.send_json({"success": True, "state": state}, 200)
      return

    if parsed.path == "/api/whatsapp/send":
      self.handle_whatsapp_send()
      return

    self.send_json({"error": "Endpoint no encontrado."}, 404)

  def handle_chat(self):
    try:
      payload = read_json(self)
    except json.JSONDecodeError:
      self.send_json({"error": "JSON invalido."}, 400)
      return

    actor = str(payload.get("actor", "")).strip().lower()
    text = str(payload.get("text", "")).strip()

    if actor not in {"merchant", "customer"}:
      self.send_json({"error": "actor debe ser 'merchant' o 'customer'."}, 400)
      return

    if not text:
      self.send_json({"error": "text es obligatorio."}, 400)
      return

    state = process_chat_message(actor, text)
    self.send_json({"success": True, "state": state}, 200)

  def handle_twilio_whatsapp(self):
    payload = read_form(self)
    sender_phone = str(payload.get("From", "")).strip()
    body = str(payload.get("Body", "")).strip()
    sender_name = str(payload.get("ProfileName", "")).strip()

    if not body:
      self.send_twiml("Mensaje vacio. Escribe por ejemplo: cobrar 1 usd")
      return

    reply, _ = process_whatsapp_message(sender_phone, body, sender_name=sender_name)
    self.send_twiml(reply)

  def handle_whatsapp_send(self):
    try:
      payload = read_json(self)
    except json.JSONDecodeError:
      self.send_json({"error": "JSON invalido."}, 400)
      return

    to_phone = str(payload.get("to", "")).strip()
    body = str(payload.get("body", "")).strip()

    if not to_phone:
      self.send_json({"error": "to es obligatorio."}, 400)
      return
    if not body:
      self.send_json({"error": "body es obligatorio."}, 400)
      return

    try:
      twilio_result = twilio_send_whatsapp_message(to_phone, body)
      sid = str(twilio_result.get("sid", "")).strip()
      if sid:
        # Twilio can return queued and fail moments later. Poll once to expose final status to the UI.
        time.sleep(0.7)
        status_payload = twilio_get_message(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, sid)
        twilio_result["status"] = str(status_payload.get("status", twilio_result.get("status", "")))
        twilio_result["errorCode"] = status_payload.get("error_code")
        twilio_result["errorMessage"] = status_payload.get("error_message")

      status_lower = str(twilio_result.get("status", "")).strip().lower()
      if status_lower in {"failed", "undelivered"}:
        error_code = twilio_result.get("errorCode")
        error_message = twilio_result.get("errorMessage") or "Fallo enviando mensaje por Twilio."
        detail = f"Twilio status={status_lower}"
        if error_code:
          detail += f", code={error_code}"
        raise RuntimeError(f"{detail}. {error_message}")
    except Exception as exc:
      self.send_json({"error": str(exc)}, 400)
      return

    self.send_json({"success": True, "message": "Mensaje enviado.", "twilio": twilio_result}, 200)

  def handle_hubspot_get(self, parsed):
    query = parse_qs(parsed.query or "")
    force_refresh = first_query_value(query, "forceRefresh").lower() in {"1", "true", "yes", "on"}

    try:
      if parsed.path == "/api/hubspot/health":
        self.send_json(hubspot_health(), 200)
        return

      if parsed.path == "/api/hubspot/pipelines":
        object_type = first_query_value(query, "objectType", "deals").lower() or "deals"
        cache_query = {
          "objectType": object_type,
        }
        payload = cached_hubspot_read(
          hubspot_cache_key(parsed.path, cache_query),
          force_refresh,
          HUBSPOT_CACHE_TTL_SECONDS,
          lambda: hubspot_fetch_pipelines(object_type),
        )
        self.send_json(payload, 200)
        return

      if parsed.path == "/api/hubspot/deals":
        limit = parse_limit(first_query_value(query, "limit", "25"), default=25)
        after = first_query_value(query, "after")
        pipeline_id = first_query_value(query, "pipelineId")
        stage_id = first_query_value(query, "stageId")
        search_query = first_query_value(query, "q")
        cache_query = {
          "limit": limit,
          "after": after,
          "pipelineId": pipeline_id,
          "stageId": stage_id,
          "q": search_query,
        }
        payload = cached_hubspot_read(
          hubspot_cache_key(parsed.path, cache_query),
          force_refresh,
          HUBSPOT_CACHE_TTL_SECONDS,
          lambda: hubspot_fetch_deals(
            limit=limit,
            after=after,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            q=search_query,
          ),
        )
        self.send_json(payload, 200)
        return

      if parsed.path == "/api/hubspot/contacts":
        limit = parse_limit(first_query_value(query, "limit", "25"), default=25)
        after = first_query_value(query, "after")
        search_query = first_query_value(query, "q")
        cache_query = {
          "limit": limit,
          "after": after,
          "q": search_query,
        }
        payload = cached_hubspot_read(
          hubspot_cache_key(parsed.path, cache_query),
          force_refresh,
          HUBSPOT_CACHE_TTL_SECONDS,
          lambda: hubspot_fetch_contacts(limit=limit, after=after, q=search_query),
        )
        self.send_json(payload, 200)
        return

      if parsed.path == "/api/hubspot/companies":
        limit = parse_limit(first_query_value(query, "limit", "25"), default=25)
        after = first_query_value(query, "after")
        search_query = first_query_value(query, "q")
        cache_query = {
          "limit": limit,
          "after": after,
          "q": search_query,
        }
        payload = cached_hubspot_read(
          hubspot_cache_key(parsed.path, cache_query),
          force_refresh,
          HUBSPOT_CACHE_TTL_SECONDS,
          lambda: hubspot_fetch_companies(limit=limit, after=after, q=search_query),
        )
        self.send_json(payload, 200)
        return

      dynamic_match = re.match(r"^/api/hubspot/(deals|contacts|companies)/([^/]+)$", parsed.path)
      if dynamic_match:
        object_type, object_id = dynamic_match.groups()
        cache_query = {"object": object_type, "id": object_id}
        payload = cached_hubspot_read(
          hubspot_cache_key(parsed.path, cache_query),
          force_refresh,
          HUBSPOT_DETAIL_CACHE_TTL_SECONDS,
          lambda: hubspot_fetch_detail(object_type, object_id),
        )
        self.send_json(payload, 200)
        return

      self.send_json({"error": "Endpoint HubSpot no encontrado."}, 404)
    except HubSpotAPIError as exc:
      self.send_json(exc.to_payload(), exc.status_code)
    except Exception as exc:
      self.send_json(
        {
          "error": "Error interno consultando HubSpot.",
          "category": "HUBSPOT_INTERNAL_ERROR",
          "details": {"reason": str(exc)},
        },
        500,
      )

  def send_json(self, payload, status_code: int):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    self.send_response(status_code)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

  def send_twiml(self, body: str):
    escaped = html.escape(str(body or ""), quote=False)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escaped}</Message></Response>'
    raw = xml.encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "application/xml; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

  def send_twiml_empty(self):
    raw = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    self.send_response(200)
    self.send_header("Content-Type", "application/xml; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)


def bootstrap_wallet_layer() -> None:
  if WALLET_PROVIDER not in ALLOWED_WALLET_PROVIDERS:
    raise RuntimeError(
      f"WALLET_PROVIDER invalido: {WALLET_PROVIDER}. Usa 'mock' o 'cdp'."
    )

  with DB_LOCK:
    with connect_db() as conn:
      ensure_wallet_accounts(conn)
      if WALLET_PROVIDER == "cdp":
        refresh_wallet_balances(conn)
      conn.commit()


def main():
  initialize_db()
  try:
    bootstrap_wallet_layer()
  except Exception as exc:
    print(f"Error iniciando wallet provider '{WALLET_PROVIDER}': {exc}")
    print("Tip: copia .env.example a .env y completa credenciales CDP.")
    raise SystemExit(1) from exc

  port = int(os.environ.get("PORT", "8787"))
  server = ThreadingHTTPServer(("127.0.0.1", port), MerchantPayHandler)
  print(f"Servidor activo en http://127.0.0.1:{port}/")
  print(f"Wallet provider: {WALLET_PROVIDER} | network: {CDP_NETWORK if WALLET_PROVIDER == 'cdp' else 'demo-local'}")
  server.serve_forever()


if __name__ == "__main__":
  main()
