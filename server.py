"""
SeismoNet UZ — WebSocket Server
================================
O'rnatish:
    pip install fastapi uvicorn

Ishga tushirish:
    python server.py

Brauzerdan ulanish:
    ws://localhost:8000/ws
"""

import json
import math
import time
import asyncio
import os
from collections import defaultdict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI(title="SeismoNet UZ")

# ── O'zbekiston viloyatlari koordinatalari ──────────────────────────
UZ_REGIONS = {
    "Toshkent"        : (41.2995, 69.2401),
    "Samarqand"       : (39.6270, 66.9750),
    "Namangan"        : (41.0011, 71.6683),
    "Andijon"         : (40.7821, 72.3442),
    "Farg'ona"        : (40.3864, 71.7864),
    "Qashqadaryo"     : (38.8610, 65.7919),
    "Surxondaryo"     : (37.9401, 67.5699),
    "Navoiy"          : (40.0840, 65.3792),
    "Jizzax"          : (40.1158, 67.8422),
    "Sirdaryo"        : (40.8393, 68.6614),
    "Xorazm"          : (41.5533, 60.6236),
    "Buxoro"          : (39.7680, 64.4219),
    "Qoraqalpog'iston": (43.8041, 59.6021),
}

# ── Server holati ───────────────────────────────────────────────────
class SeismoState:
    def __init__(self):
        # Ulangan qurilmalar: device_id → WebSocket
        self.connections: dict[str, WebSocket] = {}
        # Qurilma ma'lumotlari: device_id → {lat, lng, region, ...}
        self.devices: dict[str, dict] = {}
        # So'nggi triggerlar: [{device_id, lat, lng, mag, time}, ...]
        self.recent_triggers: list[dict] = []
        # Cooldown — bir daqiqada bir marta alert
        self.last_alert_time: float = 0
        self.total_alerts: int = 0

state = SeismoState()

