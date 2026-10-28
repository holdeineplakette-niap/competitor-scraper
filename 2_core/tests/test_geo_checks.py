from core import geo
from master_fixtures import CENTROIDS


def test_germany_box():
    assert geo.in_germany(51.0, 10.0)
    assert not geo.in_germany(48.8, 2.3)  # Paris
    assert not geo.in_germany(0.0, 0.0)


def test_plz_distance_uses_the_centroid_and_none_for_unknown_plz():
    cent = geo.centroid_lookup(CENTROIDS)
    assert geo.plz_distance_km(51.06, 13.74, "01067", cent) < 1
    assert geo.plz_distance_km(50.2000, 8.5800, "65520", cent) > 20  # Oberursel coordinates on a Bad Camberg PLZ
    assert geo.plz_distance_km(51.0, 10.0, "00000", cent) is None


def test_shared_coordinate_groups_need_different_addresses():
    rows = [
        {"id": 1, "c": (50.2, 8.58), "a": "x"},
        {"id": 2, "c": (50.2, 8.58), "a": "y"},   # same spot, other address -> group
        {"id": 3, "c": (51.0, 9.0), "a": "z"},
        {"id": 4, "c": (51.0, 9.0), "a": "z"},   # same spot, same address -> not a group
        {"id": 5, "c": None, "a": "q"},
    ]
    groups = geo.shared_coordinate_groups(rows, lambda r: r["c"], lambda r: r["a"])
    assert [[r["id"] for r in g] for g in groups] == [[1, 2]]
