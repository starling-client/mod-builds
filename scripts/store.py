#!/usr/bin/env python3
"""Compile v2's store: one library per mod, built against a game build (a Nightly), kept in this
repository's releases, where the game reads it straight from GitHub (the Compile service, worker/, is
asked only for a mod the store doesn't have).

    store/builds.json           every game build the store serves, newest first: its Nightly, the Cargo
                                profile of its APK, and its ABIs (each with the Nightly's APK it was
                                checked against)
    mods-<build>/index.json     that build's mods: id, version and the .hmod's SHA-256, to the
                                library's asset and SHA-256, and the mods that failed there
    mods-<build>/<job>.so       one mod's library (job: the .hmod's SHA-256, 32 digits, and the ABI)
    mods-<build>/<job>.json     its answer, which the index is made from; <job>.failed.json if none

    python3 scripts/store.py register              # BUILD, ABI, PROFILE, NIGHTLY, APK: builds.json gets BUILD
    python3 scripts/store.py env                   # BUILD: its PROFILE into GITHUB_ENV
    python3 scripts/store.py plan OUT              # BUILD, ABIS: the catalog's native mods BUILD lacks
    python3 scripts/store.py fetch PLAN DIR        # each planned .hmod, checked against the catalog
    python3 scripts/store.py publish RESULT        # BUILD (+ JOB, ASK, BUILT for one asked mod)
    python3 scripts/store.py index                 # BUILD: mods-<BUILD>/index.json from its answers

Only `publish`, `index` and `register` write, from jobs that run nothing of the mods: the library is
checked and uploaded, and each answer is read as JSON and written again with the fields the game
reads, so nothing a mod made can say more than that.
"""
import calendar
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import registry_v2

SCHEMA = 1
STORE = "store"
BUILDS = "builds.json"
INDEX = "index.json"
CATALOG = "https://horizon-rust.github.io/horizon-mods/index.json"
# Sunrise's own catalog, which a game built on Sunrise lists beside Horizon's.
# Each ABI the store builds for, and the Rust target its SDK slices are named after.
TARGETS = {
    "arm64-v8a": "aarch64-linux-android",
    "armeabi-v7a": "armv7-linux-androideabi",
    "x86_64": "x86_64-linux-android",
    "x86": "i686-linux-android",
}
BUILD = re.compile(r"[0-9a-f]{40}")
JOB = re.compile(r"[0-9a-f]{32}\.(%s)" % "|".join(re.escape(abi) for abi in TARGETS))
SHA256 = re.compile(r"[0-9a-f]{64}")
# What a mod's id and version may be in the index (horizon-mod.toml's rules are stricter).
ID = re.compile(r"[a-z0-9_][a-z0-9_.-]{0,63}")
VERSION = re.compile(r"[0-9A-Za-z.+-]{1,64}")
# A library's name on the phone: one plain file name, as native-mods.json takes it.
LIBRARY = re.compile(r"lib[a-z0-9_]{1,64}\.so")
# What a build's .hmod may be at most (the service's MAX_BODY).
LARGEST_MOD = 32 << 20
DID_NOT_FINISH = "The build on GitHub did not finish. Try again."
# The Cargo profiles an APK is built with: `fast` for players' Nightlies (android.yml, as releases), `quick` for
# tests. A mod is built with its APK's.
PROFILES = ("fast", "quick")


def mods_tag(build):
    return f"mods-{build}"


def job_of(source, abi):
    """The job of a .hmod whose SHA-256 is `source`, for `abi`: the Worker's `modJobOf`."""
    return f"{source[:32]}.{abi}"


def repo():
    return os.environ["GH_REPO"]


def download_url(tag, name):
    return f"https://github.com/{repo()}/releases/download/{tag}/{name}"


