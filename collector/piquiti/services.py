"""Map hostnames to apps, companies and categories.

The source is v2fly/domain-list-community: one file per service ("netflix",
"google", "apple-update"...), with include: lines linking a service to its
parent company and category-* files grouping services by type.

Lookup rules:
- app: the most specific list that names the domain directly (not via include).
- company: the top-most non-category list that includes the app, or the app itself.
- category: first match in CATEGORIES for the app or any ancestor; "@ads" rules
  force "Ads & Tracking".
- overrides from services.override.yaml win over everything.
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

V2FLY_TARBALL = "https://github.com/v2fly/domain-list-community/archive/refs/heads/master.tar.gz"

# Ordered: first match wins. Keys are v2fly category file names.
CATEGORIES = [
    ("category-ads-all", "Ads & Tracking"),
    ("category-social-media-!cn", "Social"),
    ("category-social-media-cn", "Social"),
    ("category-games", "Gaming"),
    ("category-games-!cn", "Gaming"),
    ("category-game-platforms-download", "Gaming"),
    ("category-entertainment", "Streaming & Entertainment"),
    ("category-communication", "Communication"),
    ("category-voip", "Communication"),
    ("category-ai-!cn", "AI"),
    ("category-ecommerce", "Shopping"),
    ("category-finance", "Finance"),
    ("category-media", "News"),
    ("category-tech-media", "News"),
    ("category-forums", "Forums"),
    ("category-password-management", "Security"),
    ("category-antivirus", "Security"),
    ("category-vpnservices", "VPN"),
    ("category-speedtest", "Network"),
    ("category-ntp", "Network"),
    ("category-doh", "Network"),
    ("category-dev", "Developer"),
    ("category-cdn-!cn", "CDN"),
    ("category-companies", "Tech Platforms"),
]

ADS_CATEGORY = "Ads & Tracking"


@dataclass
class Match:
    app: str | None
    company: str | None
    category: str | None
    background: bool = False
    source: str = "none"


@dataclass
class ListFile:
    name: str
    domain: set[str] = field(default_factory=set)  # suffix match
    full: set[str] = field(default_factory=set)  # exact match
    ads: set[str] = field(default_factory=set)  # entries tagged @ads
    includes: list[str] = field(default_factory=list)
    # include: lines with an attribute filter ("include:google @ads") pull in
    # only part of a list, so they do not count for category membership.
    filtered_includes: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.domain) + len(self.full)


def parse_list(name: str, text: str) -> ListFile:
    lf = ListFile(name)
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        rule, attrs = parts[0], parts[1:]
        kind, _, value = rule.partition(":") if ":" in rule else ("domain", "", rule)
        value = value.lower().rstrip(".")
        if kind == "include":
            lf.includes.append(value)
            if attrs:
                lf.filtered_includes.append(value)
        elif kind in ("domain", "full"):
            (lf.domain if kind == "domain" else lf.full).add(value)
            if "@ads" in attrs:
                lf.ads.add(value)
        # keyword: and regexp: rules are skipped; they are rare and slow to match.
    return lf


def load_lists(data_dir: Path) -> dict[str, ListFile]:
    return {p.name: parse_list(p.name, p.read_text(encoding="utf-8")) for p in data_dir.iterdir() if p.is_file()}


def download_v2fly(dest: Path) -> Path:
    """Fetch the v2fly data/ directory into dest and return its path."""
    resp = httpx.get(V2FLY_TARBALL, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if len(parts) == 3 and parts[1] == "data" and member.isfile():
                (dest / parts[2]).write_bytes(tar.extractfile(member).read())
    return dest


def pretty(name: str, names: dict[str, str]) -> str:
    if name in names:
        return names[name]
    if len(name) <= 3:
        return name.upper()  # bbc, hbo, cnn
    return " ".join(w.capitalize() for w in name.replace("!", "not-").split("-"))


class ServiceMap:
    def __init__(self, lists: dict[str, ListFile], overrides: dict | None = None):
        overrides = overrides or {}
        self.names: dict[str, str] = overrides.get("names", {}) or {}
        self.overrides: dict[str, dict] = {k.lower(): v or {} for k, v in (overrides.get("domains", {}) or {}).items()}
        self.background: set[str] = set(overrides.get("background", []) or [])

        services = {n: lf for n, lf in lists.items() if not n.startswith(("category-", "geolocation-"))}

        # Direct membership: smaller list wins when a domain appears in several.
        self.suffix: dict[str, str] = {}
        self.exact: dict[str, str] = {}
        self.ads: set[str] = set()
        for lf in sorted(services.values(), key=lambda l: l.size, reverse=True):
            for d in lf.domain:
                self.suffix[d] = lf.name
            for d in lf.full:
                self.exact[d] = lf.name
            self.ads |= lf.ads

        # Parent links between service lists, for company roll-up.
        self.parents: dict[str, list[str]] = {}
        for lf in services.values():
            for inc in lf.includes:
                if inc in services and inc != lf.name:
                    self.parents.setdefault(inc, []).append(lf.name)

        # Category of each list, resolving category include chains.
        self.category_of: dict[str, str] = {}
        for cat_file, label in reversed(CATEGORIES):
            for member in self._expand(cat_file, lists):
                self.category_of[member] = label

    @staticmethod
    def _expand(name: str, lists: dict[str, ListFile], seen: set[str] | None = None) -> set[str]:
        seen = seen if seen is not None else set()
        if name in seen or name not in lists:
            return set()
        seen.add(name)
        out = {name}
        lf = lists[name]
        for inc in lf.includes:
            if inc not in lf.filtered_includes:
                out |= ServiceMap._expand(inc, lists, seen)
        return out

    def _company(self, app: str) -> str:
        """Walk up include links to the largest ancestor; stops at cycles."""
        seen = {app}
        cur = app
        while True:
            parents = [p for p in self.parents.get(cur, []) if p not in seen and not p.endswith(("-cn", "-!cn"))]
            if not parents:
                return cur
            cur = min(parents, key=len)  # "google" over "google-trust-services-and-friends"
            seen.add(cur)

    def _category(self, app: str) -> str | None:
        cur, seen = app, set()
        while cur and cur not in seen:
            seen.add(cur)
            if cur in self.category_of:
                return self.category_of[cur]
            parents = self.parents.get(cur, [])
            cur = min(parents, key=len) if parents else None
        return None

    def lookup(self, hostname: str) -> Match:
        host = hostname.lower().rstrip(".")
        labels = host.split(".")
        suffixes = [".".join(labels[i:]) for i in range(len(labels))]

        for s in suffixes:
            if s in self.overrides:
                o = self.overrides[s]
                app = o.get("app")
                return Match(
                    app=app,
                    company=o.get("company", app),
                    category=o.get("category"),
                    background=bool(o.get("background", False)) or app in self.background,
                    source="override",
                )

        list_name = self.exact.get(host)
        hit = host if list_name else None
        if not list_name:
            for s in suffixes:
                if s in self.suffix:
                    list_name, hit = self.suffix[s], s
                    break
        if not list_name:
            return Match(None, None, None)

        company = self._company(list_name)
        category = ADS_CATEGORY if hit in self.ads else self._category(list_name)
        app = pretty(list_name, self.names)
        return Match(
            app=app,
            company=pretty(company, self.names),
            category=category,
            background=app in self.background,
            source="v2fly",
        )


def load_overrides(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def build(data_dir: Path, overrides_path: Path, refresh: bool = False) -> ServiceMap:
    if refresh or not data_dir.exists() or not any(data_dir.iterdir()):
        download_v2fly(data_dir)
    return ServiceMap(load_lists(data_dir), load_overrides(overrides_path))
