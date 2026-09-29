"""Discover release streams and adjacent release tags, never adjacent CI runs."""

from html.parser import HTMLParser
import json
import os
import re
import subprocess
from urllib.request import Request, urlopen

KERNEL_FEED = "https://www.kernel.org/releases.json"
CIP_FEED = "https://www.nigauri.org/~iwamatsu/cip-release-term.html"
MAINLINE_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
STABLE_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
CIP_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/cip/linux-cip.git"
MAINLINE = re.compile(r"v(\d+)\.(\d+)(?:-rc(\d+))?")
STABLE = re.compile(r"v(\d+)\.(\d+)(?:\.(\d+))?")
CIP = re.compile(r"v(\d+)\.(\d+)\.(\d+)-cip(\d+)(?:-rt(\d+))?")


def fetch_bytes(url, limit=4_000_000):
    request = Request(url, headers={"User-Agent": "kci-release-review/0.1",
                                    "Cache-Control": "no-cache"})
    with urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"Response exceeds {limit} bytes: {url}")
    return data


def read_tags(url):
    result = subprocess.run(
        ["git", "ls-remote", "--tags", url, "v[0-9]*"],
        capture_output=True, text=True, check=True, timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return parse_tags(result.stdout)


def parse_tags(output):
    """Prefer the peeled commit, not the object ID of an annotated tag."""
    direct, peeled = {}, {}
    for line in output.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{40})\s+refs/tags/(v[^\s]+?)(\^\{\})?", line)
        if not match:
            continue
        commit, tag, is_peeled = match.groups()
        (peeled if is_peeled else direct)[tag] = commit.lower()
    tags = {**direct, **peeled}
    if not tags:
        raise ValueError("Git returned no release tags")
    return tags


def tag_key(tag, stream):
    """Order only tags belonging to this stream, with numeric version fields."""
    kind = stream["kind"]
    pattern = {"mainline": MAINLINE, "stable": STABLE, "cip": CIP}[kind]
    match = pattern.fullmatch(tag)
    if not match:
        return None
    values = match.groups()
    major, minor = map(int, values[:2])
    if kind == "mainline":
        # rc1 follows the previous final; a final follows the last rc.
        return major, minor, 1 if values[2] is None else 0, int(values[2] or 0)
    if (major, minor) != tuple(stream["series"]):
        return None
    if kind == "stable":
        return (int(values[2] or 0),)
    if bool(values[4]) != stream["rt"]:
        return None
    # CIP numbering advances even when the upstream patch level does not.
    return int(values[3]), int(values[4] or 0), int(values[2])


def kernel_streams(document):
    rows = document.get("releases") if isinstance(document, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("kernel.org release feed has no releases array")
    streams = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid kernel.org release entry")
        moniker = row.get("moniker")
        if moniker not in {"mainline", "stable", "longterm"}:
            continue  # linux-next is a development snapshot, not a release.
        version = row.get("version")
        if not isinstance(version, str):
            raise ValueError("Release is missing its version")
        tag = "v" + version
        if MAINLINE.fullmatch(tag):
            stream = {"id": "kernel-mainline", "kind": "mainline", "branch": "master",
                      "giturl": MAINLINE_GIT, "latest": tag, "source": KERNEL_FEED}
        else:
            match = STABLE.fullmatch(tag)
            if not match or moniker == "mainline":
                raise ValueError(f"Unsupported kernel.org version: {version}")
            series = [int(value) for value in match.groups()[:2]]
            branch = f"linux-{series[0]}.{series[1]}.y"
            stream = {"id": "kernel-" + branch.replace(".", "-"), "kind": "stable",
                      "series": series, "branch": branch, "giturl": STABLE_GIT,
                      "latest": tag, "source": KERNEL_FEED}
        previous = streams.get(stream["id"])
        if previous is None or tag_key(tag, stream) > tag_key(previous["latest"], stream):
            streams[stream["id"]] = stream
    if not streams:
        raise ValueError("kernel.org feed has no recognized release streams")
    return list(streams.values())


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "pre", "div", "tr"}:
            self.parts.append("\n")


def cip_streams(document):
    parser = PlainText()
    parser.feed(document)
    content = "".join(parser.parts)
    headers = list(re.finditer(r"(?m)^\s*(linux-(\d+)\.(\d+)\.y-cip(-rt)?):[^\n]*", content))
    if not headers:
        raise ValueError("CIP page has no recognized release branches")
    streams, seen = [], set()
    for index, header in enumerate(headers):
        branch, major, minor, rt = header.groups()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(content)
        versions = re.findall(r"latest version:\s*(\S+)", content[header.end():end])
        if len(versions) != 1 or branch in seen:
            raise ValueError(f"Missing, duplicate or ambiguous CIP release: {branch}")
        seen.add(branch)
        stream = {"id": "cip-" + branch.replace(".", "-"), "kind": "cip",
                  "series": [int(major), int(minor)], "rt": bool(rt), "branch": branch,
                  "giturl": CIP_GIT, "latest": versions[0], "source": CIP_FEED}
        if tag_key(versions[0], stream) is None:
            raise ValueError(f"CIP release does not match its branch: {branch}")
        streams.append(stream)
    return streams


def discover_sources():
    """A failing source is explicit and does not discard the other source."""
    streams, notices, errors = [], [], []
    for label, url, parse in (("kernel.org", KERNEL_FEED, lambda value: kernel_streams(json.loads(value))),
                              ("CIP", CIP_FEED, cip_streams)):
        try:
            streams.extend(parse(fetch_bytes(url).decode("utf-8")))
        except (OSError, ValueError) as exc:
            errors.append(f"{label}: {exc}")
    catalogs = {}
    for url in sorted({stream["giturl"] for stream in streams}):
        try:
            catalogs[url] = read_tags(url)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"Release tags for {url}: {exc}")
    available = []
    for stream in streams:
        tags = catalogs.get(stream["giturl"])
        if tags is None:
            continue
        if stream["latest"] not in tags:
            errors.append(f"{stream['id']}: advertised release {stream['latest']} is absent from Git tags")
            continue
        if stream["kind"] == "cip":
            candidates = [tag for tag in tags if tag_key(tag, stream) is not None]
            latest = max(candidates, key=lambda tag: tag_key(tag, stream))
            if latest != stream["latest"]:
                notices.append(f"{stream['branch']}: CIP page lists {stream['latest']}; official Git tags show {latest}.")
                stream = {**stream, "latest": latest}
        available.append(stream)
    return available, catalogs, notices, errors


def adjacent_releases(stream, tags, previous=None):
    """Bootstrap the newest pair; afterwards include every intervening release."""
    ordered = sorted((tag for tag in tags if tag_key(tag, stream) is not None),
                     key=lambda tag: tag_key(tag, stream))
    latest = stream["latest"]
    if latest not in ordered:
        raise ValueError(f"Release {latest} does not belong to {stream['id']}")
    end = ordered.index(latest)
    if previous is None:
        start = end
    else:
        if previous not in ordered:
            raise ValueError(f"Previously observed tag disappeared: {previous}")
        start = ordered.index(previous) + 1
        if start > end + 1:
            raise ValueError(f"Release source moved backwards from {previous} to {latest}")
    if start == 0:
        raise ValueError(f"No predecessor release for {latest}")
    return [(ordered[index - 1], ordered[index]) for index in range(start, end + 1)]