def fetch(url, limit=LARGEST_MOD):
    """The bytes at `url`, or None on a 404."""
    request = urllib.request.Request(url, headers={"User-Agent": "starling-mod-builds"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if len(body) > limit:
        raise ValueError(f"{url}: more than {limit} bytes")
    return body


def fetch_json(url):
    """The JSON at `url`, or None on a 404."""
    body = fetch(url, 64 << 20)
    return None if body is None else json.loads(body)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# ---- the answer of one build ----------------------------------------------------------------------


def answer(result, job, built):
    """What a build's `result` folder says, as the store keeps it: ("done", status, library) or
    ("failed", failure, None). `result/<job>.status.json` is what scripts/mod_lib.py wrote, beside the
    library it names; only the fields below are kept, each checked."""
    status = None
    try:
        status = json.loads((result / f"{job}.status.json").read_text())
    except (OSError, ValueError):
        pass
    if not isinstance(status, dict):
        status = None
    about = about_of(status or {}, job)
    if built and status and status.get("state") == "done" and about:
        library = result / f"{job}.so"
        name = status.get("library")
        size, want = status.get("size"), status.get("sha256")
        identity = status.get("identity")
        if (
            isinstance(name, str)
            and LIBRARY.fullmatch(name)
            and isinstance(identity, str)
            and len(identity) <= 400
            and identity.split(" ")[:1] == [about.get("build")]
            and library.is_file()
            and library.stat().st_size == size
            and sha256_of(library) == want
        ):
            done = {"state": "done", **about, "library": name, "sha256": want, "size": size, "identity": identity}
            return "done", done, library
        return "failed", failure(about or {}, "The build came out different from what it said.", False), None
    if built and status and status.get("state") == "failed":
        error = str(status.get("error") or "The build failed.")[:600]
        return "failed", failure(about or {}, error, bool(status.get("settled"))), None
    return "failed", failure(about or {}, DID_NOT_FINISH, False), None


def about_of(status, job):
    """The mod the answer is of: id, version, the .hmod's SHA-256 and the ABI, when they are plain
    and agree with `job`; else {} (and the index leaves it out)."""
    abi = job.split(".", 1)[1] if JOB.fullmatch(job) else None
    build = status.get("build")
    mod, version, source = status.get("id"), status.get("version"), status.get("source")
    if not (
        abi
        and isinstance(build, str) and BUILD.fullmatch(build)
        and isinstance(mod, str) and ID.fullmatch(mod)
        and isinstance(version, str) and VERSION.fullmatch(version)
        and isinstance(source, str) and SHA256.fullmatch(source)
        and job_of(source, abi) == job
    ):
        return {}
    return {"id": mod, "version": version, "source": source, "abi": abi, "build": build}


def failure(about, error, settled):
    return {"state": "failed", **about, "error": error, "settled": settled, "at": int(time.time())}


def ensure_release(gh, tag, title):
    """Release `tag`, made as a prerelease when it isn't there."""
    if gh("release", "view", tag, "--json", "tagName", check=False, quiet=True) != 0:
        gh("release", "create", tag, "--prerelease", "--title", title, "--notes", "", check=False)


def publish(result, build, gh, out, ask=None, built=True):
    """Puts the answer of every job in `result` up in mods-<build>, the library before its answer, and
    lets the sealed request go (one asked mod: `ask`, from the Compile service). The jobs are the
    `*.status.json` in `result`, and for an asked mod JOB even when its build left nothing."""
    if not BUILD.fullmatch(build) or (ask is not None and not re.fullmatch(r"[0-9a-f]{32}", ask)):
        sys.exit(f"Not a request: {build} {ask}")
    jobs = sorted({p.name[: -len(".status.json")] for p in result.glob("*.status.json")} if result.is_dir() else set())
    asked = os.environ.get("JOB")
    if asked:
        if not JOB.fullmatch(asked):
            sys.exit(f"Not a job: {asked}")
        jobs = [asked]
    jobs = [job for job in jobs if JOB.fullmatch(job)]
    tag = mods_tag(build)
    ensure_release(gh, tag, f"Mods for {build[:12]}")
    out.mkdir(parents=True, exist_ok=True)
    told = []
    for job in jobs:
        state, status, library = answer(result, job, built)
        if state == "done":
            so = out / f"{job}.so"
            os.replace(library, so)
            gh("release", "upload", tag, str(so), "--clobber")
            (out / f"{job}.json").write_text(json.dumps(status))
            gh("release", "upload", tag, str(out / f"{job}.json"), "--clobber")
            gh("release", "delete-asset", tag, f"{job}.failed.json", "-y", check=False)
        else:
            (out / f"{job}.failed.json").write_text(json.dumps(status))
            gh("release", "upload", tag, str(out / f"{job}.failed.json"), "--clobber")
        told.append({k: v for k, v in status.items() if k in ("id", "version", "state", "error")})
    if ask:
        gh("release", "delete-asset", "requests", f"{ask}.bin", "-y", check=False)
    print(json.dumps(told))


# ---- the index of one build -----------------------------------------------------------------------


def index(build, listed, read, previous=None):
    """mods-<build>/index.json from the release's assets `listed` ({name, id}), reading an answer
    (`read(name)`, its JSON) only when the index before (`previous`) doesn't hold it as it is: the
    index job runs after every build, one at a time. A library gone (clean.py) takes its mod out."""
    names = {a["name"]: a for a in listed}
    kept = {}
    for row in (previous or {}).get("mods", []) + (previous or {}).get("failed", []):
        if isinstance(row, dict) and isinstance(row.get("job"), str):
            kept[(row["job"], row.get("state"))] = row
    mods, failed = [], []
    for name, asset in sorted(names.items()):
        done = name.endswith(".json") and not name.endswith(".failed.json") and name != INDEX
        job = name[: -len(".failed.json")] if name.endswith(".failed.json") else name[: -len(".json")]
        if not (done or name.endswith(".failed.json")) or not JOB.fullmatch(job):
            continue
        # An answer the build replaced since (built again) is read again: its asset is another.
        row = kept.get((job, "done" if done else "failed"))
        if not row or row.get("asset") != asset["id"]:
            status = read(name)
            row = entry(status, job, asset["id"]) if isinstance(status, dict) else None
        if not row or row.get("build", build) != build:
            continue
        # A job once done is never built again (the Worker answers it at once), so its answer
        # stands over any failure before it that publish couldn't delete.
        if done:
            if f"{job}.so" in names:
                mods.append(row)
        # Its library there, the job is done, though its answer was pruned (`prunable`).
        elif f"{job}.json" not in names and f"{job}.so" not in names:
            failed.append(row)
    # Rows whose answers were pruned (`prunable`): a done one while its library is there, a failure for good always.
    seen = {row["job"] for row in mods + failed}
    for (job, state), row in sorted(kept.items()):
        if job in seen or job.startswith("unplayable-") or row.get("build", build) != build:
            continue
        if state == "done" and f"{job}.json" not in names and f"{job}.so" in names:
            mods.append(row)
            seen.add(job)
        elif state == "failed" and row.get("settled") and f"{job}.failed.json" not in names \
                and f"{job}.json" not in names and f"{job}.so" not in names:
            failed.append(row)
            seen.add(job)
    mods.sort(key=lambda row: row["job"])
    built = {row["id"] for row in mods}
    for mod, why in sorted(unplayable().items()):
        if mod not in built:
            failed.append({"state": "failed", "job": f"unplayable-{mod}", "id": mod, "error": why,
                           "settled": True})
    return {"schema": SCHEMA, "build": build, "mods": mods, "failed": failed}


def unplayable():
    """scripts/unplayable.json: catalog mods no build plays (Horizon's WebAssembly mods), by id."""
    rows = json.loads((pathlib.Path(__file__).parent / "unplayable.json").read_text(encoding="utf-8"))
    return {k: v for k, v in rows.items() if ID.fullmatch(k)}


def entry(status, job, asset):
    """One row of the index from an answer, with the fields the game reads, or None."""
    about = about_of(status, job)
    if not about:
        return None
    if status.get("state") == "done":
        name, sha, size, identity = (status.get(k) for k in ("library", "sha256", "size", "identity"))
        if not (
            isinstance(name, str) and LIBRARY.fullmatch(name)
            and isinstance(sha, str) and SHA256.fullmatch(sha)
            and isinstance(size, int) and size > 0
            and isinstance(identity, str)
        ):
            return None
        return {"state": "done", "job": job, **about, "file": f"{job}.so", "library": name, "sha256": sha,
                "size": size, "identity": identity, "asset": asset}
    if status.get("state") == "failed":
        return {"state": "failed", "job": job, **about, "error": str(status.get("error", ""))[:600],
                "settled": bool(status.get("settled")), "asset": asset}
    return None


def release_assets(tag):
    pages = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo()}/releases/tags/{tag}"],
        check=True, capture_output=True,
    ).stdout
    release = json.loads(pages)[0]
    pages = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo()}/releases/{release['id']}/assets?per_page=100"],
        check=True, capture_output=True,
    ).stdout
    return [{"name": a["name"], "id": a["id"], "created_at": a.get("created_at", "")}
            for page in json.loads(pages) for a in page]


