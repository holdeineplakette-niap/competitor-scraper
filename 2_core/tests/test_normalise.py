import pytest

from core import normalise as nm

SAME = [
    ("Hauptstr. 16A", "Hauptstraße 16 a"),
    ("Hauptstrasse 5", "Hauptstr. 5"),
    ("Haupt-Str. 5", "Haupt Straße 5"),
    ("Hauptstr.5", "Hauptstraße 5"),
    ("Müllerstr. 3", "Muellerstrasse 3"),
    ("Industriestr. 5 (bei Fa. Muster)", "Industriestraße 5"),
    ("Industriestr. 5 (bei Fa. Muster", "Industriestraße 5"),
    ("St.-Martin-Str. 2", "Sankt Martin Straße 2"),
    ("Straße des 17. Juni 5", "Str. des 17. Juni 5"),
    ("Robert-Bosch-Str. 16 A", "Robert Bosch Strasse 16a"),
    ("Hauptstr. Nr. 5", "Hauptstraße 5"),
    ("Hauptstr. 1 - 2", "Hauptstraße 1-2"),
    ("  Hauptstr.   5  ", "hauptstr. 5"),
    ("Schloßstr. 1", "Schlossstrasse 1"),
    ("Hauptstraße 5,", "Hauptstr. 5"),
    ("Hauptstr. 5.", "Hauptstr. 5"),
    ("Dürener Str. 411, (A.T.U-Gelände)", "Dürener Straße 411"),
    ("Auf Wolfsgang (Autohaus Eifel Mosel) 1", "Auf Wolfsgang 1"),
    ("(Gewerbepark) Industriestr. 5", "Industriestraße 5"),
]
DIFFERENT = [
    ("Hauptstr. 16", "Hauptstr. 16a"),
    ("Hauptstr. 5a", "Hauptstr. 5b"),
    ("Hauptstr. 1-2", "Hauptstr. 12"),
    ("Hauptstr. 5", "Nebenstr. 5"),
    ("Bahnhofstr. 5", "Bahnhofplatz 5"),
    ("An der B 42", "B42"),
]


@pytest.mark.parametrize("a,b", SAME)
def test_street_variants_are_equal(a, b):
    assert nm.street_key(a) == nm.street_key(b)


@pytest.mark.parametrize("a,b", DIFFERENT)
def test_distinct_streets_are_not_equal(a, b):
    assert nm.street_key(a) != nm.street_key(b)


def test_missing_street_has_no_name():
    assert not nm.has_street("") and not nm.has_street(None) and not nm.has_street("   ")
    assert nm.has_street("Hauptstr. 5")


def test_address_key_includes_the_plz():
    assert nm.address_key("Hauptstr. 5", "01067") != nm.address_key("Hauptstr. 5", "01069")


def test_plz_is_padded_not_guessed():
    assert nm.plz_clean("1067") == ("01067", "padded")
    assert nm.plz_clean(" 01067 ") == ("01067", "")
    assert nm.plz_clean("553340") == ("553340", "")  # never repaired by pattern
    assert nm.plz_clean("") == ("", "")


def test_plz_correction_only_from_the_explicit_table_and_city():
    table = {("fsp", "553340", "meckenheim"): "53340"}
    assert nm.plz_clean("553340", "fsp", "Meckenheim", table) == ("53340", "corrected")
    assert nm.plz_clean("553340", "fsp", "Bonn", table) == ("553340", "")
    assert nm.plz_clean("553340", "dekra", "Meckenheim", table) == ("553340", "")


def test_plz_validity():
    assert nm.is_valid_plz("01067", {"01067"})
    assert not nm.is_valid_plz("01067", {"10115"})
    assert not nm.is_valid_plz("1067", None) and not nm.is_valid_plz("", None)
    assert nm.is_valid_plz("99999", None)


def test_house_number_letter_rules():
    assert nm.numbers_compatible("7", "7c") and nm.numbers_compatible("1a", "1") and nm.numbers_compatible("5", "5")
    assert not nm.numbers_compatible("5a", "5b") and not nm.numbers_compatible("5", "15") and not nm.numbers_compatible("", "5")
    assert nm.numbers_related("26", "26-28") and nm.numbers_related("", "5") and nm.numbers_related("5a", "5b")
    assert not nm.numbers_related("5", "15")


def test_one_letter_typo_is_similar_but_a_different_street_is_not():
    assert nm.street_similarity("Willhelm-Dichmann-Str. 3", "Wilhelm-Dichmann-Straße 3") >= 0.85
    assert nm.street_similarity("Am Mastweg 4", "Gewerbegebiet Am Mastweg 4") < 0.85  # containment never merges
    assert nm.street_similarity("Neue Industriestr. 5", "Industriestr. 5") < 0.9  # dedupe.SIMILAR_MIN
    assert nm.street_related("Am Mastweg 4", "Gewerbegebiet Am Mastweg 4")  # but it is a proposal for a human
    assert nm.street_similarity("Hauptstr. 5", "Nebenstr. 5") < 0.85
    assert nm.street_similarity("Hauptstr. 5a", "Hauptstr. 5b") == 0.0


def test_related_streets_for_candidates():
    assert nm.street_related("Dornhofstr. 26", "Dornhofstr. 26-28")
    assert nm.street_related("Wallersheimer Weg 63-67", "Wallersheimer Weg")
    assert not nm.street_related("Handwerkstr. 8", "Handwerkstr. 15")
    assert not nm.street_related("Zeilweg 6", "Kirchgasse 12")


def test_city_keys_match_district_suffixes():
    assert nm.city_keys("Bielefeld-Brackwede") & nm.city_keys("Bielefeld")
    assert not (nm.city_keys("Bad Homburg") & nm.city_keys("Oberursel"))
    assert nm.city_keys("") == set()


def test_fold_handles_none_and_nan():
    assert nm.fold(None) == "" and nm.fold(float("nan")) == ""
