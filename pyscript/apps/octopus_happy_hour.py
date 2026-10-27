"""
Octopus Happy Hour (pyscript app)

Makes the most of Octopus Octoplus Happy Hours (and other Power Up / free
electricity sessions): while the electricity is free, the Solis battery
charges from the grid at full speed, the Cosy hot water is boosted, and you
get a push notification.

How it works
------------
- Watches the Octopus Energy integration's power up events entity. Booked
  sessions (its `events` attribute) are the happy hours; back-to-back
  sessions are merged into one window. Optionally it books available Weekend
  Happy Hours itself while you have tokens left (auto_join).
- A few minutes before a happy hour it writes one Solis charge slot (time,
  charge current, target SOC, switch on). The inverter starts and stops on
  its own, so if HA / pyscript / SolisCloud dies the battery still stops
  charging at the end of the happy hour.
- When the happy hour starts it:
    * boosts the heat pump hot water to `hot_water_temperature` until the
      end of the happy hour (the Cosy ends the boost itself),
    * runs any `start_actions` you've configured (scripts, switches, ...),
    * sends a push notification.
- When it ends it switches the charge slot off and clears it, runs any
  `end_actions` and (optionally) notifies again.

Only the configured charge slot is touched. Requires 6-slot Solis firmware
(HMI >= 4B00) with time-of-use (timed charge/discharge) enabled for battery
charging, and the Octopus Energy integration with Octoplus.
"""
import json
import re
import asyncio
import logging
import hashlib
import hmac
import base64
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from zoneinfo import ZoneInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession

log = logging.getLogger("pyscript.octopus_happy_hour")
log.setLevel(logging.DEBUG)

VERSION = "1.0.0"

# -----------------------------
# SolisCloud endpoints / constants
# -----------------------------
BASE_URL = "https://www.soliscloud.com:13333"
VERB = "POST"
LOGIN_URL = "/v2/api/login"
CONTROL_URL = "/v2/api/control"
AT_READ_URL = "/v2/api/atRead"
AT_READ_BATCH_URL = "/v2/api/atReadBatch"
INVERTER_LIST_URL = "/v1/api/inverterList"

# 6-slot firmware (HMI >= 4B00) charge slot CIDs, slot 1..6
CHARGE_TIME_CIDS = ["5946", "5949", "5952", "5955", "5958", "5961"]
CHARGE_CURRENT_CIDS = ["5948", "5951", "5954", "5957", "5960", "5963"]
CHARGE_SOC_CIDS = ["5928", "5929", "5930", "5931", "5932", "5933"]
STORAGE_MODE_CID = "636"

# Per-slot on/off switches ("1"/"0"). Writes must include "yuanzhi": a bitmask
# of all 12 switch states before the change, bit = index in SLOT_SWITCH_CIDS
# (charge slots 1-6 -> bits 0-5, discharge slots 1-6 -> bits 6-11).
CHARGE_SWITCH_CIDS = ["5916", "5917", "5918", "5919", "5920", "5921"]
DISCHARGE_SWITCH_CIDS = ["5922", "5923", "5924", "5925", "5926", "5927"]
SLOT_SWITCH_CIDS = CHARGE_SWITCH_CIDS + DISCHARGE_SWITCH_CIDS
EMPTY_SLOT = "00:00-00:00"

STATUS_SENSOR = "sensor.octopus_happy_hour"
ACTIVE_SENSOR = "binary_sensor.octopus_happy_hour_active"
OCTOPUS_DOMAIN = "octopus_energy"
JOIN_SERVICE = "join_octoplus_weekend_happy_hour_event"
BOOST_SERVICE = "boost_water_heater"

# -----------------------------
# Configuration (from configuration.yaml -> pyscript: apps: octopus_happy_hour:)
# -----------------------------
try:
    # Set when this file lives in <config>/pyscript/apps/
    CFG = pyscript.app_config or {}
except Exception:
    # Fallback if the file was placed directly in <config>/pyscript/
    CFG = (pyscript.config.get("apps") or {}).get("octopus_happy_hour") or {}
    log.warning("Not loaded as a pyscript app; move this file to <config>/pyscript/apps/")


def _cfg_bool(key, default):
    return str(CFG.get(key, default)).lower() in ("true", "1", "yes", "on")


def _cfg_list(key):
    value = CFG.get(key) or []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _cfg_actions(key):
    """
    A list of HA actions, each {service|action: "domain.name", data: {...},
    target: {...}}. Invalid entries are logged and dropped.
    """
    out = []
    value = CFG.get(key) or []
    if isinstance(value, dict):
        value = [value]
    for item in value:
        if isinstance(item, str):
            item = {"service": item}
        name = str((item.get("service") or item.get("action") or "") if isinstance(item, dict) else "")
        domain, _, service_name = name.partition(".")
        if not domain or not service_name:
            log.error("Ignoring %s entry %s: needs service: <domain>.<name>", key, item)
            continue
        out.append({
            "domain": domain,
            "service": service_name,
            "data": dict(item.get("data") or {}),
            "target": dict(item.get("target")) if item.get("target") else None,
        })
    return out


EVENTS_ENTITY = str(CFG.get("power_up_events_entity", ""))
# sensor.octopus_energy_<account>_octoplus_weekend_happy_hours, derived from the events entity.
_m = re.match(r"^event\.octopus_energy_(.+)_octoplus_power_up_events$", EVENTS_ENTITY)
TOKENS_ENTITY = str(CFG.get("weekend_happy_hours_entity",
                            f"sensor.octopus_energy_{_m.group(1)}_octoplus_weekend_happy_hours" if _m else ""))