# An answer the index holds goes after this long: a phone that asked for the job has read it by then (it waits on the
# answer file itself, and the Worker lets a request go after six hours, clean.py).
ANSWER_HOURS = 6


def prunable(made, listed, now=None):
    """The assets of `listed` that are answers `made` (the index just uploaded) holds and that are older than
    ANSWER_HOURS: a done job's `<job>.json` while its library is there, a failure for good's `<job>.failed.json`."""
    now = time.time() if now is None else now
    done = {row["job"] for row in made.get("mods", [])}
    settled = {row["job"] for row in made.get("failed", []) if row.get("settled")}
    out = []
    for asset in listed:
        name = asset["name"]
        job = name[: -len(".failed.json")] if name.endswith(".failed.json") else name[: -len(".json")]
        # A failure before the job was done is old news too.
        held = (name.endswith(".failed.json") and (job in settled or job in done)) or (
            name.endswith(".json") and not name.endswith(".failed.json") and job in done)
        if not held or name == INDEX:
            continue
        try:
            made_at = calendar.timegm(time.strptime(asset.get("created_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if now - made_at > ANSWER_HOURS * 3600:
            out.append(asset)
    return out


def write_index(build, gh, out):
    tag = mods_tag(build)
    listed = release_assets(tag)
    previous = fetch_json(download_url(tag, INDEX)) if any(a["name"] == INDEX for a in listed) else None
    made = index(build, listed, lambda name: fetch_json(download_url(tag, name)), previous)
    out.mkdir(parents=True, exist_ok=True)
    (out / INDEX).write_text(json.dumps(made, indent=1))
    gh("release", "upload", tag, str(out / INDEX), "--clobber")
    print(f"{tag}: {len(made['mods'])} mods, {len(made['failed'])} failed")
    # Only once the index that holds them is up: GitHub keeps 1000 assets per release.
    gone = prunable(made, listed)
    for asset in gone:
        gh("api", "-X", "DELETE", f"repos/{repo()}/releases/assets/{asset['id']}", check=False, quiet=True)
    if gone:
        print(f"{tag}: {len(gone)} answers the index holds pruned, {len(listed) - len(gone)} assets left")


# ---- the builds the store serves -------------------------------------------------------------------


def register(builds, build, nightly, profile, abi, apk=None, date=None, game=None):
    """builds.json with game build `build` for `abi`, first (newest first by date): its Nightly, the Cargo
    profile its APK is built with (a mod is built with the same), and the Nightly's APK the base was checked
    against. A build already there gets the ABI added, or its row replaced for that ABI."""
    if not BUILD.fullmatch(build) or abi not in TARGETS or profile not in PROFILES:
        raise ValueError(f"not a build to register: {build} {abi} {profile}")
    if apk and not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/releases/download/[\w.+-]+/[\w.+-]+\.apk", apk):
        raise ValueError(f"an APK is a GitHub release's download, not {apk}")
    rows = [b for b in (builds or {}).get("builds", []) if isinstance(b, dict) and BUILD.fullmatch(str(b.get("build")))]
    row = next((b for b in rows if b["build"] == build), None)
    if row is None:
        row = {"build": build, "abis": {}}
        rows.append(row)
    row.update({k: v for k, v in {"nightly": nightly, "date": date or time.strftime("%Y-%m-%d", time.gmtime()),
                                  "game": game, "profile": profile}.items() if v or k not in row})
    row.setdefault("abis", {})[abi] = {"target": TARGETS[abi], **({"apk": apk} if apk else {})}
    rows.sort(key=lambda b: str(b.get("date") or ""), reverse=True)
    return {"schema": SCHEMA, "builds": rows}


def build_row(build):
    """Build `build`'s row of the store's builds.json, or None."""
    listed = fetch_json(download_url(STORE, BUILDS)) or {}
    return next((b for b in listed.get("builds", []) if isinstance(b, dict) and b.get("build") == build), None)


# ---- what the catalog asks for --------------------------------------------------------------------


# What a failure for good said of a mod the build route has since learned to take (mod_lib.py GAME_CRATES): asked again.
# "uses the crate X:" and "chooses features of" were refusals of any crate but a few (before 2026-11-23): a crate the
# game's Cargo.lock has, and a mod's own, are now built ("uses the crate X, which the game doesn't have" is for good).
# "has [native.mixin…]" was a refusal of every mod with [native.*] rows (before 2026-11-24): a staged base builds them.
NOW_TAKEN = re.compile(r"uses the crate [\w-]+:|chooses features of|makes the game's crates build again|"
                       r"The game's crates built again|has \[native\.")


def port_digest(mod):
    """The SHA-256 of Starling's port of `mod` (scripts/ports/<mod>.patch), or None."""
    path = pathlib.Path(__file__).resolve().parent / "ports" / f"{mod}.patch"
    return hashlib.sha256(path.read_bytes()).hexdigest() if ID.fullmatch(str(mod)) and path.is_file() else None


def plan(catalog, build, abis, have, game=None, rebuild=()):
    """The catalog's native mods to build for `build`: every version of each (not revoked, for the
    build's vanilla `game` when it says one) and each ABI, and the newest version of each other mod, for
    mod_lib.py to say whether it is a wasm mod (the game's catalogs leave those out); but those its index
    `have` holds done or
    failed for good (unless for a reason the route now takes, NOW_TAKEN). Each: id, version, the .hmod's url,
    sha256 and size, its job."""
    revoked = {(r.get("id"), r.get("sha256")) for r in catalog.get("revoked", []) if isinstance(r, dict)}
    # `rebuild`: mods built again although the index holds them (a port fixed after they were built).
    held = {row["job"] for row in (have or {}).get("mods", []) if row.get("id") not in rebuild}
    held |= {row["job"] for row in (have or {}).get("failed", [])
             if row.get("settled") and not NOW_TAKEN.search(str(row.get("error", "")))
             # A port made or changed since the failure: built again with it.
             and port_digest(row.get("id")) == row.get("port")}
    planned, seen = [], set()
    for project in catalog.get("projects", []):
        if project.get("kind") not in ("native-mod", "mod") or not ID.fullmatch(str(project.get("id", ""))):
            continue
        if (project["id"], None) in revoked:
            continue
        versions = project.get("versions", [])
        for version in versions if project["kind"] == "native-mod" else versions[:1]:
            file = version.get("file") or {}
            source = str(file.get("sha256", ""))
            if not SHA256.fullmatch(source) or (project["id"], source) in revoked:
                continue
            if game and version.get("game") not in (None, game):
                continue
            if not str(file.get("url", "")).startswith("https://"):
                continue
            if not (0 < int(file.get("size", 0)) <= LARGEST_MOD):
                continue
            for abi in abis:
                job = job_of(source, abi)
                if job in held or job in seen:
                    continue
                seen.add(job)
                planned.append({"id": project["id"], "version": str(version.get("version", "")), "url": file["url"],
                                "sha256": source, "size": int(file["size"]), "abi": abi, "job": job})
    return planned


def fetch_planned(planned, into):
    """Each planned .hmod into `into` as <sha256>.hmod, checked against the catalog's SHA-256; one
    that doesn't match is left out (and so not built)."""
    into.mkdir(parents=True, exist_ok=True)
    kept = []
    for row in planned:
        path = into / f"{row['sha256']}.hmod"
        if not path.is_file():
            body = fetch(row["url"])
            if body is None or hashlib.sha256(body).hexdigest() != row["sha256"]:
                print(f"{row['id']} {row['version']}: not the catalog's file, left out")
                continue
            path.write_bytes(body)
        kept.append(row)
    return kept


# ---- the GitHub CLI --------------------------------------------------------------------------------


def run_gh(*args, check=True, quiet=False):
    done = subprocess.run(["gh", *args], check=check, capture_output=quiet)
    return done.returncode


def abis():
    listed = [a.strip() for a in os.environ.get("ABIS", "arm64-v8a").split(",") if a.strip()]
    unknown = [a for a in listed if a not in TARGETS]
    if unknown:
        sys.exit(f"Not an ABI: {', '.join(unknown)}")
    return listed


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    build = os.environ.get("BUILD", "")
    if not BUILD.fullmatch(build):
        sys.exit(f"Not a build: {build!r}")
    if command == "publish":
        ask = os.environ.get("ASK") or None
        publish(pathlib.Path(argv[2]), build, run_gh, pathlib.Path("answer"), ask,
                os.environ.get("BUILT", "success") == "success")
    elif command == "index":
        write_index(build, run_gh, pathlib.Path("answer"))
    elif command == "plan":
        # Horizon's index has been gone since GitHub flagged its organisation (26/11), and Starling
        # takes no new Horizon mods: a catalog that isn't there plans nothing, Sunrise's still does.
        # Sunrise serves only its signed v2 index since 26/11 (registry_v2.py), checked before it is read.
        catalog = fetch_json(CATALOG) or {}
        other = registry_v2.fetch_catalog(lambda url: fetch(url, 64 << 20)) or {}
        catalog = {**catalog, "projects": [*catalog.get("projects", []), *other.get("projects", [])],
                   "revoked": [*catalog.get("revoked", []), *other.get("revoked", [])]}
        known = build_row(build)
        if not known:
            sys.exit(f"{build} is not in the store's {BUILDS}: run store-base.yml for it first")
        have = fetch_json(download_url(mods_tag(build), INDEX))
        rebuild = set(os.environ.get("REBUILD", "").split())
        planned = plan(catalog, build, [a for a in abis() if a in known["abis"]], have, known.get("game"), rebuild)
        pathlib.Path(argv[2]).write_text(json.dumps(planned, indent=1))
        print(f"{len(planned)} to build for {build[:12]}")
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a") as f:
                f.write(f"count={len(planned)}\n")
    elif command == "fetch":
        planned = json.loads(pathlib.Path(argv[2]).read_text())
        kept = fetch_planned(planned, pathlib.Path(argv[3]))
        pathlib.Path(argv[2]).write_text(json.dumps(kept, indent=1))
    elif command == "env":
        # The build's Cargo profile, for the base's cache key and the build itself.
        row = build_row(build)
        if not row:
            sys.exit(f"{build} is not in the store's {BUILDS}: run store-base.yml for it first")
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write(f"PROFILE={row.get('profile') or 'fast'}\n")
    elif command == "register":
        made = register(fetch_json(download_url(STORE, BUILDS)), build, os.environ.get("NIGHTLY") or None,
                        os.environ.get("PROFILE", "fast"), os.environ["ABI"], os.environ.get("APK") or None)
        ensure_release(run_gh, STORE, "Compile store")
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / BUILDS
            path.write_text(json.dumps(made, indent=1))
            run_gh("release", "upload", STORE, str(path), "--clobber")
        print(f"{BUILDS}: {len(made['builds'])} builds")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