# ── Haversine masofa ────────────────────────────────────────────────
def haversine(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    d1 = math.radians(lat2 - lat1)
    d2 = math.radians(lng2 - lng1)
    a = (math.sin(d1/2)**2 +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) *
         math.sin(d2/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

def nearest_region(lat, lng) -> str:
    return min(UZ_REGIONS, key=lambda r: haversine(lat, lng, *UZ_REGIONS[r]))

def eta_seconds(elat, elng, tlat, tlng) -> dict:
    dist   = haversine(elat, elng, tlat, tlng)
    p_time = dist / 6.5
    s_time = dist / 3.5
    return {
        "distance_km"    : round(dist, 1),
        "s_arrival_s"    : round(s_time, 1),
        "warning_window" : round(s_time - p_time, 1),
    }

# ── Epicentr hisoblash ──────────────────────────────────────────────
def calc_epicenter(triggers: list[dict]) -> dict:
    """Amplituda og'irlikli koordinata markazi."""
    total_w = sum(t["amplitude"] for t in triggers) or len(triggers)
    lat = sum(t["lat"] * t["amplitude"] for t in triggers) / total_w
    lng = sum(t["lng"] * t["amplitude"] for t in triggers) / total_w
    mag = sum(t["magnitude"] for t in triggers) / len(triggers)
    return {
        "lat"    : round(lat, 4),
        "lng"    : round(lng, 4),
        "region" : nearest_region(lat, lng),
        "magnitude": round(mag, 1),
    }

# ── Alert yuborish ──────────────────────────────────────────────────
async def broadcast_alert(epicenter: dict):
    """Barcha ulangan qurilmalarga ETA hisoblash va alert yuborish."""
    now = time.time()
    if now - state.last_alert_time < 60:
        return  # spam himoya
    state.last_alert_time = now
    state.total_alerts   += 1

    elat = epicenter["lat"]
    elng = epicenter["lng"]
    mag  = epicenter["magnitude"]

    print(f"\n{'='*55}")
    print(f"  ⚠  ZILZILA TASDIQLANDI — M{mag}")
    print(f"  Epicentr: {epicenter['region']} ({elat}, {elng})")
    print(f"  Ulangan qurilmalar: {len(state.connections)}")
    print(f"{'='*55}")

    # Har bir qurilmaga individual ETA — alert davomiyligi 5 soniya
    ALERT_DURATION = int(os.environ.get("ALERT_DURATION", "5"))
    dead = []
    for device_id, ws in state.connections.items():
        dev = state.devices.get(device_id, {})
        dlat = dev.get("lat", elat)
        dlng = dev.get("lng", elng)

        eta = eta_seconds(elat, elng, dlat, dlng)

        msg = {
            "type"          : "ALERT",
            "magnitude"     : mag,
            "epicenter"     : epicenter["region"],
            "epicenter_lat" : elat,
            "epicenter_lng" : elng,
            "distance_km"   : eta["distance_km"],
            "eta_seconds"   : ALERT_DURATION,
            "s_arrival_s"   : eta["s_arrival_s"],
        }

        print(f"  → {device_id[:12]:12s} | "
              f"{eta['distance_km']:6.0f} km | "
              f"{ALERT_DURATION}s ogohlantirish")

        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            dead.append(device_id)

    # O'lik ulanishlarni tozalash
    for d in dead:
        state.connections.pop(d, None)
        state.devices.pop(d, None)

    print(f"{'='*55}\n")

# ── WebSocket endpoint ──────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    device_id = None

    # Railway 60s da idle ulanishni uzadi — har 25s da ping yuboramiz
    async def keepalive():
        while True:
            await asyncio.sleep(25)
            try:
                await ws.send_text('{"type":"PING"}')
            except Exception:
                break

    ping_task = asyncio.create_task(keepalive())

    try:
        async for raw in ws.iter_text():
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            mtype = msg.get("type")

            # ── Qurilma ro'yxatdan o'tishi ──
            if mtype == "REGISTER":
                device_id = msg.get("device_id", f"dev_{id(ws)}")
                lat = float(msg.get("lat", 41.3))
                lng = float(msg.get("lng", 69.2))

                state.connections[device_id] = ws
                state.devices[device_id] = {
                    "lat"      : lat,
                    "lng"      : lng,
                    "region"   : nearest_region(lat, lng),
                    "last_seen": time.time(),
                }

                region = state.devices[device_id]["region"]
                print(f"[+] {device_id[:16]:16s} | {region:20s} | "
                      f"{lat:.3f},{lng:.3f} | "
                      f"Jami: {len(state.connections)} qurilma")

                await ws.send_text(json.dumps({
                    "type"         : "REGISTERED",
                    "device_id"    : device_id,
                    "region"       : region,
                    "device_count" : len(state.connections),
                }))

            # ── Trigger (P-to'lqin aniqlandi) ──
            elif mtype == "TRIGGER":
                if not device_id:
                    continue

                trigger = {
                    "device_id" : device_id,
                    "lat"       : state.devices[device_id]["lat"],
                    "lng"       : state.devices[device_id]["lng"],
                    "magnitude" : float(msg.get("magnitude", 4.0)),
                    "amplitude" : float(msg.get("amplitude", 0.1)),
                    "time"      : time.time(),
                }
                state.recent_triggers.append(trigger)

                # Eski triggerlarni tozalash (5 soniya oyna)
                cutoff = time.time() - 5.0
                state.recent_triggers = [
                    t for t in state.recent_triggers
                    if t["time"] > cutoff
                ]

                # Noyob qurilmalar
                unique = {t["device_id"]: t
                          for t in state.recent_triggers}

                min_dev = int(os.environ.get("MIN_DEVICES", "1"))
                print(f"[T] {device_id[:12]:12s} "
                      f"M{trigger['magnitude']} | "
                      f"Triggerlar: {len(unique)}/{min_dev}")

                # MIN_DEVICES+ qurilma → alert (default: 1)
                if len(unique) >= min_dev:
                    epicenter = calc_epicenter(list(unique.values()))
                    await broadcast_alert(epicenter)
                    state.recent_triggers.clear()

            # ── Heartbeat ──
            elif mtype == "HEARTBEAT":
                if device_id and device_id in state.devices:
                    state.devices[device_id]["last_seen"] = time.time()
                    lat = float(msg.get("lat", state.devices[device_id]["lat"]))
                    lng = float(msg.get("lng", state.devices[device_id]["lng"]))
                    state.devices[device_id]["lat"] = lat
                    state.devices[device_id]["lng"] = lng
                    await ws.send_text(json.dumps({
                        "type"         : "PONG",
                        "device_count" : len(state.connections),
                    }))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[!] WebSocket xatosi ({device_id}): {e}")
    finally:
        ping_task.cancel()
        if device_id:
            state.connections.pop(device_id, None)
            state.devices.pop(device_id, None)
            print(f"[-] {device_id[:16]:16s} | "
                  f"Jami: {len(state.connections)} qurilma")

# ── Status API ──────────────────────────────────────────────────────
@app.get("/api/status")
async def get_status():
    return {
        "active_devices" : len(state.connections),
        "total_alerts"   : state.total_alerts,
        "recent_triggers": len(state.recent_triggers),
    }

# ── HTML fayllar ────────────────────────────────────────────────────
@app.get("/")
async def serve_html():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>index.html topilmadi</h1>", status_code=404)

@app.get("/demo.html")
async def serve_demo():
    try:
        with open("demo.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>demo.html topilmadi</h1>", status_code=404)

# ── Ishga tushirish (Railway PORT env o'zgaruvchisini oladi) ─────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print("=" * 55)
    print(f"  SeismoNet UZ Server — port {port}")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
