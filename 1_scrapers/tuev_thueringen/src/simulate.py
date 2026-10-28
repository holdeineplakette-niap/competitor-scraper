"""Offline fake of tuev-thueringen.de for tuev_thueringen.run: the store group "Prüfanlagen" and the cross-check URL.

The page has the real shape (Shopware + Acris Store Locator): every store three times, as map overlay, desktop table
row and mobile entry, merged per data-store-id. Twelve own stations, as on 2026-10-06. Faults can be injected to prove
that a run does not pass.
"""

from __future__ import annotations

from tuev_thueringen import adapter
from tuev_thueringen.adapter import FetchError

# (store id, name, street, PLZ, city, lat, lng, e-mail)
STORES = [
    (f"{i:032x}", f"TÜV Thüringen Kfz-Prüfanlage {city}", street, zip_, city, lat, lng, f"pruefanlage-{i}@tuev-thueringen.de")
    for i, (street, zip_, city, lat, lng) in enumerate([
        ("Ernst-Ruska-Ring 6", "07745", "Jena", 50.893, 11.595),
        ("Melchendorfer Straße 64", "99096", "Erfurt", 50.958, 11.050),
        ("Rudolstädter Straße 40", "07318", "Saalfeld", 50.643, 11.358),
        ("Am Bahnhof 1", "99734", "Nordhausen", 51.497, 10.792),
        ("Industriestraße 2", "98527", "Suhl", 50.610, 10.693),
        ("Gewerbegebiet 3", "07546", "Gera", 50.880, 12.083),
        ("Weimarische Straße 4", "99423", "Weimar", 50.979, 11.330),
        ("Langensalzaer Straße 5", "99974", "Mühlhausen", 51.208, 10.454),
        ("Eisenacher Straße 6", "99817", "Eisenach", 50.975, 10.320),
        ("Arnstädter Straße 7", "99310", "Arnstadt", 50.835, 10.947),
        ("Sonneberger Straße 8", "96515", "Sonneberg", 50.359, 11.170),
        ("Altenburger Straße 9", "04600", "Altenburg", 50.985, 12.433),
    ], 1)
]
FAULTS = ("http_403", "no_table", "view_mismatch", "cross_differs", "cross_down", "bad_address", "duplicate_id", "drop_half")


def _contact(store) -> str:
    sid, *_, email = store
    return (f'<a href="tel:0361 0000{sid[-2:]}">0361 0000{sid[-2:]}</a><br/>'
            f'<a class="acris-store-locator-mail" data-mail="{email}" href="mailto:{email}"><span></span></a><br/>'
            'Mo-Fr: 08:00 - 17:00 Uhr<div>Sa: 08:00 - 12:00 Uhr</div>'
            '<div><a href="https://www.tuev-thueringen.de/hu-terminvereinbarung">HU-Terminvereinbarung</a></div>')


def page(fault: str | None = None, cross: bool = False) -> str:
    stores = list(STORES)
    if fault == "drop_half":
        stores = stores[: len(stores) // 2]
    if fault == "bad_address":
        sid, name, _, _, city, lat, lng, email = stores[0]
        stores[0] = (sid, name, "Ohne Postleitzahl", "", city, lat, lng, email)
    if fault == "duplicate_id":
        stores.append(stores[0])
    if cross and fault == "cross_differs":
        stores = stores[:-1]
    overlay = "".join(
        f'<div class="acris-store-locator-map-overlay__store mb-2" data-lat="{lat}" data-lng="{lng}" data-store-id="{sid}">'
        f'<button type="button">{name}</button><a href="/store-locator/detail/{sid}">Details</a></div>'
        for sid, name, _, _, _, lat, lng, _ in stores)
    table = "".join(
        f'<tr data-store-id="{s[0]}"><td>{s[1]}</td><td>{s[2]}  {s[3]} {s[4]} Germany</td><td>{_contact(s)}</td>'
        f'<td><a href="/store-locator/detail/{s[0]}">Details</a></td></tr>' for s in stores)
    mobile_stores = stores[:-1] if fault == "view_mismatch" else stores
    mobile = "".join(
        f'<div class="acris-store-locator-group-mobile-table-entry" data-store-id="{s[0]}"><table><tbody>'
        f'<tr><th>Name :</th><td><span>{s[1]}</span></td></tr>'
        f'<tr><th>Adresse :</th><td><span>{s[2]}  {s[3]} {s[4]} Germany</span></td></tr>'
        f'<tr><th>Kontakt :</th><td><span>{_contact(s)}</span></td></tr>'
        f'<tr><th>Details :</th><td><span><a href="/store-locator/detail/{s[0]}">Details</a></span></td></tr>'
        '</tbody></table></div>' for s in mobile_stores)
    return (f'<html><body>{overlay}<table id="{adapter.TABLE_ID}"><tbody>{table}</tbody></table>{mobile}</body></html>')


class SimulatedThueringen:
    """The client interface of tuev_thueringen.run (get) without a request."""

    def __init__(self, fault: str | None = None):
        if fault is not None and fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}; choose from {FAULTS}")
        self.fault = fault
        self.requests_made = 0

    def get(self, url: str) -> str:
        self.requests_made += 1
        cross = url == adapter.CROSSCHECK_URL
        if self.fault == "http_403" and not cross:
            raise FetchError(f"HTTP 403 for {url}")
        if self.fault == "no_table" and not cross:
            raise FetchError(f"no store table on {url} (challenge page or changed layout)")
        if self.fault == "cross_down" and cross:
            raise FetchError(f"HTTP 503 for {url}")
        return page(self.fault, cross)
