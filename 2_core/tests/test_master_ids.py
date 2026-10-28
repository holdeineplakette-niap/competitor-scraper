from core import master_ids as ids


def test_first_master_numbers_groups_in_sorted_order():
    out, events = ids.assign([["B-2"], ["A-1", "A-2"], ["C-3"]], {})
    assert out == ["PS-000002", "PS-000001", "PS-000003"] and events == []


def test_ids_carry_over_by_source_id_overlap():
    prev = {"PS-000007": ["A-1"], "PS-000009": ["B-1"]}
    out, events = ids.assign([["B-1", "B-9"], ["A-1"], ["N-1"]], prev)
    assert out == ["PS-000009", "PS-000007", "PS-000010"] and events == []


def test_two_previous_ids_that_now_merge_keep_the_lower_and_log_the_other():
    prev = {"PS-000003": ["A-1"], "PS-000005": ["B-1"]}
    out, events = ids.assign([["A-1", "B-1"]], prev)
    assert out == ["PS-000003"]
    assert events == [{"event": "retired", "master_id": "PS-000005", "merged_into": "PS-000003"}]


def test_an_even_split_gives_the_first_group_the_id_and_the_other_a_new_one():
    prev = {"PS-000003": ["A-1", "B-1"]}
    out, events = ids.assign([["B-1"], ["A-1"]], prev)
    assert out == ["PS-000004", "PS-000003"]  # equal overlap: A-1 sorts first
    assert events == [{"event": "split_new_id", "master_id": "PS-000004", "from": "PS-000003"}]


def test_a_split_keeps_the_id_with_the_larger_part():
    prev = {"PS-000001": ["A", "B", "C", "D"]}
    out, events = ids.assign([["A"], ["B", "C", "D"]], prev)
    assert out == ["PS-000002", "PS-000001"]
    assert events[0]["event"] == "split_new_id"


def test_an_id_that_disappeared_in_an_earlier_month_is_never_reissued():
    """The registry (highest id ever issued) lives in each official summary; the previous master alone would forget PS-000003."""
    prev = {"PS-000001": ["A-1"], "PS-000002": ["B-1"]}
    out, _ = ids.assign([["A-1"], ["B-1"], ["D-1"]], prev)
    assert out[2] == "PS-000003"  # without the registry
    out, _ = ids.assign([["A-1"], ["B-1"], ["D-1"]], prev, max_issued=3)
    assert out[2] == "PS-000004"


def test_a_vanished_previous_id_is_not_reported_as_retired_and_not_reused():
    out, events = ids.assign([["N-1"]], {"PS-000004": ["GONE-1"]})
    assert out == ["PS-000005"] and events == []


def test_result_is_independent_of_input_order():
    prev = {"PS-000001": ["A-1"], "PS-000002": ["B-1"]}
    g1 = [["A-1"], ["B-1"], ["N-1"], ["M-1"]]
    g2 = list(reversed(g1))
    o1, _ = ids.assign(g1, prev)
    o2, _ = ids.assign(g2, prev)
    assert dict(zip(map(tuple, g1), o1)) == dict(zip(map(tuple, g2), o2))
