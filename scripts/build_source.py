#!/usr/bin/env python3
"""Build an AltSource catalog from IPA files or GitHub Releases.

The generated source follows the AltStore source shape used by LiveContainer.
Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "source.config.json"
OVERRIDES_PATH = ROOT / "app-overrides.json"
OUTPUT_PATH = ROOT / "source.json"
ICON_DIR = ROOT / "icons"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-repo", help="owner/repository to scan for releases")
    parser.add_argument("--public-base-url", help="public URL containing source.json")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--icon-dir", type=Path, default=ICON_DIR)
    parser.add_argument(
        "--ipa-dir",
        type=Path,
        help="scan local IPA files instead of GitHub Releases",
    )
    parser.add_argument(
        "--include-prereleases",
        action="store_true",
        help="include GitHub prereleases in addition to the config value",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the existing generated source without changing files",
    )
    return parser.parse_args()


def normalized_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def iso_date(value: str | None) -> str:
    if not value:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00Z"
    return text.replace("+00:00", "Z")


def clean_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip() or fallback
    return str(value)


def plist_value(plist: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = plist.get(key)
        if value not in (None, ""):
            return value
    return None


def bundle_display_name(plist: dict[str, Any], bundle_id: str) -> str:
    name = plist_value(plist, "CFBundleDisplayName", "CFBundleName")
    if isinstance(name, dict):
        name = next(iter(name.values()), None)
    return clean_text(name, bundle_id.rsplit(".", 1)[-1])


def icon_names(plist: dict[str, Any]) -> list[str]:
    names: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            if value not in names:
                names.append(value)
        elif isinstance(value, list):
            for item in value:
                add(item)

    icon_files = plist.get("CFBundleIconFiles")
    add(icon_files)

    icons = plist.get("CFBundleIcons")
    if isinstance(icons, dict):
        primary = icons.get("CFBundlePrimaryIcon")
        if isinstance(primary, dict):
            add(primary.get("CFBundleIconFiles"))

    ipad_icons = plist.get("CFBundleIcons~ipad")
    if isinstance(ipad_icons, dict):
        primary = ipad_icons.get("CFBundlePrimaryIcon")
        if isinstance(primary, dict):
            add(primary.get("CFBundleIconFiles"))

    return names


def find_plist(archive: zipfile.ZipFile) -> str:
    candidates = [
        name
        for name in archive.namelist()
        if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", name, re.IGNORECASE)
    ]
    if not candidates:
        raise ValueError("IPA does not contain Payload/*.app/Info.plist")
    return candidates[0]


def matching_icon_member(archive: zipfile.ZipFile, names: Iterable[str]) -> str | None:
    members = archive.namelist()
    by_lower = {member.lower(): member for member in members}
    png_members = [member for member in members if member.lower().endswith(".png")]

    for requested in names:
        requested = requested.strip()
        if not requested:
            continue
        candidates = [requested]
        if not requested.lower().endswith(".png"):
            candidates.append(f"{requested}.png")
        for candidate in candidates:
            direct = by_lower.get(candidate.lower())
            if direct:
                return direct
            suffix = "/" + candidate.lower()
            for member in png_members:
                if member.lower().endswith(suffix):
                    return member

    # Some IPA builders omit icon metadata. Prefer a large-looking PNG in the app.
    app_pngs = [member for member in png_members if ".app/" in member.lower()]
    if app_pngs:
        return max(app_pngs, key=lambda member: archive.getinfo(member).file_size)
    return None


def read_ipa(path: Path) -> tuple[dict[str, Any], bytes | None]:
    with zipfile.ZipFile(path) as archive:
        plist_path = find_plist(archive)
        plist = plistlib.loads(archive.read(plist_path))
        if not isinstance(plist, dict):
            raise ValueError("Info.plist must contain a dictionary")
        icon_member = matching_icon_member(archive, icon_names(plist))
        icon_data = archive.read(icon_member) if icon_member else None
        return plist, icon_data


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return value or "app"


def public_url(base_url: str, *parts: str) -> str:
    encoded = "/".join(urllib.parse.quote(part, safe="._-") for part in parts)
    return f"{base_url}/{encoded}"


def request_json(url: str, token: str | None = None) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ipa-store-source-builder",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url: str, destination: Path, token: str | None = None) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "ipa-store-source-builder",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def github_releases(repo: str, token: str | None, include_prereleases: bool) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    for page in range(1, 11):
        url = f"https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
        batch = request_json(url, token)
        if not isinstance(batch, list):
            raise ValueError("GitHub Releases API returned an unexpected response")
        releases.extend(batch)
        if len(batch) < 100:
            break
    return [
        release
        for release in releases
        if not release.get("draft") and (include_prereleases or not release.get("prerelease"))
    ]


def release_version(release: dict[str, Any], plist: dict[str, Any]) -> str:
    version = plist_value(plist, "CFBundleShortVersionString", "CFBundleVersion")
    if version:
        return clean_text(version)
    return clean_text(release.get("tag_name"), "0.0.0")


def release_description(release: dict[str, Any]) -> str:
    body = clean_text(release.get("body"))
    return body[:12000]


def local_items(ipa_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(ipa_dir.glob("*.ipa")):
        try:
            plist, icon_data = read_ipa(path)
        except (OSError, ValueError, zipfile.BadZipFile, plistlib.InvalidFileException) as exc:
            raise ValueError(f"Could not read {path.name}: {exc}") from exc
        items.append(
            {
                "plist": plist,
                "icon_data": icon_data,
                "version": release_version({}, plist),
                "date": iso_date(None),
                "downloadURL": path.name,
                "size": path.stat().st_size,
                "description": "",
                "beta": False,
                "source_name": path.name,
            }
        )
    return items


def github_items(
    repo: str,
    token: str | None,
    include_prereleases: bool,
    temp_dir: Path,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    releases = github_releases(repo, token, include_prereleases)
    for release in releases:
        assets = release.get("assets", [])
        for asset in assets:
            name = clean_text(asset.get("name"))
            if not name.lower().endswith(".ipa"):
                continue
            path = temp_dir / safe_filename(f"{release.get('id', 'release')}-{name}")
            try:
                download(clean_text(asset.get("browser_download_url")), path, token)
                plist, icon_data = read_ipa(path)
            except (OSError, ValueError, zipfile.BadZipFile, plistlib.InvalidFileException) as exc:
                raise ValueError(f"Could not read release asset {name}: {exc}") from exc
            items.append(
                {
                    "plist": plist,
                    "icon_data": icon_data,
                    "version": release_version(release, plist),
                    "date": iso_date(clean_text(release.get("published_at"), clean_text(release.get("created_at")))),
                    "downloadURL": clean_text(asset.get("browser_download_url")),
                    "size": int(asset.get("size") or path.stat().st_size),
                    "description": release_description(release),
                    "beta": bool(release.get("prerelease")),
                    "source_name": name,
                }
            )
    return items


def version_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return clean_text(item.get("date")), clean_text(item.get("version"))


def build_app(
    bundle_id: str,
    items: list[dict[str, Any]],
    override: dict[str, Any],
    config: dict[str, Any],
    base_url: str,
    icon_dir: Path,
) -> dict[str, Any]:
    items = sorted(items, key=version_sort_key, reverse=True)
    latest = items[0]
    plist = latest["plist"]
    name = clean_text(override.get("name"), bundle_display_name(plist, bundle_id))
    developer = clean_text(override.get("developerName"), clean_text(config.get("defaultDeveloperName"), "未知开发者"))
    icon_filename = f"{safe_filename(bundle_id)}.png"
    icon_url: str | None = None
    icon_data = next((item.get("icon_data") for item in items if item.get("icon_data")), None)
    if icon_data:
        icon_dir.mkdir(parents=True, exist_ok=True)
        (icon_dir / icon_filename).write_bytes(icon_data)
        icon_url = public_url(base_url, "icons", icon_filename)

    versions: list[dict[str, Any]] = []
    for item in items:
        version: dict[str, Any] = {
            "version": item["version"],
            "date": item["date"],
            "downloadURL": item["downloadURL"],
            "size": item["size"],
        }
        description = item.get("description")
        if description:
            version["localizedDescription"] = description
        min_os = plist_value(item["plist"], "MinimumOSVersion")
        if min_os:
            version["minOSVersion"] = clean_text(min_os)
        versions.append(version)

    app: dict[str, Any] = {
        "beta": bool(override.get("beta", latest.get("beta", False))),
        "name": name,
        "bundleIdentifier": bundle_id,
        "developerName": developer,
        "subtitle": clean_text(override.get("subtitle"), "") or name,
        "version": latest["version"],
        "versionDate": latest["date"],
        "versionDescription": clean_text(override.get("versionDescription"), clean_text(latest.get("description"))),
        "downloadURL": latest["downloadURL"],
        "localizedDescription": clean_text(override.get("localizedDescription"), name),
        "category": clean_text(override.get("category"), "utilities"),
        "size": latest["size"],
        "versions": versions,
    }
    override_icon_url = clean_text(override.get("iconURL"))
    if override_icon_url or icon_url:
        app["iconURL"] = override_icon_url or icon_url
    for key in ("tintColor", "screenshotURLs", "appPermissions"):
        if key in override:
            app[key] = override[key]
    return app


def validate_source(source: dict[str, Any]) -> None:
    required = ("name", "identifier", "apps")
    missing = [key for key in required if key not in source or source[key] is None]
    if missing:
        raise ValueError(f"source.json is missing required fields: {', '.join(missing)}")
    for key in ("name", "identifier"):
        if not clean_text(source.get(key)):
            raise ValueError(f"source.json field {key} must not be empty")
    if not isinstance(source["apps"], list):
        raise ValueError("source.json apps must be an array")
    seen: set[str] = set()
    for app in source["apps"]:
        if not isinstance(app, dict):
            raise ValueError("each app must be an object")
        for key in ("name", "bundleIdentifier", "versions"):
            if not app.get(key):
                raise ValueError(f"app is missing required field: {key}")
        bundle_id = app["bundleIdentifier"]
        if bundle_id in seen:
            raise ValueError(f"duplicate bundleIdentifier: {bundle_id}")
        seen.add(bundle_id)
        if not isinstance(app["versions"], list) or not app["versions"]:
            raise ValueError(f"app {bundle_id} must have at least one version")
        for version in app["versions"]:
            for key in ("version", "downloadURL"):
                if not version.get(key):
                    raise ValueError(f"version of {bundle_id} is missing required field: {key}")


def build(args: argparse.Namespace) -> dict[str, Any]:
    config = read_json(args.config)
    overrides = read_json(args.overrides) if args.overrides.exists() else {}
    if not isinstance(overrides, dict):
        raise ValueError("app-overrides.json must contain an object")

    base_url = normalized_base_url(args.public_base_url or clean_text(config.get("publicBaseURL")))
    if not base_url or "YOUR-USERNAME" in base_url or "YOUR-REPOSITORY" in base_url:
        raise ValueError("Set publicBaseURL in source.config.json or pass --public-base-url")

    if args.ipa_dir:
        items = local_items(args.ipa_dir)
    elif args.github_repo:
        include_prereleases = bool(config.get("includePrereleases")) or args.include_prereleases
        with tempfile.TemporaryDirectory(prefix="ipa-store-") as temporary:
            items = github_items(args.github_repo, os.environ.get("GITHUB_TOKEN"), include_prereleases, Path(temporary))
    else:
        raise ValueError("Pass --github-repo or --ipa-dir")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        bundle_id = clean_text(plist_value(item["plist"], "CFBundleIdentifier"))
        if not bundle_id:
            raise ValueError(f"{item['source_name']} has no CFBundleIdentifier")
        grouped.setdefault(bundle_id, []).append(item)

    # Keep only the newest copy of an identical version and URL.
    for bundle_id, bundle_items in grouped.items():
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for item in bundle_items:
            unique[(item["version"], item["downloadURL"])] = item
        grouped[bundle_id] = list(unique.values())

    apps = [
        build_app(bundle_id, grouped[bundle_id], overrides.get(bundle_id, {}), config, base_url, args.icon_dir)
        for bundle_id in sorted(grouped, key=lambda value: clean_text(overrides.get(value, {}).get("name"), value).lower())
    ]
    source: dict[str, Any] = {
        "name": clean_text(config.get("name"), "我的 IPA 源"),
        "identifier": clean_text(config.get("identifier"), "com.personal.ipastore.source"),
        "website": clean_text(config.get("website")),
        "subtitle": clean_text(config.get("subtitle")),
        "description": clean_text(config.get("description")),
        "tintColor": clean_text(config.get("tintColor"), "#635BFF"),
        "apps": apps,
    }
    if config.get("iconURL"):
        source["iconURL"] = config["iconURL"]
    if config.get("headerURL"):
        source["headerURL"] = config["headerURL"]
    validate_source(source)
    return source


def main() -> int:
    args = parse_args()
    if args.check:
        source = read_json(args.output)
        validate_source(source)
        print(f"OK: {args.output} ({len(source['apps'])} app(s))")
        return 0

    try:
        source = build(args)
    except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} ({len(source['apps'])} app(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
