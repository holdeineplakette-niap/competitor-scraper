"""Minimal robots.txt matcher with wildcard support (Python's robotparser ignores '*' and '$' patterns).

Only the rules of `User-agent: *` are used. The scraper checks every URL against them before fetching.
"""

from __future__ import annotations

import re


class RobotsDisallowedError(RuntimeError):
    """The URL is disallowed by robots.txt and the run was not explicitly allowed to fetch it."""


def parse_rules(robots_text: str) -> tuple[list[str], list[str], list[str]]:
    """(disallow patterns, allow patterns, sitemap urls) of the `User-agent: *` group."""
    disallow: list[str] = []
    allow: list[str] = []
    sitemaps: list[str] = []
    in_star_group = False
    seen_rule_in_group = False
    for line in robots_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (p.strip() for p in line.split(":", 1))
        field = field.lower()
        if field == "sitemap":
            sitemaps.append(value)
        elif field == "user-agent":
            if seen_rule_in_group:  # a new group starts
                in_star_group, seen_rule_in_group = False, False
            if value == "*":
                in_star_group = True
        elif field in ("disallow", "allow"):
            seen_rule_in_group = True
            if in_star_group and value:
                (disallow if field == "disallow" else allow).append(value)
    return disallow, allow, sitemaps


def _to_regex(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = re.escape(pattern.rstrip("$")).replace(r"\*", ".*")
    return re.compile("^" + body + ("$" if anchored else ""))


def disallowing_rule(path_and_query: str, disallow: list[str], allow: list[str]) -> str | None:
    """The Disallow pattern that blocks this path+query, or None. The longest matching rule wins; Allow wins ties."""
    best_dis = max((p for p in disallow if _to_regex(p).match(path_and_query)), key=len, default=None)
    if best_dis is None:
        return None
    best_allow = max((p for p in allow if _to_regex(p).match(path_and_query)), key=len, default=None)
    if best_allow is not None and len(best_allow) >= len(best_dis):
        return None
    return best_dis
