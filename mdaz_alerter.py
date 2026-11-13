#!/usr/bin/env python3
"""
MDAZ (FP 11) order-alerter — office-PC edition.

What it does, once per run:
  1. Fetches FP 11 pending orders from the Control Room API (the same request
     shape your AWS collector uses, which works from this network).
  2. Classifies each order's stage and how long it's been stuck there.
  3. Posts a Google Chat message for orders past the threshold — once per
     order+stage, so it doesn't repeat every 5 minutes.

It never posts customer names, phones, addresses, coordinates, or products —
only order number, restaurant, delivery associate, stage, and minutes waiting.

Secrets (token + webhook URL) come from config.json beside this file, or from
environment variables. Nothing sensitive is hardcoded. Keep config.json on this
PC only — do not upload it anywhere.

Run modes:
  python mdaz_alerter.py --test     one request, prints status + count, posts nothing
  python mdaz_alerter.py --sample   prints field names of one order (to verify the schema)
  python mdaz_alerter.py --dry-run  full run, prints the alerts it WOULD send, posts nothing
  python mdaz_alerter.py            full run, posts to Chat (unless DRY_RUN=true in config)

Requires: pip install requests tzdata
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests tzdata")

try:
    from zoneinfo import ZoneInfo
    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:
    sys.exit("Cairo timezone unavailable. On Windows run:  pip install tzdata")

HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DEFAULTS = {
    "FP_ID": "11",
    "CONTROL_ROOM_BASE": "",   # set via CONTROL_ROOM_BASE secret/env or config.json
    "PAGE_LIMIT": 100,
    "MAX_PAGES": 25,
    "TIMEOUT_SECONDS": 20,
    # Minutes an order can sit in a stage before it's flagged.
    "UNASSIGNED_THRESHOLD_MIN": 10,   # pending, no delivery associate yet
    "FIRST_MILE_THRESHOLD_MIN": 20,   # DA heading to / at restaurant, not picked up
    "LAST_MILE_THRESHOLD_MIN": 20,    # order picked up / out for delivery, not delivered
    # Re-alert an order that's still breaching after this many minutes (0 = alert once only).
    "REALERT_MIN": 30,
    "STATE_FILE": os.path.join(HERE, "mdaz_alert_state.json"),
    "DRY_RUN": True,                  # flip to false once you've confirmed the alerts look right
    "MAX_ALERTS_PER_MESSAGE": 25,
}


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception as exc:
            sys.exit(f"Could not read config.json: {exc}")
    # Environment variables override config.json (used by GitHub Actions).
    for key in ("BREADFAST_BEARER_TOKEN", "GOOGLE_CHAT_WEBHOOK_URL", "FP_ID", "STATE_FILE", "CONTROL_ROOM_BASE"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    # Numeric knobs, overridable from env without editing code.
    for key in ("UNASSIGNED_THRESHOLD_MIN", "FIRST_MILE_THRESHOLD_MIN",
                "LAST_MILE_THRESHOLD_MIN", "REALERT_MIN", "PAGE_LIMIT",
                "MAX_PAGES", "TIMEOUT_SECONDS", "MAX_ALERTS_PER_MESSAGE"):
        if os.environ.get(key):
            try:
                cfg[key] = type(DEFAULTS[key])(os.environ[key])
            except (TypeError, ValueError):
                pass
    # DRY_RUN as a real boolean from env ("false" must mean post for real).
    if os.environ.get("DRY_RUN") is not None:
        cfg["DRY_RUN"] = os.environ["DRY_RUN"].strip().lower() in ("1", "true", "yes", "on")
    token = str(cfg.get("BREADFAST_BEARER_TOKEN", "")).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    cfg["BREADFAST_BEARER_TOKEN"] = token
    return cfg


# ----------------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------------
def now_cairo():
    return datetime.now(CAIRO)


def cairo_date_str(dt=None):
    return (dt or now_cairo()).strftime("%d-%m-%Y")


def parse_epoch_ms(value):
    """Epoch milliseconds -> Cairo datetime, or None."""
    if value in (None, "", "null", "None"):
        return None
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return None
    if n < 1_000_000_000_000 or n >= 10_000_000_000_000:  # not 13-digit ms
        return None
    return datetime.fromtimestamp(n / 1000, tz=timezone.utc).astimezone(CAIRO)


def parse_local_dt(value):
    """Loose datetime-string parser -> Cairo datetime, or None."""
    if value in (None, "", "null", "None", "0000-00-00 00:00:00"):
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d %b %Y %I:%M %p"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CAIRO)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).astimezone(CAIRO)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=CAIRO)).astimezone(CAIRO)
    except ValueError:
        return None


def minutes_since(dt, ref):
    if not dt:
        return None
    return (ref - dt).total_seconds() / 60.0


# ----------------------------------------------------------------------------
# HTTP — the exact request shape proven to work from this network
# ----------------------------------------------------------------------------
def headers(cfg):
    return {
        "Authorization": "Bearer " + cfg["BREADFAST_BEARER_TOKEN"],
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.breadfast.com",
        "Referer": "https://www.breadfast.com/",
    }


def orders_params(cfg, offset, day):
    # List of tuples preserves the bracketed / repeated keys exactly.
    return [
        ("page[limit]", str(cfg["PAGE_LIMIT"])),
        ("page[offset]", str(offset)),
        ("pending_orders_only", "true"),
        ("fps[]", str(cfg["FP_ID"])),
        ("type", "teams"),
        ("date", day),
    ]


def fetch_pending_orders(cfg):
    """Returns (orders_list, http_status). Raises on network failure."""
    url = cfg["CONTROL_ROOM_BASE"].rstrip("/") + "/orders/"
    day = cairo_date_str()
    orders, offset, status = [], 0, None
    session = requests.Session()
    session.headers.update(headers(cfg))
    for _ in range(int(cfg["MAX_PAGES"])):
        resp = session.get(url, params=orders_params(cfg, offset, day),
                           timeout=cfg["TIMEOUT_SECONDS"])
        status = resp.status_code
        if status != 200:
            return orders, status  # 453 etc. — caller reports it
        payload = resp.json()
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        rows = rows if isinstance(rows, list) else []
        orders.extend(r for r in rows if isinstance(r, dict))
        if len(rows) < int(cfg["PAGE_LIMIT"]):
            break
        offset += int(cfg["PAGE_LIMIT"])
    return orders, status


def norm(value):
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


TERMINAL = {"completed", "delivered", "cancelled", "canceled", "returned"}
HEADING_RESTAURANT = {"heading_to_restaurant", "going_to_restaurant",
                      "in_route_to_restaurant", "assigned"}
ARRIVED_RESTAURANT = {"arrived_at_restaurant", "arrived_to_restaurant",
                      "arrived_restaurant", "at_restaurant", "reached_restaurant"}
LAST_MILE_ACTIONS = {"in_route", "inroute", "out_for_delivery", "outfordelivery",
                     "heading_to_customer", "going_to_customer", "in_route_to_customer"}


def has_da(order):
    people = order.get("deliveryPersons") or []
    return isinstance(people, list) and any(isinstance(p, dict) for p in people)


def da_label(order):
    people = order.get("deliveryPersons") or []
    for p in people:
        if isinstance(p, dict):
            name = str(p.get("name") or "").strip()
            hrid = str(p.get("hrid") or p.get("hrId") or p.get("hrID") or "").strip()
            if name or hrid:
                return (name + (f" ({hrid})" if hrid else "")) or "DA"
    return "unassigned"


def classify(order, ref):
    """
    Returns (breach_type, minutes, info) or None if not a breach / out of scope.
    breach_type in {UNASSIGNED, FIRST_MILE, LAST_MILE}.
    """
    status = norm(order.get("status"))
    if status in TERMINAL:
        return None

    actions = order.get("actions") or {}
    last_action = norm(order.get("lastDaAction"))
    last_action_dt = parse_local_dt(order.get("lastDaActionDate"))
    received_dt = parse_epoch_ms(actions.get("received"))
    arrived_customer = parse_epoch_ms(actions.get("arrived"))
    placing_dt = (parse_local_dt(order.get("placingTime"))
                  or parse_local_dt(order.get("createdAt"))
                  or parse_local_dt(order.get("creationTime")))

    info = {
        "order": str(order.get("orderNumber") or order.get("id") or "").strip(),
        "restaurant": str(order.get("restaurantName") or "").strip(),
        "da": da_label(order),
        "status": order.get("status"),
    }

    # LAST MILE: picked up / out for delivery, not yet at customer.
    heading_customer = last_action in LAST_MILE_ACTIONS or ("route" in last_action and "customer" in last_action)
    if heading_customer and received_dt and not arrived_customer:
        mins = minutes_since(received_dt, ref)
        return ("LAST_MILE", mins, info) if mins is not None else None

    # FIRST MILE: DA assigned / heading to restaurant, not yet arrived there.
    heading_rest = (last_action in HEADING_RESTAURANT
                    or ("heading" in last_action and "restaurant" in last_action)
                    or (has_da(order) and norm(actions.get("current")) in {"preparing", "prepared"}))
    arrived_rest = last_action in ARRIVED_RESTAURANT or ("arriv" in last_action and "restaurant" in last_action)
    if heading_rest and not arrived_rest:
        start = last_action_dt or placing_dt
        mins = minutes_since(start, ref)
        return ("FIRST_MILE", mins, info) if mins is not None else None

    # UNASSIGNED: still pending, no DA on it yet.
    if not has_da(order):
        mins = minutes_since(placing_dt, ref)
        return ("UNASSIGNED", mins, info) if mins is not None else None

    return None


THRESHOLD_KEY = {
    "UNASSIGNED": "UNASSIGNED_THRESHOLD_MIN",
    "FIRST_MILE": "FIRST_MILE_THRESHOLD_MIN",
    "LAST_MILE": "LAST_MILE_THRESHOLD_MIN",
}
STAGE_LABEL = {
    "UNASSIGNED": "🕒 No DA assigned",
    "FIRST_MILE": "🛵 First mile (to restaurant)",
    "LAST_MILE": "📦 Last mile (to customer)",
}


# ----------------------------------------------------------------------------
# State (dedup) — a small JSON file on this PC
# ----------------------------------------------------------------------------
def load_state(cfg):
    try:
        with open(cfg["STATE_FILE"], "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(cfg, state):
    try:
        with open(cfg["STATE_FILE"], "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception as exc:
        print(f"WARN: could not write state file: {exc}")


# ----------------------------------------------------------------------------
# Chat
# ----------------------------------------------------------------------------
def post_to_chat(cfg, text):
    url = str(cfg.get("GOOGLE_CHAT_WEBHOOK_URL", "")).strip()
    if not url:
        sys.exit("No GOOGLE_CHAT_WEBHOOK_URL set (config.json or env).")
    resp = requests.post(url, json={"text": text}, timeout=cfg["TIMEOUT_SECONDS"])
    resp.raise_for_status()
    return resp.status_code


def build_message(cfg, breaches, ref):
    head = f"*MDAZ (FP {cfg['FP_ID']}) — {len(breaches)} order(s) need attention*  ·  {ref:%H:%M}"
    lines = [head]
    for b in breaches[: int(cfg["MAX_ALERTS_PER_MESSAGE"])]:
        bt, mins, info = b
        rest = f" · {info['restaurant']}" if info["restaurant"] else ""
        lines.append(f"{STAGE_LABEL[bt]} · *#{info['order']}*{rest} · DA: {info['da']} · {int(mins)}m")
    extra = len(breaches) - int(cfg["MAX_ALERTS_PER_MESSAGE"])
    if extra > 0:
        lines.append(f"…and {extra} more.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def run(cfg, mode):
    ref = now_cairo()
    if not str(cfg.get("CONTROL_ROOM_BASE", "")).strip():
        sys.exit("No CONTROL_ROOM_BASE set (CONTROL_ROOM_BASE secret/env or config.json).")

    orders, status = fetch_pending_orders(cfg)
    if status != 200:
        print(f"API returned HTTP {status} — not 200. Orders not collected. "
              f"(If this is 453 from this PC, the office network is being blocked too.)")
        sys.exit(2)
    print(f"Fetched {len(orders)} pending order(s) for FP {cfg['FP_ID']} at {ref:%Y-%m-%d %H:%M}.")

    if mode == "sample":
        if orders:
            print("Field names on first order:")
            print(sorted(orders[0].keys()))
            print("actions keys:", sorted((orders[0].get("actions") or {}).keys()))
        else:
            print("No pending orders right now to sample.")
        return
    if mode == "test":
        print("Connectivity OK (HTTP 200). Posting nothing in --test mode.")
        return

    # Classify and apply thresholds.
    breaches = []
    for order in orders:
        result = classify(order, ref)
        if not result:
            continue
        bt, mins, info = result
        if mins is None or mins < float(cfg[THRESHOLD_KEY[bt]]):
            continue
        breaches.append((bt, mins, info))

    # Dedup against state.
    state = load_state(cfg)
    realert = float(cfg["REALERT_MIN"])
    to_send, new_state = [], {}
    now_ts = time.time()
    for b in breaches:
        bt, mins, info = b
        key = f"{info['order']}:{bt}"
        prev = state.get(key)
        new_state[key] = prev or {"first": now_ts, "last_alerted": 0}
        last_alerted = (prev or {}).get("last_alerted", 0)
        due = (last_alerted == 0) or (realert > 0 and (now_ts - last_alerted) >= realert * 60)
        if due:
            to_send.append(b)
            new_state[key]["last_alerted"] = now_ts

    if not to_send:
        print(f"{len(breaches)} order(s) over threshold, nothing new to alert.")
        save_state(cfg, new_state)
        return

    message = build_message(cfg, to_send, ref)
    if mode == "dry-run" or cfg.get("DRY_RUN"):
        print("--- DRY RUN (not posted) ---")
        print(message)
        print("----------------------------")
        # In a real dry run we still persist state so repeat dry-runs are quiet;
        # comment the next line out if you want dry runs to keep re-printing.
        save_state(cfg, new_state)
        return

    code = post_to_chat(cfg, message)
    print(f"Posted {len(to_send)} alert(s) to Chat (HTTP {code}).")
    save_state(cfg, new_state)


def main():
    mode = "run"
    if "--test" in sys.argv:
        mode = "test"
    elif "--sample" in sys.argv:
        mode = "sample"
    elif "--dry-run" in sys.argv:
        mode = "dry-run"
    cfg = load_config()
    if not cfg.get("BREADFAST_BEARER_TOKEN"):
        sys.exit("No BREADFAST_BEARER_TOKEN set (config.json or env).")

    # Overlap guard: safe to schedule as often as every minute. If a previous
    # run is still going (lock < 120s old), skip this tick instead of stacking.
    if mode in ("run", "dry-run"):
        lock = os.path.join(HERE, "mdaz_alerter.lock")
        try:
            if os.path.exists(lock) and (time.time() - os.path.getmtime(lock)) < 120:
                print("Previous run still active; skipping this tick.")
                return
            open(lock, "w").close()
        except Exception:
            lock = None
        try:
            run(cfg, mode)
        finally:
            if lock:
                try:
                    os.remove(lock)
                except Exception:
                    pass
    else:
        run(cfg, mode)


if __name__ == "__main__":
    main()
