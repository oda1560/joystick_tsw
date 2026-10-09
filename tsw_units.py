"""
Metric or imperial units by where the route is: the railways of Great Britain, Ireland and the United States use
miles and mph (the game shows yards on those routes), the rest of the game's routes metres and km/h.
"""

YARD, MILE, MPH = 0.9144, 1609.344, 0.44704      # in metres, metres, m/s


def imperial_at(latitude, longitude):
    """Whether a place (the game's DriverAid.PlayerInfo geoLocation) uses imperial units on the railway."""
    if 49.8 <= latitude <= 61.0 and -10.7 <= longitude <= 1.8:          # Great Britain and Ireland ...
        return not (latitude < 51.0 and longitude > 1.45) and not (latitude < 50.65 and longitude > -1.0)
                                                                         # ... but not the French coast opposite
    return 24.0 <= latitude <= 50.0 and -125.0 <= longitude <= -66.0     # the United States


def distance(metres, imperial=False):
    """'950 m' / '1.23 km', or '1,039 yd' / '1.23 mi'; '13 m past' when behind."""
    if metres is None:
        return "-"
    if imperial:
        small, unit, large, large_unit, switch = metres / YARD, "yd", metres / MILE, "mi", MILE
    else:
        small, unit, large, large_unit, switch = metres, "m", metres / 1000, "km", 1000.0
    if metres < -1:
        return f"{-small:,.0f} {unit} past"
    if small < 10:
        return f"{max(small, 0.0):.1f} {unit}"
    if metres < switch:
        return f"{small:,.0f} {unit}"
    return f"{large:.2f} {large_unit}"


def speed(metres_per_second, imperial=False, digits=0):
    if imperial:
        return f"{metres_per_second / MPH:.{digits}f} mph"
    return f"{metres_per_second * 3.6:.{digits}f} km/h"
