"""GCJ-02 conversion adapted from https://github.com/googollee/eviltransform."""

from math import cos, pi, radians, sin, sqrt


def wgs84_to_gcj02(lon, lat):
    if not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lon, lat
    x, y = lon - 105, lat - 35
    x_pi, y_pi = x * pi, y * pi
    common = 20 * sin(6 * x_pi) + 20 * sin(2 * x_pi)
    north = common + 20 * sin(y_pi) + 40 * sin(y_pi / 3)
    north += 160 * sin(y_pi / 12) + 320 * sin(y_pi / 30)
    north = north * 2 / 3 - 100 + 2 * x + 3 * y + .2 * y * y + .1 * x * y + .2 * sqrt(abs(x))
    east = common + 20 * sin(x_pi) + 40 * sin(x_pi / 3)
    east += 150 * sin(x_pi / 12) + 300 * sin(x_pi / 30)
    east = east * 2 / 3 + 300 + x + 2 * y + .1 * x * x + .1 * x * y + .1 * sqrt(abs(x))
    eccentricity = 0.00669342162296594323
    axis = 6378137.0
    latitude = radians(lat)
    magic = 1 - eccentricity * sin(latitude) ** 2
    d_lat = north * 180 * magic * sqrt(magic) / (axis * (1 - eccentricity) * pi)
    d_lon = east * 180 * sqrt(magic) / (axis * cos(latitude) * pi)
    return lon + d_lon, lat + d_lat


def gcj02_to_wgs84(lon, lat):
    """Iteratively invert the approximate forward transform."""
    candidate_lon, candidate_lat = lon, lat
    for _ in range(12):
        mapped_lon, mapped_lat = wgs84_to_gcj02(candidate_lon, candidate_lat)
        lon_error, lat_error = mapped_lon - lon, mapped_lat - lat
        if max(abs(lon_error), abs(lat_error)) < 0.0000001:
            break
        candidate_lon -= lon_error
        candidate_lat -= lat_error
    return candidate_lon, candidate_lat
