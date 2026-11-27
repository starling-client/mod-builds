#!/usr/bin/env python3
"""Keeps the releases the Compile service uses small: sealed mods older than six hours (a run that never
came, or one whose answer could not be put up: a build waits and runs well under that), and builds
older than RESULT_DAYS or beyond the newest RESULTS_KEPT (a release holds 1000 assets at most). Asking
for the same mods again builds them again."""
import calendar
import json
import os
import sys
import pathlib
import subprocess
import time

RESULT_DAYS = 14
RESULTS_KEPT = 900
LEGACY_KEPT = 600
REQUEST_DAYS = 0.25


def gh_json(*args):
    return json.loads(subprocess.run(["gh", "api", *args], check=True, capture_output=True).stdout)


def assets(tag):
    release = gh_json(f"repos/{os.environ['GH_REPO']}/releases/tags/{tag}")
    pages = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{os.environ['GH_REPO']}/releases/{release['id']}/assets?per_page=100"],
        check=True,
        capture_output=True,
    ).stdout
    return [a for page in json.loads(pages) for a in page]


def stale(listed, days, kept, now):
    """The assets to delete: older than `days`, or beyond the `kept` newest."""
    newest = sorted(listed, key=lambda a: a["created_at"], reverse=True)
    old = now - days * 86400
    return [
        a
        for i, a in enumerate(newest)
        if i >= kept or calendar.timegm(time.strptime(a["created_at"], "%Y-%m-%dT%H:%M:%SZ")) < old
    ]


# A build's mods release (mods-<build>) past this many assets is pruned (a release holds 1000 at most: a full one
# took no new library, so Compile failed for every mod not built yet, 2026-11-23).
MODS_PRUNE_AT = 800


def mods_prunable(listed, index):
    """The assets of a mods-<build> release that can go, oldest kind first: an answer that may pass another time
    (a failure not settled, built again when asked), a failure a library has replaced, and a library of a mod
    whose newer version (by when it was built) is there for the same ABI. A phone with the older version asks
    for it again and it is built again."""
    by_name = {a["name"]: a for a in listed}
    gone = []
    for row in index.get("failed", []):
        job = row.get("job", "")
        if not row.get("settled") or f"{job}.json" in by_name:
            gone.append(f"{job}.failed.json")
    newest = {}
    for row in index.get("mods", []):
        key = (row.get("id"), row.get("job", "").partition(".")[2])
        # When it was built: its answer's time, or its library's once the answer was pruned (store.prunable).
        made = (by_name.get(f"{row.get('job')}.json") or by_name.get(f"{row.get('job')}.so") or {}).get("created_at", "")
        newest.setdefault(key, []).append((made, row["job"]))
    for rows in newest.values():
        for _, job in sorted(rows, reverse=True)[1:]:
            gone += [f"{job}.so", f"{job}.json"]
    return [by_name[name] for name in gone if name in by_name]


def prune_mods(now):
    """Each mods-<build> release near the limit, pruned (mods_prunable), its index written again."""
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import store
    repo = os.environ["GH_REPO"]
    tags = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/releases?per_page=100", "-q",
                           '.[] | select(.tag_name | startswith("mods-")) | .tag_name'],
                          check=True, capture_output=True, text=True).stdout.split()
    for tag in tags:
        listed = assets(tag)
        if len(listed) < MODS_PRUNE_AT:
            continue
        build = tag.removeprefix("mods-")
        index = store.fetch_json(store.download_url(tag, store.INDEX)) or {}
        gone = mods_prunable(listed, index)
        for asset in gone:
            subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{repo}/releases/assets/{asset['id']}"], check=True)
        print(f"{tag}: {len(listed)} assets, {len(gone)} deleted")
        if gone:
            store.write_index(build, store.run_gh, pathlib.Path("answer"))


def main():
    now = time.time()
    prune_mods(now)
    shards = [(f"results-{c}", RESULT_DAYS, RESULTS_KEPT) for c in "0123456789abcdef"]
    # "results" is the release before the shards: a release holds 1000 assets at most, and one
    # filled in the first evening, so builds go to results-<first hex digit of the job> now.
    for tag, days, kept in [("requests", REQUEST_DAYS, 10**9), ("results", RESULT_DAYS, LEGACY_KEPT), *shards]:
        gone = stale(assets(tag), days, kept, now)
        for asset in gone:
            subprocess.run(
                ["gh", "api", "-X", "DELETE", f"repos/{os.environ['GH_REPO']}/releases/assets/{asset['id']}"],
                check=True,
            )
        print(f"{tag}: {len(gone)} deleted")


if __name__ == "__main__":
    main()
