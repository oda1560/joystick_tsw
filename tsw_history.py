"""
The services you've driven to the end, kept to compare runs (service_history.json next to this script): when, the
route, service and train, your points, how many stops you made on time and how close to the marker (from the
game's checkpoint save, tsw_score.py), the most passengers aboard and how many rides between stations were smooth
(tsw_comfort.py).

    history = History()
    run = history.finish(stop, comfort)   # at the end of a service (tsw_stops.StopTracker.stop, phase "end";
                                          # comfort: tsw_comfort.ComfortWatch.service): keeps the run, again as
                                          # the points come in, and gives it; None if the save has no stops for it
    history.best_before(run)              # the best other run of that service (most points), or None
    history.count(run["service"])         # runs of it kept
    history.runs                          # all of them, oldest first

A run is told apart by the service and the game time it arrived at its first stop, so the same run read again
(the bridge started again at the end of it, say) is kept once.
"""

import datetime
import json
import os

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service_history.json")
ON_TIME = 60.0                 # s: arriving up to this late counts as on time (early too)


class History:
    def __init__(self, path=HISTORY_FILE):
        self.path = path
        self.runs = []
        try:
            with open(path, encoding="utf-8") as f:
                self.runs = [r for r in json.load(f) if isinstance(r, dict) and r.get("id")]
        except OSError:
            pass
        except (ValueError, TypeError):
            try:                              # keep a broken file rather than write over it
                os.replace(path, path + ".bad")
            except OSError:
                pass

    def finish(self, stop, comfort=None):
        points = stop.get("points") or {}
        stops = points.get("stops") or []
        if not stops or points.get("first") is None:
            return None
        run_id = f"{stop['service']}@{points['first']:.1f}"
        old = next((r for r in self.runs if r["id"] == run_id), None)
        late = [late for late, _ in stops]
        finished = old["finished"] if old else datetime.datetime.now().isoformat(timespec="seconds")
        run = {"id": run_id, "finished": finished, "service": stop["service"], "route": stop.get("route") or "",
               "train": stop.get("train") or "", "points": points.get("points"), "stops": len(stops),
               "on_time": sum(s <= ON_TIME for s in late), "worst_late": round(max(late), 1),
               "accuracy": round(sum(abs(off) for _, off in stops) / len(stops), 1)}
        if comfort and comfort.get("passengers"):
            run.update(passengers=comfort["passengers"], rides=comfort["rides"], smooth=comfort["smooth"])
        elif old:                              # the comfort part from before (the bridge started again here)
            run.update({k: old[k] for k in ("passengers", "rides", "smooth") if k in old})
        if run != old:
            if old:
                self.runs[self.runs.index(old)] = run
            else:
                self.runs.append(run)
            self._save()
        return run

    def best_before(self, run):
        others = [r for r in self.runs if r["service"] == run["service"] and r["id"] != run["id"]
                  and r.get("points") is not None]
        return max(others, key=lambda r: r["points"], default=None)

    def count(self, service):
        return sum(r["service"] == service for r in self.runs)

    def _save(self):
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.runs, f, indent=1)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass
