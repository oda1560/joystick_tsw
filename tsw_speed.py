"""
Watches the train's speed against the speed limit the game gives, for the speeding popup, and whether the route
uses metric or imperial units (tsw_units.py), for both popups.

The game itself only counts speeding past a tolerance (Player.SpeedingTolerance, 3 mph on Airedale-Wharfedale);
the popup comes up at the limit, and says when the game counts it.

    watch = SpeedWatch(); watch.start()
    watch.state     # None when not speeding, else {"limit": m/s, "speed": m/s,
                    #                               "counted": the game counts it as speeding}
    watch.limit     # the speed limit now, m/s (None: no limit, or not in a cab)
    watch.imperial  # the route uses miles and mph
"""

import threading
import time

import tsw_joystick as core
import tsw_units

POLL_SECONDS = 0.25
PLACE_SECONDS = 5.0    # how often to check where the train is, for the units
OVER = 0.3             # m/s (~1 km/h) over the limit before it counts as speeding: holding line speed doesn't flicker it
BACK = 0.0             # m/s over the limit at which it stops counting
NO_LIMIT = 1000.0      # m/s; the game gives 3.4e38 for no limit


class SpeedWatch(threading.Thread):
    """Polls the train's speed and speed limit on a thread of its own."""

    def __init__(self):
        super().__init__(daemon=True)
        self.api = core.TSWApi()
        self.running = True
        self.state = None
        self.limit = None
        self.imperial = False
        self.last_place = 0.0

    def run(self):
        while self.running:
            try:
                self.step()
            except Exception:             # game not running, or a dropped connection
                self.state = self.limit = None
                time.sleep(1)
            time.sleep(POLL_SECONDS)

    def step(self):
        api = self.api
        if not api.key and not api.load_key():
            self.state = self.limit = None
            time.sleep(2)
            return
        now = time.monotonic()
        if now - self.last_place > PLACE_SECONDS:
            self.last_place = now
            geo = (api.get("DriverAid.PlayerInfo").get("Values") or {}).get("geoLocation") or {}
            if "latitude" in geo and "longitude" in geo:
                self.imperial = tsw_units.imperial_at(geo["latitude"], geo["longitude"])
        speed = api.get_value("CurrentDrivableActor.Function.HUD_GetSpeed")
        if speed is None:                 # not in a cab
            self.state = self.limit = None
            return
        aid = api.get("DriverAid.Data").get("Values") or {}
        limit = (aid.get("speedLimit") or {}).get("value")
        if not limit or limit > NO_LIMIT:
            self.state = self.limit = None
            return
        self.limit = limit
        speed = abs(speed)
        if speed - limit > (BACK if self.state else OVER):
            counted = bool(api.get_value("Player.Function.IsDrivableActorSpeeding"))
            self.state = {"limit": limit, "speed": speed, "counted": counted}
        else:
            self.state = None