AUTO_JOIN = _cfg_bool("auto_join", False)
JOIN_SELECTION = str(CFG.get("join_selection", "earliest")).lower()

# Battery (Solis)
BATTERY_CHARGE = _cfg_bool("battery_charge", bool(CFG.get("key_id")))
CHARGE_SLOT = int(CFG.get("charge_slot", 3))
CHARGE_CURRENT = int(CFG.get("charge_current", 100))
CHARGE_SOC = int(CFG.get("charge_soc", 100))
PREPARE_MINUTES = int(CFG.get("prepare_minutes", 5))

# Hot water (Cosy)
HEAT_PUMP_ID = str(CFG.get("heat_pump_id", ""))
WATER_HEATER = str(CFG.get("water_heater_entity",
                           f"water_heater.octopus_energy_heat_pump_{HEAT_PUMP_ID}" if HEAT_PUMP_ID else ""))
HOT_WATER_BOOST = _cfg_bool("hot_water_boost", bool(WATER_HEATER))
HOT_WATER_TEMPERATURE = float(CFG.get("hot_water_temperature", 50))

# Anything else that should run while the power is free.
START_ACTIONS = _cfg_actions("start_actions")
END_ACTIONS = _cfg_actions("end_actions")

# Notifications
NOTIFY_SERVICES = _cfg_list("notify_service")
NOTIFY_DATA = dict(CFG.get("notify_data") or {})
NOTIFY_SCHEDULED = _cfg_bool("notify_scheduled", True)
NOTIFY_END = _cfg_bool("notify_end", True)

INVERTER_TIMEZONE = str(CFG.get("inverter_timezone", hass.config.time_zone or "UTC"))
SYNC_INVERTER_TIME = _cfg_bool("sync_inverter_time", True)
STARTUP_DELAY_SECONDS = int(CFG.get("startup_delay_seconds", 120))

DIAGNOSTICS_ONLY = _cfg_bool("diagnostics_only", False)
CONTROL_RETRIES = int(CFG.get("control_retries", 3))
CONTROL_DELAY = float(CFG.get("control_delay", 0.5))
INTER_WRITE_DELAY = float(CFG.get("inter_write_delay", 0.25))
VERIFY_READBACK = _cfg_bool("verify_readback", False)

_missing_credentials = [k for k in ("key_id", "secret", "username", "password", "plantId") if not CFG.get(k)]
if BATTERY_CHARGE and _missing_credentials:
    log.error("battery_charge needs SolisCloud credentials (missing %s); battery charging is off",
              ", ".join(_missing_credentials))
    BATTERY_CHARGE = False
if BATTERY_CHARGE and not 1 <= CHARGE_SLOT <= 6:
    log.error("charge_slot must be 1-6, not %s; battery charging is off", CHARGE_SLOT)
    BATTERY_CHARGE = False
if HOT_WATER_BOOST and not WATER_HEATER:
    log.error("hot_water_boost needs heat_pump_id or water_heater_entity; hot water boost is off")
    HOT_WATER_BOOST = False

try:
    INV_TZ = ZoneInfo(INVERTER_TIMEZONE)
except Exception as e:
    log.warning("Invalid inverter_timezone '%s' (%s), falling back to UTC", INVERTER_TIMEZONE, e)
    INV_TZ = timezone.utc

# -----------------------------
# Runtime state (dicts so functions can mutate without `global`)
# -----------------------------
# What we last wrote to the charge slot. "unknown" means we don't know
# what's on the inverter (e.g. after a restart) and must write it.
_program = {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False, "unknown": True}
_cache = {"inverter_id": None, "inverter_sn": None, "last_time_sync": None}
_flags = {"busy": False, "started": False, "last_reason": None, "last_write_ok": None,
          "events_warned_at": None}
# Sessions we booked ourselves (id -> session), so we act on them even before
# the integration refreshes its events, and when a booking last failed (id -> time).
_joined = {"by_us": {}, "failed_at": {}}
# Happy hour windows (by id) we've announced, and the ones that have started
# but not finished (id -> {"window", "boost_until", ...}).
_seen = {"announced": set(), "running": {}}
_last = {"window": None}

# -----------------------------
# SolisCloud auth helpers (shared with solis_power_down)
# -----------------------------
def digest(body: str) -> str:
    return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")


