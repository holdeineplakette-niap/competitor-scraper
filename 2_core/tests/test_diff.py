import pandas as pd

from core import diff
from core import master_schema as ms


def master(*rows):
    out = []
    for r in rows:
        base = {c: "" for c in ms.MASTER_COLUMNS}
        base.update(r)
        out.append(base)
    return pd.DataFrame(out, columns=ms.MASTER_COLUMNS)


def row(mid, street="Hauptstr. 5", zip_="01067", **kw):
    return {"master_id": mid, "uo": "DEKRA", "name": f"n{mid}", "street": street, "zip": zip_, "city": "Dresden",
            "lat": "51.060000", "lng": "13.740000", "phone": "1", **kw}


def kinds(d):
    return sorted(zip(d["change"], d["master_id"], d["field"]))


def test_first_master_has_headers_only():
    d = diff.diff_masters(None, master(row("PS-000001")))
    assert list(d.columns) == diff.DIFF_COLUMNS and d.empty


def test_new_disappeared_and_changed():
    old = master(row("PS-000001"), row("PS-000002", street="Nebenstr. 1"))
    new = master(row("PS-000001", phone="2"), row("PS-000003", street="Am Markt 9"))
    d = diff.diff_masters(old, new)
    assert kinds(d) == [("changed", "PS-000001", "phone"), ("disappeared", "PS-000002", ""), ("new", "PS-000003", "")]
    assert d[d["change"] == "changed"].iloc[0][["old", "new"]].tolist() == ["1", "2"]


def test_coordinates_count_as_changed_only_beyond_200_m():
    old = master(row("PS-000001"))
    near = master(row("PS-000001", lat="51.060500"))   # ~55 m
    far = master(row("PS-000001", lat="51.063000"))    # ~333 m
    assert diff.diff_masters(old, near).empty
    d = diff.diff_masters(old, far)
    assert kinds(d) == [("changed", "PS-000001", "lat/lng")]


def test_coordinate_that_appears_or_vanishes_is_a_change():
    old = master(row("PS-000001", lat="", lng=""))
    d = diff.diff_masters(old, master(row("PS-000001")))
    assert kinds(d) == [("changed", "PS-000001", "lat/lng")]


def test_a_vanished_id_whose_address_reappears_is_possibly_rekeyed_not_closure_plus_opening():
    old = master(row("PS-000001"))
    new = master(row("PS-000009", street="Hauptstraße 5"))
    d = diff.diff_masters(old, new)
    assert kinds(d) == [("possibly_rekeyed", "PS-000001", "")]
    assert d.iloc[0]["other_master_id"] == "PS-000009"


def test_two_new_ids_at_the_old_address_are_not_guessed():
    old = master(row("PS-000001"))
    new = master(row("PS-000008"), row("PS-000009", street="Hauptstraße 5"))
    assert sorted(diff.diff_masters(old, new)["change"]) == ["disappeared", "new", "new"]


def test_scraped_at_and_last_seen_alone_are_not_changes():
    old = master(row("PS-000001", scraped_at="a", last_seen="2026-09-01"))
    new = master(row("PS-000001", scraped_at="b", last_seen="2026-10-01"))
    assert diff.diff_masters(old, new).empty