def passwordEncode(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def prepare_header(config, body: str, canonicalized_resource: str):
    content_md5 = digest(body)
    content_type = "application/json"
    now = datetime.now(timezone.utc)
    date = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    encrypt_str = (
        VERB + "\n" + content_md5 + "\n" + content_type + "\n" + date + "\n" + canonicalized_resource
    )
    hmac_obj = hmac.new(
        config["secret"].encode("utf-8"),
        msg=encrypt_str.encode("utf-8"),
        digestmod=hashlib.sha1,
    )
    sign = base64.b64encode(hmac_obj.digest())
    authorization = "API " + str(config["key_id"]) + ":" + sign.decode("utf-8")
    return {
        "Content-MD5": content_md5,
        "Content-Type": content_type,
        "Date": date,
        "Authorization": authorization,
    }


def _clean_json_text(text: str) -> str:
    # SolisCloud sometimes returns JSON with trailing commas.
    return re.sub(r'("(?:\\?.)*?")|,\s*([}\]])', r"\1\2", text)


# -----------------------------
# SolisCloud I/O helpers (shared with solis_power_down)
# -----------------------------
async def solis_post(session, url_path, body_dict, token=None):
    body = json.dumps(body_dict, separators=(",", ":"))
    headers = prepare_header(CFG, body, url_path)
    if token:
        headers["token"] = token
    return await session.post(BASE_URL + url_path, data=body, headers=headers)


async def resp_json(resp):
    text = await resp.text()
    return json.loads(_clean_json_text(text))


async def solis_login(session):
    body = {"userInfo": str(CFG["username"]), "password": passwordEncode(str(CFG["password"]))}
    resp = await solis_post(session, LOGIN_URL, body)
    if resp.status != HTTPStatus.OK:
        log.error("Login failed with status %s", resp.status)
        return None
    data = await resp_json(resp)
    token = data.get("csrfToken")
    if not token:
        log.error("Login succeeded but csrfToken missing: %s", data)
    return token


async def get_inverter(session):
    """Return (inverter_id, inverter_sn), cached after the first lookup."""
    if _cache["inverter_id"] and _cache["inverter_sn"]:
        return _cache["inverter_id"], _cache["inverter_sn"]

    resp = await solis_post(session, INVERTER_LIST_URL, {"stationId": str(CFG["plantId"])})
    if resp.status != HTTPStatus.OK:
        log.error("inverterList failed status %s", resp.status)
        return None, None
    data = await resp_json(resp)
    records = (data.get("data") or {}).get("page", {}).get("records", []) or []
    if not records:
        log.error("No inverters returned from inverterList")
        return None, None

    cfg_sn = str(CFG.get("inverter_sn", "")).strip()
    cfg_id = str(CFG.get("inverter_id", "")).strip()
    if cfg_sn.lower() in ("unknown", "unavailable", "none"):
        cfg_sn = ""
    if cfg_id.lower() in ("unknown", "unavailable", "none"):
        cfg_id = ""

    chosen = None
    if cfg_sn or cfg_id:
        for r in records:
            if (cfg_sn and str(r.get("sn")) == cfg_sn) or (cfg_id and str(r.get("id")) == cfg_id):
                chosen = r
                break
    else:
        storage_records = []
        for r in records:
            if str(r.get("productModel")) == "2":
                storage_records.append(r)
        if len(storage_records) == 1:
            chosen = storage_records[0]
        elif len(storage_records) == 0 and len(records) == 1:
            chosen = records[0]

    if not chosen:
        log.error("Could not pick an inverter; set inverter_sn or inverter_id. Available:")
        for r in records:
            log.error("  - ID: %s, SN: %s, Name: %s, ProductModel: %s",
                      r.get("id"), r.get("sn"), r.get("name"), r.get("productModel"))
        return None, None

    _cache["inverter_id"] = chosen.get("id")
    _cache["inverter_sn"] = chosen.get("sn")
    log.info("Using inverter ID=%s SN=%s Name=%s",
             _cache["inverter_id"], _cache["inverter_sn"], chosen.get("name"))
    return _cache["inverter_id"], _cache["inverter_sn"]


async def get_control_value(session, token, inverter_sn, cid):
    """Read one CID. Returns its value as a string, or None."""
    for attempt in range(1, CONTROL_RETRIES + 1):
        r = await solis_post(session, AT_READ_URL,
                             {"inverterSn": str(inverter_sn), "cid": str(cid)}, token=token)
        if r.status != HTTPStatus.OK:
            await asyncio.sleep(0.2)
            continue
        try:
            data = await resp_json(r)
            payload = data.get("data")
            if isinstance(payload, list) and payload:
                payload = payload[0]
            if str(data.get("code")) == "0" and isinstance(payload, dict) and "msg" in payload:
                return str(payload["msg"])
        except Exception as e:
            log.warning("AT_READ cid=%s attempt %s parse error: %s", cid, attempt, e)
        await asyncio.sleep(0.2)
    return None


async def write_control(session, token, inverter_sn, cid, value, old_value=None):
    body = {"inverterSn": str(inverter_sn), "cid": str(cid), "value": str(value)}
    if old_value is not None:
        body["yuanzhi"] = str(old_value)
    last_text = None
    for attempt in range(1, CONTROL_RETRIES + 1):
        r = await solis_post(session, CONTROL_URL, body, token=token)
        try:
            last_text = await r.text()
        except Exception:
            last_text = None

        if r.status != HTTPStatus.OK:
            log.warning("CONTROL cid=%s attempt %s/%s http=%s", cid, attempt, CONTROL_RETRIES, r.status)
            await asyncio.sleep(0.3)
            continue
        try:
            data = json.loads(_clean_json_text(last_text or ""))
            payload = data.get("data") or []
            if str(data.get("code")) == "0" and payload and str(payload[0].get("code")) == "0":
                if VERIFY_READBACK:
                    await asyncio.sleep(CONTROL_DELAY)
                    rb = await get_control_value(session, token, inverter_sn, cid)
                    if rb is not None and rb != str(value):
                        log.warning("Readback cid=%s: wrote '%s' but read '%s'", cid, value, rb)
                    else:
                        log.debug("Readback cid=%s: %s", cid, rb)
                return True
        except Exception as e:
            log.warning("CONTROL cid=%s attempt %s/%s parse error: %s", cid, attempt, CONTROL_RETRIES, e)
        await asyncio.sleep(0.3)

    log.error("CONTROL cid=%s failed after %s attempts. Last response: %s", cid, CONTROL_RETRIES, last_text)
    return False


async def read_slot_switches(session, token, inverter_sn):
    """Return {cid: "0"/"1"} for the 12 slot switches; unreadable ones are left out."""
    states = {}
    try:
        r = await solis_post(session, AT_READ_BATCH_URL,
                             {"inverterSn": str(inverter_sn), "cids": ",".join(SLOT_SWITCH_CIDS)},
                             token=token)
        if r.status == HTTPStatus.OK:
            data = await resp_json(r)
            if str(data.get("code")) == "0":
                for outer in data.get("data") or []:
                    items = outer if isinstance(outer, list) else [outer]
                    for item in items:
                        if isinstance(item, dict) and "cid" in item and "msg" in item:
                            states[str(item["cid"])] = str(item["msg"])
        else:
            log.warning("atReadBatch for slot switches failed http=%s", r.status)
    except Exception as e:
        log.warning("atReadBatch for slot switches failed: %s", e)

    for cid in SLOT_SWITCH_CIDS:
        if cid not in states:
            value = await get_control_value(session, token, inverter_sn, cid)
            if value is not None:
                states[cid] = value
    log.debug("Slot switch states: %s", states)
    return states


def slot_switch_mask(states):
    """The "yuanzhi" bitmask for the current switch states, or None if any is unknown."""
    mask = 0
    for bit, cid in enumerate(SLOT_SWITCH_CIDS):
        if cid not in states:
            return None
        if states[cid] == "1":
            mask |= 1 << bit
    return str(mask)


async def set_slot_switch(session, token, inverter_sn, states, cid, enabled):
    """Switch a slot on/off if it isn't already. Updates `states` on success."""
    want = "1" if enabled else "0"
    if states.get(cid) == want:
        return True
    old_value = slot_switch_mask(states)
    if old_value is None:
        log.warning("Slot switch states unknown; writing CID %s without yuanzhi", cid)
    log.info("%s charge slot %s (CID %s)", "Enabling" if enabled else "Disabling", CHARGE_SLOT, cid)
    ok = await write_control(session, token, inverter_sn, cid, want, old_value=old_value)
    if ok:
        states[cid] = want
    return ok


async def sync_inverter_time(session, token, inverter_id):
    value = datetime.now(INV_TZ).strftime("%Y-%m-%d %H:%M:%S")
    body = f'{{"inverterId":"{inverter_id}","cid":"56","value":"{value}"}}'
    headers = prepare_header(CFG, body, CONTROL_URL)
    headers["token"] = token
    try:
        resp = await session.post(BASE_URL + CONTROL_URL, data=body, headers=headers)
        if resp.status == HTTPStatus.OK:
            data = await resp_json(resp)
            if str(data.get("code")) == "0":
                log.info("Synced inverter time to %s (%s)", value, INVERTER_TIMEZONE)
                _cache["last_time_sync"] = datetime.now(timezone.utc)
                return
            log.warning("Time sync returned code=%s msg=%s", data.get("code"), data.get("msg"))
        else:
            log.warning("Time sync HTTP status: %s", resp.status)
    except Exception as e:
        log.error("Time sync failed: %s", e)


async def solis_connect():
    """Log in and pick the inverter. Returns (session, token, inverter_id, inverter_sn) or None."""
    session = async_get_clientsession(hass)
    token = await solis_login(session)
    if not token:
        return None
    inverter_id, inverter_sn = await get_inverter(session)
    if not inverter_sn:
        return None
    return session, token, inverter_id, inverter_sn


async def solis_apply(ops):
    """
    Apply charge slot changes in order. `ops` is a list of
    ("value", cid, value) and ("switch", enabled). Returns True if all succeeded.
    """
    if not ops:
        return True
    if DIAGNOSTICS_ONLY:
        log.warning("DIAGNOSTICS MODE: would write %s", ops)
        return True

    conn = await solis_connect()
    if conn is None:
        return False
    session, token, inverter_id, inverter_sn = conn

    if SYNC_INVERTER_TIME:
        last = _cache["last_time_sync"]
        if last is None or datetime.now(timezone.utc) - last > timedelta(hours=24):
            await sync_inverter_time(session, token, inverter_id)

    switches = None
    for op in ops:
        if op[0] == "switch" and switches is None:
            switches = await read_slot_switches(session, token, inverter_sn)

    ok = True
    switch_cid = CHARGE_SWITCH_CIDS[CHARGE_SLOT - 1]
    for op in ops:
        if op[0] == "switch":
            if not await set_slot_switch(session, token, inverter_sn, switches, switch_cid, op[1]):
                ok = False
                # Never switch on a slot whose settings didn't all land.
                break
        else:
            log.info("Writing charge slot %s CID %s = %s", CHARGE_SLOT, op[1], op[2])
            if not await write_control(session, token, inverter_sn, op[1], op[2]):
                ok = False
                break
        await asyncio.sleep(INTER_WRITE_DELAY)
    return ok



async def log_storage_mode():
    """Read the storage mode once so the log shows whether TOU is likely enabled."""
    if DIAGNOSTICS_ONLY or not BATTERY_CHARGE:
        return
    try:
        conn = await solis_connect()
        if conn is None:
            return
        session, token, _, inverter_sn = conn
        value = await get_control_value(session, token, inverter_sn, STORAGE_MODE_CID)
        log.info("Inverter storage mode (CID %s) = %s. Time-of-use / timed charge-discharge "
                 "must be enabled for charge slots to work.", STORAGE_MODE_CID, value)
    except Exception as e:
        log.warning("Could not read storage mode: %s", e)


# -----------------------------
# Time helpers
# -----------------------------
def now_local():
    return datetime.now(INV_TZ)


def to_local(dt):
    return dt.astimezone(INV_TZ)


def parse_dt(value):
    """Datetime or ISO string -> tz-aware datetime (naive is taken as UTC), or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_window(start, end):
    return f"{to_local(start).strftime('%H:%M')}-{to_local(end).strftime('%H:%M')}"


def fmt_day(dt):
    return to_local(dt).strftime("%a %d %b")


# -----------------------------
# HA state helpers
# -----------------------------
def _state_obj(entity_id):
    if not entity_id:
        return None
    return hass.states.get(entity_id)


def _float_state(entity_id):
    s = _state_obj(entity_id)
    if s is None:
        return None
    try:
        return float(s.state)
    except (TypeError, ValueError):
        return None


def _attrs(entity_id):
    s = _state_obj(entity_id)
    if s is None:
        return {}
    return dict(getattr(s, "attributes", None) or {})


# -----------------------------
# Happy hours
# -----------------------------
def _session_from_event(ev):
    if not isinstance(ev, dict):
        return None
    start = parse_dt(ev.get("start"))
    end = parse_dt(ev.get("end"))
    if start is None or end is None or end <= start:
        return None
    return {
        "id": str(ev.get("id")),
        "code": ev.get("code"),
        "start": start,
        "end": end,
        "availability": str(ev.get("availability") or ""),
    }


def sessions():
    """(booked, available) sessions from the power up events entity."""
    attrs = _attrs(EVENTS_ENTITY)
    booked = []
    seen = set()
    for ev in attrs.get("events") or []:
        s = _session_from_event(ev)
        if s is not None:
            booked.append(s)
            seen.add(s["id"])
    for sid, s in _joined["by_us"].items():
        if sid not in seen:
            booked.append(s)
            seen.add(sid)

    available = []
    for ev in attrs.get("available_events") or []:
        s = _session_from_event(ev)
        if s is not None and s["id"] not in seen:
            available.append(s)
    return booked, available


def _by_start(items):
    ordered = sorted([(s["start"].timestamp(), i) for i, s in enumerate(items)])
    return [items[i] for _, i in ordered]


def happy_hours():
    """Booked sessions as windows sorted by start, with overlapping / back-to-back ones merged."""
    booked, _ = sessions()
    windows = []
    for s in _by_start(booked):
        if windows and s["start"] <= windows[-1]["end"]:
            if s["end"] > windows[-1]["end"]:
                windows[-1]["end"] = s["end"]
            windows[-1]["sessions"] += 1
            continue
        windows.append({"id": to_local(s["start"]).strftime("%Y-%m-%dT%H:%M"),
                        "start": s["start"], "end": s["end"], "sessions": 1})
    return windows


def target_window(windows, now):
    """The happy hour running now, or else the next one."""
    for w in windows:
        if w["end"] > now:
            return w
    return None


def is_running(window, now):
    return window is not None and window["start"] <= now < window["end"]


def events_entity_status():
    """"ok", "not_configured" or "missing" for the power up events entity."""
    if not EVENTS_ENTITY:
        return "not_configured"
    if hass.states.get(EVENTS_ENTITY) is None:
        return "missing"
    return "ok"


def events_entity_warning(status):
    if status == "missing":
        return (f"Power up events entity '{EVENTS_ENTITY}' doesn't exist, so happy hours are ignored. "
                f"It's disabled by default: enable it, then check power_up_events_entity against "
                f"Developer Tools -> States.")
    if status == "not_configured":
        return "power_up_events_entity isn't set, so happy hours are ignored."
    return None


def warn_events_entity(status):
    """Log a missing/unset events entity at most once an hour."""
    if status == "ok":
        _flags["events_warned_at"] = None
        return
    now = datetime.now(timezone.utc)
    last = _flags["events_warned_at"]
    if last is not None and now - last < timedelta(hours=1):
        return
    _flags["events_warned_at"] = now
    if status == "missing":
        log.error(events_entity_warning(status))
    else:
        log.warning(events_entity_warning(status))


def tokens_available():
    """Weekend happy hour tokens left, or None if unknown."""
    return _float_state(TOKENS_ENTITY)


async def auto_join():
    """Book one available weekend happy hour (earliest or latest) if there's a token for it."""
    if not AUTO_JOIN or events_entity_status() != "ok":
        return
    tokens = tokens_available()
    if tokens is not None and tokens < 1:
        log.debug("No weekend happy hour tokens left; not joining")
        return
    now = now_local()
    _, available = sessions()
    candidates = []
    for s in available:
        if s["start"] <= now or not s["code"] or s["availability"].lower() == "full":
            continue
        failed = _joined["failed_at"].get(s["id"])
        if failed is not None and now - failed < timedelta(hours=1):
            continue
        candidates.append(s)
    if not candidates:
        return
    candidates = _by_start(candidates)
    s = candidates[-1] if JOIN_SELECTION == "latest" else candidates[0]

    log.info("Booking weekend happy hour %s %s (code %s, %s tokens left)",
             fmt_day(s["start"]), fmt_window(s["start"], s["end"]), s["code"], tokens)
    try:
        await hass.services.async_call(
            OCTOPUS_DOMAIN, JOIN_SERVICE, {"event_code": s["code"]},
            blocking=True, target={"entity_id": EVENTS_ENTITY},
        )
    except Exception as e:
        _joined["failed_at"][s["id"]] = now
        log.error("Failed to book happy hour %s: %s (will retry in an hour)", s["code"], e)
        return
    _joined["by_us"][s["id"]] = s


# -----------------------------
# Notifications and actions
# -----------------------------
async def notify(title, message):
    for target in NOTIFY_SERVICES:
        domain, _, name = target.partition(".")
        if not name:
            domain, name = "notify", domain
        data = {"title": title, "message": message}
        if NOTIFY_DATA:
            data["data"] = dict(NOTIFY_DATA)
        try:
            await hass.services.async_call(domain, name, data, blocking=False)
        except Exception as e:
            log.warning("Notify via %s failed: %s", target, e)


async def run_actions(actions, label):
    """Call each configured action. Failures are logged and don't stop the rest."""
    for a in actions:
        if DIAGNOSTICS_ONLY:
            log.warning("DIAGNOSTICS MODE: would call %s.%s data=%s target=%s (%s)",
                        a["domain"], a["service"], a["data"], a["target"], label)
            continue
        log.info("Happy hour %s: calling %s.%s", label, a["domain"], a["service"])
        try:
            await hass.services.async_call(a["domain"], a["service"], dict(a["data"]),
                                           blocking=False, target=a["target"])
        except Exception as e:
            log.error("Happy hour %s action %s.%s failed: %s", label, a["domain"], a["service"], e)


def plan_summary():
    """What happens during a happy hour, for notifications."""
    parts = []
    if BATTERY_CHARGE:
        parts.append(f"battery charging to {CHARGE_SOC}%")
    if HOT_WATER_BOOST:
        parts.append(f"hot water boosting to {HOT_WATER_TEMPERATURE:g}°C")
    if START_ACTIONS:
        parts.append(f"{len(START_ACTIONS)} other action{'s' if len(START_ACTIONS) != 1 else ''}")
    if not parts:
        return ""
    text = ", ".join(parts)
    return text[0].upper() + text[1:] + "."


def _diag_suffix():
    return " (diagnostics only: nothing will be changed)" if DIAGNOSTICS_ONLY else ""


# -----------------------------
# Hot water
# -----------------------------
def boost_minutes(window, now):
    """
    Boost length in whole 15 minutes (the service's step), up to the end of the
    window. A run a minute or two late still rounds up to the full length.
    """
    remaining = (window["end"] - max(now, window["start"])).total_seconds() / 60.0
    return min(12 * 60, int((remaining + 2) // 15) * 15)


async def boost_hot_water(window, now):
    """Boost the water heater until the end of the window. Returns when the boost ends, or None."""
    if not HOT_WATER_BOOST:
        return None
    minutes = boost_minutes(window, now)
    if minutes < 15:
        log.info("Less than 15 minutes of happy hour left; not boosting hot water")
        return None
    hours, mins = divmod(minutes, 60)
    data = {"hours": hours, "minutes": mins, "target_temperature": HOT_WATER_TEMPERATURE}
    if DIAGNOSTICS_ONLY:
        log.warning("DIAGNOSTICS MODE: would call %s.%s on %s with %s",
                    OCTOPUS_DOMAIN, BOOST_SERVICE, WATER_HEATER, data)
    else:
        log.info("Boosting %s to %s°C for %s min", WATER_HEATER, HOT_WATER_TEMPERATURE, minutes)
        try:
            await hass.services.async_call(OCTOPUS_DOMAIN, BOOST_SERVICE, data,
                                           blocking=True, target={"entity_id": WATER_HEATER})
        except Exception as e:
            log.error("Hot water boost on %s failed: %s", WATER_HEATER, e)
            return None
    return max(now, window["start"]) + timedelta(minutes=minutes)


# -----------------------------
# Window start / end
# -----------------------------
async def announce_new(windows, now):
    """Notify about happy hours we haven't seen before (not after a restart: see seed())."""
    for w in windows:
        if w["end"] <= now or w["id"] in _seen["announced"]:
            continue
        _seen["announced"].add(w["id"])
        if NOTIFY_SCHEDULED and w["start"] > now:
            await notify("Happy Hour booked",
                         f"{fmt_day(w['start'])} {fmt_window(w['start'], w['end'])}. "
                         f"{plan_summary()}{_diag_suffix()}")


async def start_window(window, now):
    """Run the start of a happy hour once; top up the boost if the window grew."""
    rec = _seen["running"].get(window["id"])
    if rec is not None:
        rec["window"] = window
        if HOT_WATER_BOOST and not rec["resumed"] and rec["boost_attempts"] < 3:
            until = rec["boost_until"]
            if until is None or window["end"] - until >= timedelta(minutes=15):
                rec["boost_attempts"] += 1
                rec["boost_until"] = await boost_hot_water(window, now) or until
        return

    rec = {"window": window, "boost_until": None, "boost_attempts": 1, "resumed": False}
    _seen["running"][window["id"]] = rec
    log.info("Happy hour %s started", fmt_window(window["start"], window["end"]))
    rec["boost_until"] = await boost_hot_water(window, now)
    await run_actions(START_ACTIONS, "start")
    summary = plan_summary()
    await notify("Happy Hour started",
                 f"Free electricity until {to_local(window['end']).strftime('%H:%M')}."
                 f"{' ' + summary if summary else ''}{_diag_suffix()}")


async def finish_ended(windows, now):
    """Run the end of every started happy hour that has finished (or been cancelled)."""
    current = {}
    for w in windows:
        current[w["id"]] = w
    for wid in list(_seen["running"].keys()):
        w = current.get(wid)
        if w is not None and now < w["end"]:
            continue
        rec = _seen["running"].pop(wid)
        log.info("Happy hour %s ended", fmt_window(rec["window"]["start"], rec["window"]["end"]))
        await run_actions(END_ACTIONS, "end")
        if NOTIFY_END:
            await notify("Happy Hour over", f"The {fmt_window(rec['window']['start'], rec['window']['end'])} "
                                            f"happy hour has finished.")


def seed(now):
    """
    After a restart, treat known happy hours as already announced, and one
    that's already running as already started (its boost is still running on
    the Cosy and the start actions have run), so nothing fires twice.
    """
    for w in happy_hours():
        if w["end"] <= now:
            continue
        _seen["announced"].add(w["id"])
        if is_running(w, now):
            _seen["running"][w["id"]] = {"window": w, "boost_until": None, "boost_attempts": 0,
                                         "resumed": True}
    _flags["started"] = True


# -----------------------------
# Battery: make the charge slot match the window
# -----------------------------
def desired_program(window, now):
    """What the charge slot should hold right now."""
    empty = {"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}
    if not BATTERY_CHARGE or window is None:
        return empty
    if now < window["start"] - timedelta(minutes=PREPARE_MINUTES) or now >= window["end"]:
        return empty
    return {
        "window": fmt_window(window["start"], window["end"]),
        "current": CHARGE_CURRENT,
        "soc": CHARGE_SOC,
        "enabled": True,
    }


def ops_for(want):
    """The writes that take the inverter from `_program` to `want`, in a safe order."""
    have = _program
    unknown = have["unknown"]
    slot = CHARGE_SLOT - 1
    ops = []

    if not want["enabled"]:
        if unknown or have["enabled"]:
            ops.append(("switch", False))
        if unknown or have["window"] != EMPTY_SLOT:
            ops.append(("value", CHARGE_TIME_CIDS[slot], EMPTY_SLOT))
        return ops

    fresh = unknown or not have["enabled"] or have["window"] != want["window"]
    if fresh:
        ops.append(("value", CHARGE_TIME_CIDS[slot], want["window"]))
    if fresh or want["current"] != have["current"]:
        ops.append(("value", CHARGE_CURRENT_CIDS[slot], str(want["current"])))
    if fresh or want["soc"] != have["soc"]:
        ops.append(("value", CHARGE_SOC_CIDS[slot], str(want["soc"])))
    if fresh:
        ops.append(("switch", True))
    return ops


def _apply_ops_to_program(ops):
    slot = CHARGE_SLOT - 1
    for op in ops:
        if op[0] == "switch":
            _program["enabled"] = op[1]
        elif op[1] == CHARGE_TIME_CIDS[slot]:
            _program["window"] = op[2]
        elif op[1] == CHARGE_CURRENT_CIDS[slot]:
            _program["current"] = int(op[2])
        elif op[1] == CHARGE_SOC_CIDS[slot]:
            _program["soc"] = int(op[2])
    if not _program["enabled"] and _program["window"] == EMPTY_SLOT:
        _program["current"] = None
        _program["soc"] = None
    _program["unknown"] = False


async def update_charge_slot(window, now, reason):
    if not BATTERY_CHARGE:
        return
    want = desired_program(window, now)
    ops = ops_for(want)
    if not ops:
        log.debug("[%s] no change (charge slot=%s)", reason, _program["window"])
        return
    log.info("[%s] charge slot %s -> %s (current=%s soc=%s) via %s",
             reason, CHARGE_SLOT, want["window"], want["current"], want["soc"], ops)
    ok = await solis_apply(ops)
    _flags["last_write_ok"] = ok
    if ok:
        _apply_ops_to_program(ops)
    else:
        # Partial writes leave the inverter in an unknown state; rewrite it all next time.
        _program["unknown"] = True
        log.error("Failed to write charge slot; will retry on next check")


# -----------------------------
# Reconcile
# -----------------------------
async def reconcile(reason):
    if not _flags["started"]:
        # Wait for startup, so other apps (e.g. EV-Follow clearing its slots) go first.
        log.debug("[%s] not started yet", reason)
        return
    # Serialise: pyscript runs on the event loop, so check-and-set is atomic.
    while _flags["busy"]:
        await asyncio.sleep(1)
    _flags["busy"] = True
    try:
        now = now_local()
        events_status = events_entity_status()
        warn_events_entity(events_status)
        windows = happy_hours()
        window = target_window(windows, now)

        await announce_new(windows, now)
        await update_charge_slot(window, now, reason)
        await finish_ended(windows, now)
        if is_running(window, now):
            await start_window(window, now)

        _last["window"] = window
        _flags["last_reason"] = reason
        publish_status(window, windows, now, events_status)
    finally:
        _flags["busy"] = False


def publish_status(window, windows, now, events_status="ok"):
    running = is_running(window, now)
    if window is None:
        status = "idle"
    elif running:
        status = "active"
    elif _program["enabled"]:
        status = "armed"
    else:
        status = "scheduled"

    rec = _seen["running"].get(window["id"]) if running else None
    upcoming = []
    for w in windows:
        if w["end"] > now:
            upcoming.append(f"{fmt_day(w['start'])} {fmt_window(w['start'], w['end'])}")

    attrs = {
        "window": fmt_window(window["start"], window["end"]) if window else None,
        "start": window["start"].isoformat() if window else None,
        "end": window["end"].isoformat() if window else None,
        "minutes_to_start": max(0, int((window["start"] - now).total_seconds() // 60)) if window else None,
        "booked": upcoming,
        "tokens": tokens_available(),
        "auto_join": AUTO_JOIN,
        "battery_charge": BATTERY_CHARGE,
        "charge_slot": CHARGE_SLOT if BATTERY_CHARGE else None,
        "programmed_window": _program["window"] if BATTERY_CHARGE else None,
        "programmed_current": _program["current"] if BATTERY_CHARGE else None,
        "programmed_soc": _program["soc"] if BATTERY_CHARGE else None,
        "slot_enabled": _program["enabled"] if BATTERY_CHARGE else None,
        "slot_state_unknown": _program["unknown"] if BATTERY_CHARGE else None,
        "last_write_ok": _flags["last_write_ok"],
        "hot_water_boost": HOT_WATER_BOOST,
        "water_heater": WATER_HEATER or None,
        "hot_water_temperature": HOT_WATER_TEMPERATURE if HOT_WATER_BOOST else None,
        "boost_until": rec["boost_until"].isoformat() if rec and rec["boost_until"] else None,
        "start_actions": len(START_ACTIONS),
        "end_actions": len(END_ACTIONS),
        "last_reason": _flags["last_reason"],
        "power_up_events": events_status,
        "diagnostics_only": DIAGNOSTICS_ONLY,
        "version": VERSION,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "friendly_name": "Octopus Happy Hour",
        "icon": "mdi:glass-cocktail",
    }
    warning = events_entity_warning(events_status)
    if warning:
        attrs["warning"] = warning
    state.set(STATUS_SENSOR, status, new_attributes=attrs)
    state.set(ACTIVE_SENSOR, "on" if running else "off", new_attributes={
        "friendly_name": "Octopus Happy Hour active",
        "icon": "mdi:glass-cocktail",
    })


# -----------------------------
# Triggers
# -----------------------------
@time_trigger("startup")
async def octopus_happy_hour_startup():
    log.info("=== Octopus Happy Hour v%s starting (battery_charge=%s slot %s, hot_water_boost=%s, "
             "auto_join=%s, diagnostics_only=%s) ===", VERSION, BATTERY_CHARGE, CHARGE_SLOT,
             HOT_WATER_BOOST, AUTO_JOIN, DIAGNOSTICS_ONLY)
    if not EVENTS_ENTITY or hass.states.get(EVENTS_ENTITY) is None:
        log.warning("Power up events entity '%s' not found yet - check power_up_events_entity "
                    "(it's disabled by default in the Octopus integration)", EVENTS_ENTITY)
    if HOT_WATER_BOOST and hass.states.get(WATER_HEATER) is None:
        log.warning("Water heater '%s' not found yet - check heat_pump_id / water_heater_entity",
                    WATER_HEATER)
    # Let the Octopus / Solis integrations (and EV-Follow's slot initialisation) finish first.
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    seed(now_local())
    await log_storage_mode()
    await auto_join()
    # _program starts unknown, so this also clears any leftover slot.
    await reconcile("startup")


# Not a state_trigger: in pyscript expressions `event` is its own built-in,
# so `event.<entity>` can't be used there. state_changed covers state and
# attribute changes for any domain.
@event_trigger("state_changed", f"entity_id == '{EVENTS_ENTITY}'")
async def octopus_happy_hour_events_changed(entity_id=None, **kwargs):
    task.unique("octopus_happy_hour_events", kill_me=True)
    await auto_join()
    await reconcile("events changed")


@time_trigger("cron(* * * * *)")
async def octopus_happy_hour_periodic():
    # Every minute so the start actions and notification are on time. Only
    # changes are written to the inverter, so this costs nothing otherwise.
    await reconcile("periodic")


@time_trigger("cron(7 * * * *)")
async def octopus_happy_hour_join_sweep():
    await auto_join()


# -----------------------------
# Services (for testing from Developer Tools -> Actions)
# -----------------------------
@service
async def octopus_happy_hour_reconcile():
    """Book (if auto_join), re-check happy hours and update the Solis charge slot now."""
    await auto_join()
    await reconcile("manual")


@service
async def octopus_happy_hour_clear():
    """Switch off and clear the charge slot (it is rewritten if a happy hour is still due)."""
    if not BATTERY_CHARGE:
        return
    _program["unknown"] = True
    ok = await solis_apply(ops_for({"window": EMPTY_SLOT, "current": None, "soc": None, "enabled": False}))
    if ok:
        _program.update({"window": EMPTY_SLOT, "current": None, "soc": None,
                         "enabled": False, "unknown": False})


@service
async def octopus_happy_hour_preview():
    """Log the booked and available happy hours and what will happen, without changing anything."""
    now = now_local()
    windows = happy_hours()
    _, available = sessions()
    window = target_window(windows, now)
    upcoming = []
    for w in windows:
        if w["end"] > now:
            upcoming.append(f"{fmt_day(w['start'])} {fmt_window(w['start'], w['end'])}")
    bookable = []
    for s in _by_start(available):
        if s["end"] > now:
            bookable.append(f"{fmt_day(s['start'])} {fmt_window(s['start'], s['end'])} "
                            f"({s['code']}, {s['availability'] or 'unknown'})")
    log.info("Preview: booked=%s available=%s tokens=%s", upcoming or "none", bookable or "none",
             tokens_available())
    if window is None:
        log.info("Preview: no happy hour booked")
        return
    log.info("Preview for %s %s: charge slot %s, hot water boost %s, %s start / %s end actions. %s",
             fmt_day(window["start"]), fmt_window(window["start"], window["end"]),
             desired_program(window, window["start"]) if BATTERY_CHARGE else "off",
             f"{boost_minutes(window, window['start'])} min at {HOT_WATER_TEMPERATURE:g}°C"
             if HOT_WATER_BOOST else "off",
             len(START_ACTIONS), len(END_ACTIONS), plan_summary())
