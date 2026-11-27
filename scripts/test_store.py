"""store.py and mod_lib.py with a GitHub CLI and a compiler of their own: python -m unittest discover -s scripts"""
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import textwrap
import unittest
import zipfile

import mod_lib
import store

BUILD = "5e1f0c2d3a4b5c6d7e8f90a1b2c3d4e5f6a7b8c9"
RUST = "1.92.0"


def hmod(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buffer.getvalue()


MANIFEST = "id = 'veinminer'\nversion = '0.3.0'\napi = '0.2'\n[native]\ngame = '26.3'\nhorizon = 'x'\nmixins = 'y'\n"
CARGO = "[package]\nname = 'veinminer'\nversion = '0.3.0'\nedition = '2024'\n[dependencies]\nhorizon-mod-api = { path = '.horizon/sdk/api/crates/horizon-mod-api' }\n"
MOD = hmod({"horizon-mod.toml": MANIFEST, "Cargo.toml": CARGO, "src/lib.rs": "pub fn hooks() {}"})
# Starling's tools/android_native_sdk.py `mods`, as the base's checkout has it: the library and native-mods.json in
# OUT, and the error a mod that doesn't compile makes Cargo say.
ANDROID_SDK = textwrap.dedent(
    r"""
    import hashlib, json, os, pathlib, sys, tomllib, zipfile
    out, hmod = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    args = dict(zip(sys.argv[4::2], sys.argv[5::2]))
    assert os.environ["RUSTFLAGS"] == "-C prefer-dynamic"
    with zipfile.ZipFile(hmod) as z:
        manifest = tomllib.loads(z.read("horizon-mod.toml").decode())
        if "src/again.rs" in z.namelist():
            print("   Compiling serde_json v1.0.151\n   Compiling hmod-veinminer v0.3.0 (/x)", file=sys.stderr)
        else:
            print("   Compiling hmod-veinminer v0.3.0 (/x)", file=sys.stderr)
        if "src/broken.rs" in z.namelist():
            sys.exit("error[E0425]: cannot find value `x` in this scope; could not compile `hmod-veinminer`")
    out.mkdir(parents=True)
    library = out / f"lib{manifest['id']}.so"
    library.write_bytes(b"ELF " + args["--abi"].encode())
    identity = f"{os.environ['HORIZON_GAME_BUILD_ID']} {manifest['id']} {manifest['version']} RUST_RELEASE"
    (out / "native-mods.json").write_text(json.dumps({"libraries": [{"file": library.name, "mod": manifest["id"],
        "sha256": hashlib.sha256(library.read_bytes()).hexdigest(), "identity": identity}]}))
    """.replace("RUST_RELEASE", RUST)
)


class Builds(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.mods = self.dir / "mods"
        self.mods.mkdir()
        self.result = self.dir / "result"
        self.result.mkdir()
        self.source = self.dir / "source"
        (self.source / "tools").mkdir(parents=True)
        (self.source / "tools" / "android_native_sdk.py").write_text(ANDROID_SDK)

    def job(self, body, abi="arm64-v8a"):
        source = hashlib.sha256(body).hexdigest()
        (self.mods / f"{source}.hmod").write_bytes(body)
        return {"job": store.job_of(source, abi), "abi": abi, "sha256": source}

    def build(self, body):
        row = self.job(body)
        return mod_lib.build_one(row, self.mods, self.source, self.result, BUILD, "quick"), row["job"]

    def test_a_mod_builds_into_its_library_and_says_who_it_is(self):
        status, job = self.build(MOD)
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["identity"], f"{BUILD} veinminer 0.3.0 {RUST}")
        self.assertEqual(status["library"], "libveinminer.so")
        library = self.result / f"{job}.so"
        self.assertEqual(library.read_bytes(), b"ELF arm64-v8a")
        self.assertEqual(status["sha256"], hashlib.sha256(library.read_bytes()).hexdigest())
        # And store.py reads it back as the store keeps it.
        state, kept, _ = store.answer(self.result, job, True)
        self.assertEqual(state, "done")
        self.assertEqual(kept["id"], "veinminer")
        self.assertEqual(kept["source"], hashlib.sha256(MOD).hexdigest())

    def test_what_the_api_route_does_not_take_fails_for_good(self):
        cases = {
            "build script": {"build.rs": "fn main() {}"},
            "crate": {"Cargo.toml": CARGO + "rand = '0.8'\n"},
            "features": {"Cargo.toml": CARGO + "[features]\nx = []\n"},
        }
        for why, files in cases.items():
            body = hmod({"horizon-mod.toml": MANIFEST, "Cargo.toml": CARGO, "src/lib.rs": "", **files})
            status, _ = self.build(body)
            self.assertEqual(status["state"], "failed", why)
            self.assertTrue(status["settled"], why)
            self.assertIn("whole-game Compile", status["error"], why)

    def test_a_crate_of_the_games_own_is_taken_plainly_and_only_while_the_game_stays_as_built(self):
        files = {"horizon-mod.toml": MANIFEST, "src/lib.rs": ""}
        for line in ("serde_json = '1'\n", "sha2 = { version = '0.10' }\n", "log = '0.4'\n"):
            status, _ = self.build(hmod({**files, "Cargo.toml": CARGO + line}))
            self.assertEqual(status["state"], "done", (line, status))
        # Features of a crate the game has are taken (serde's derive, say).
        status, _ = self.build(hmod({**files, "Cargo.toml": CARGO + "serde_json = { version = '1', features = ['raw_value'] }\n"}))
        self.assertEqual(status["state"], "done", status)
        for line in ("log = { git = 'https://example.com/log' }\n", "rand = '0.8'\n",
                     "core = { path = '../outside' }\n"):
            status, _ = self.build(hmod({**files, "Cargo.toml": CARGO + line}))
            self.assertEqual((status["state"], status["settled"]), ("failed", True), line)
            self.assertIn("whole-game Compile", status["error"], line)
        # The game's crates compiled again: no APK has them so.
        status, _ = self.build(hmod({**files, "Cargo.toml": CARGO + "serde_json = '1'\n", "src/again.rs": ""}))
        self.assertEqual(status["state"], "done", status)

    def test_a_crate_of_the_mods_own_inside_its_package_is_taken_by_the_same_rules(self):
        files = {"horizon-mod.toml": MANIFEST, "src/lib.rs": "", "core/src/lib.rs": ""}
        own = "[package]\nname = 'core'\nversion = '1.0.0'\n[features]\naudio = []\n[dependencies]\nserde_json = '1'\n"
        status, _ = self.build(hmod({**files, "Cargo.toml": CARGO + "core = { path = 'core' }\n", "core/Cargo.toml": own}))
        self.assertEqual(status["state"], "done", status)
        for inner in (own + "rand = '0.8'\n", own + "[build-dependencies]\ncc = '1'\n"):
            status, _ = self.build(hmod({**files, "Cargo.toml": CARGO + "core = { path = 'core' }\n", "core/Cargo.toml": inner}))
            self.assertEqual((status["state"], status["settled"]), ("failed", True), inner)

    def test_a_wasm_mod_is_told_it_does_not_run_on_starling_and_content_alone_is_no_native_mod(self):
        plain = "id = 'zoom'\nversion = '3.0.3'\napi = '0.2'\n"
        status, _ = self.build(hmod({"horizon-mod.toml": plain + "[wasm]\nsha512 = 'x'\n", "mod.wasm": "\0asm"}))
        self.assertEqual((status["state"], status["settled"], status["id"], status["version"]),
                         ("failed", True, "zoom", "3.0.3"), status)
        self.assertIn("doesn't run on Starling", status["error"])
        status, _ = self.build(hmod({"horizon-mod.toml": plain, "assets/zoom/lang/en_us.json": "{}"}))
        self.assertEqual((status["state"], status["settled"], status["id"]), ("failed", True, "zoom"), status)
        self.assertNotIn("doesn't run on Starling", status["error"])

    def test_a_file_outside_the_mod_or_a_mod_not_native_is_refused(self):
        for body in (hmod({"../evil": "x", "horizon-mod.toml": MANIFEST, "Cargo.toml": CARGO}),
                     hmod({"horizon-mod.toml": "id = 'x'\nversion = '1'\n", "Cargo.toml": CARGO}),
                     b"not a zip at all"):
            status, _ = self.build(body)
            self.assertEqual(status["state"], "failed")
            self.assertTrue(status["settled"])
        self.assertFalse((self.dir / "evil").exists())

    def test_a_package_refused_before_its_manifest_is_still_an_answer_about_the_catalogs_mod(self):
        row = {**self.job(b"not a zip at all"), "id": "wrench", "version": "1.0.0"}
        status = mod_lib.build_one(row, self.mods, self.source, self.result, BUILD, "quick")
        self.assertEqual((status["state"], status["id"], status["version"]), ("failed", "wrench", "1.0.0"))
        self.assertEqual(store.about_of(status, row["job"])["id"], "wrench")

    def test_a_compile_error_is_the_mods_own_and_settled(self):
        body = hmod({"horizon-mod.toml": MANIFEST, "Cargo.toml": CARGO, "src/lib.rs": "", "src/broken.rs": ""})
        status, _ = self.build(body)
        self.assertEqual(status["state"], "failed")
        self.assertIn("E0425", status["error"])
        self.assertTrue(status["settled"])

    def test_a_mod_with_native_tables_needs_the_whole_game(self):
        manifest = MANIFEST + "[[native.mixin]]\nclass = 'x'\n"
        status, _ = self.build(hmod({"horizon-mod.toml": manifest, "Cargo.toml": CARGO, "src/lib.rs": ""}))
        self.assertEqual((status["state"], status["settled"]), ("failed", True))
        self.assertIn("[native.mixin]", status["error"])

    def test_a_file_not_the_one_asked_for_is_not_built(self):
        row = self.job(MOD)
        (self.mods / f"{row['sha256']}.hmod").write_bytes(MOD + b"changed")
        status = mod_lib.build_one(row, self.mods, self.source, self.result, BUILD, "quick")
        self.assertEqual(status["state"], "failed")


class Publish(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.calls = []

    def gh(self, *args, check=True, quiet=False):
        uploaded = pathlib.Path(args[3]).read_bytes() if args[:2] == ("release", "upload") else None
        self.calls.append((args, uploaded))
        return 1 if args[:2] == ("release", "view") else 0

    def test_a_built_mod_goes_up_library_first_and_its_request_goes(self):
        builds = Builds()
        builds.setUp()
        _, job = builds.build(MOD)
        import os
        os.environ["JOB"] = job
        try:
            store.publish(builds.result, BUILD, self.gh, self.dir / "answer", "0" * 32)
        finally:
            del os.environ["JOB"]
        names = [c[0] for c in self.calls]
        self.assertIn(("release", "create", f"mods-{BUILD}", "--prerelease", "--title", f"Mods for {BUILD[:12]}",
                       "--notes", ""), names)
        uploads = [pathlib.Path(a[3]).name for a in names if a[1] == "upload"]
        self.assertEqual(uploads, [f"{job}.so", f"{job}.json"])
        self.assertEqual(names[-1], ("release", "delete-asset", "requests", f"{'0' * 32}.bin", "-y"))
        status = json.loads(next(data for args, data in self.calls if args[1] == "upload" and args[3].endswith(".json")))
        self.assertEqual(set(status), {"state", "id", "version", "source", "abi", "build", "library", "sha256", "size",
                                       "identity"})

    def test_a_build_that_never_ended_may_be_asked_again(self):
        job = store.job_of("a" * 64, "arm64-v8a")
        import os
        os.environ["JOB"] = job
        try:
            store.publish(self.dir / "nothing", BUILD, self.gh, self.dir / "answer", "1" * 32, built=False)
        finally:
            del os.environ["JOB"]
        failure = json.loads(next(data for args, data in self.calls if args[1] == "upload"))
        self.assertEqual((failure["error"], failure["settled"]), (store.DID_NOT_FINISH, False))

    def test_names_that_are_not_a_request_stop_it(self):
        with self.assertRaises(SystemExit):
            store.publish(self.dir, "../etc", self.gh, self.dir / "answer", "0" * 32)
        self.assertEqual(self.calls, [])


class Index(unittest.TestCase):
    def status(self, mod, version, abi="arm64-v8a", state="done"):
        source = hashlib.sha256(f"{mod} {version}".encode()).hexdigest()
        job = store.job_of(source, abi)
        about = {"id": mod, "version": version, "source": source, "abi": abi, "build": BUILD}
        if state == "done":
            return job, {"state": "done", **about, "library": f"lib{mod}.so", "sha256": "e" * 64, "size": 10,
                         "identity": f"{BUILD} {mod} {version} {RUST}"}
        return job, {"state": "failed", **about, "error": "error[E0425]", "settled": True, "at": 1}

    def test_the_index_lists_each_mod_with_its_library_and_the_failures(self):
        a, done = self.status("veinminer", "0.3.0")
        b, failed = self.status("wrench", "1.0.0", state="failed")
        answers = {f"{a}.json": done, f"{b}.failed.json": failed}
        listed = [{"name": n, "id": i} for i, n in enumerate([f"{a}.so", f"{a}.json", f"{b}.failed.json", "index.json"])]
        made = store.index(BUILD, listed, answers.get)
        self.assertEqual([m["id"] for m in made["mods"]], ["veinminer"])
        row = made["mods"][0]
        self.assertEqual((row["file"], row["library"], row["source"]), (f"{a}.so", "libveinminer.so", done["source"]))
        self.assertEqual([m["id"] for m in made["failed"] if not m["job"].startswith("unplayable-")], ["wrench"])

    def test_the_index_lists_the_unplayable_mods_as_failed_for_good(self):
        made = store.index(BUILD, [], {}.get)
        zoom = [row for row in made["failed"] if row["id"] == "zoom"]
        self.assertEqual(len(zoom), 1)
        self.assertTrue(zoom[0]["settled"])
        self.assertNotIn("#", [row["id"] for row in made["failed"]])

    def test_the_index_reads_only_answers_it_doesnt_hold_and_drops_a_library_gone(self):
        a, done = self.status("veinminer", "0.3.0")
        b, other = self.status("carryblock", "0.1.0")
        listed = [{"name": f"{a}.so", "id": 1}, {"name": f"{a}.json", "id": 2}]
        first = store.index(BUILD, listed, {f"{a}.json": done}.get)
        read = []
        listed += [{"name": f"{b}.so", "id": 3}, {"name": f"{b}.json", "id": 4}]
        second = store.index(BUILD, listed, lambda name: read.append(name) or other, first)
        self.assertEqual(read, [f"{b}.json"])
        self.assertEqual(sorted(m["id"] for m in second["mods"]), ["carryblock", "veinminer"])
        third = store.index(BUILD, listed[1:], lambda name: read.append(name), second)
        self.assertEqual([m["id"] for m in third["mods"]], ["carryblock"])

    def test_an_answer_pruned_keeps_its_row_while_the_library_stays(self):
        a, done = self.status("veinminer", "0.3.0")
        b, failed = self.status("wrench", "1.0.0", state="failed")
        old, new = "2026-11-20T23:14:48Z", "2026-11-27T12:00:00Z"
        listed = [{"name": f"{a}.so", "id": 1, "created_at": old}, {"name": f"{a}.json", "id": 2, "created_at": old},
                  {"name": f"{b}.failed.json", "id": 3, "created_at": old}, {"name": "index.json", "id": 4,
                                                                             "created_at": old}]
        first = store.index(BUILD, listed, {f"{a}.json": done, f"{b}.failed.json": failed}.get)
        now = store.calendar.timegm((2026, 11, 27, 13, 0, 0, 0, 0, 0))
        self.assertEqual(sorted(x["id"] for x in store.prunable(first, listed, now)), [2, 3])
        # A fresh answer stays: a phone that asked may still be reading it.
        fresh = [dict(x, created_at=new) for x in listed]
        self.assertEqual(store.prunable(first, fresh, now), [])
        # The answers gone, the rows stay: the library is there, and a failure for good is for good.
        left = [x for x in listed if x["id"] not in (2, 3)]
        second = store.index(BUILD, left, lambda name: self.fail(f"read {name}"), first)
        self.assertEqual([m["id"] for m in second["mods"]], ["veinminer"])
        self.assertEqual([m["id"] for m in second["failed"] if not m["job"].startswith("unplayable-")], ["wrench"])
        # The library gone too (clean.py), its mod goes.
        third = store.index(BUILD, [x for x in left if x["id"] != 1], lambda name: None, second)
        self.assertEqual(third["mods"], [])

    def test_an_answer_that_lies_about_its_job_is_left_out(self):
        a, done = self.status("veinminer", "0.3.0")
        done["source"] = "f" * 64
        made = store.index(BUILD, [{"name": f"{a}.so", "id": 1}, {"name": f"{a}.json", "id": 2}], {f"{a}.json": done}.get)
        self.assertEqual(made["mods"], [])


class Plan(unittest.TestCase):
    catalog = {
        "projects": [
            {"id": "veinminer", "kind": "native-mod", "versions": [
                {"version": "0.3.0", "game": "26.3", "file": {"url": "https://x/v.hmod", "size": 9, "sha256": "a" * 64}},
                {"version": "0.4.0", "game": "26.4", "file": {"url": "https://x/v4.hmod", "size": 9, "sha256": "c" * 64}},
            ]},
            {"id": "pack", "kind": "resourcepack", "versions": [{"file": {"url": "https://x", "size": 1, "sha256": "d" * 64}}]},
            {"id": "gone", "kind": "native-mod", "versions": [{"file": {"url": "https://x", "size": 1, "sha256": "b" * 64}}]},
            {"id": "badfile", "kind": "native-mod", "versions": [{"file": {"url": "https://x", "size": 1, "sha256": "e" * 64}}]},
        ],
        "revoked": [{"id": "gone", "reason": "taken down"}, {"id": "badfile", "sha256": "e" * 64}],
    }

    def test_the_catalogs_native_mods_for_this_game_not_revoked_nor_built(self):
        planned = store.plan(self.catalog, BUILD, ["arm64-v8a"], None, "26.3")
        self.assertEqual([(p["id"], p["version"], p["job"]) for p in planned],
                         [("veinminer", "0.3.0", store.job_of("a" * 64, "arm64-v8a"))])
        have = {"mods": [{"job": store.job_of("a" * 64, "arm64-v8a")}], "failed": []}
        self.assertEqual(store.plan(self.catalog, BUILD, ["arm64-v8a"], have, "26.3"), [])
        # A failure that may pass is planned again; one for good is not.
        maybe = {"mods": [], "failed": [{"job": store.job_of("a" * 64, "arm64-v8a"), "settled": False}]}
        self.assertEqual(len(store.plan(self.catalog, BUILD, ["arm64-v8a"], maybe, "26.3")), 1)
        for error, again in (("This mod has [native.mixin]: it needs the whole-game Compile.", 1),
                             ("This mod uses the crate serde_json: it needs the whole-game Compile.", 1),
                             ("This mod uses the crate rand: it needs the whole-game Compile.", 1),
                             ("This mod uses the crate rand, which the game doesn't have: it needs the whole-game Compile.", 0)):
            good = {"mods": [], "failed": [{"job": store.job_of("a" * 64, "arm64-v8a"), "settled": True, "error": error}]}
            self.assertEqual(len(store.plan(self.catalog, BUILD, ["arm64-v8a"], good, "26.3")), again, error)
        self.assertEqual(len(store.plan(self.catalog, BUILD, ["arm64-v8a", "x86_64"], None, "26.3")), 2)
        # Another mod of the registry: only its newest version, for mod_lib.py to say whether it is a wasm mod.
        file = lambda letter: {"url": "https://x/z.hmod", "size": 9, "sha256": letter * 64}
        other = {"projects": [{"id": "zoom", "kind": "mod", "versions": [
            {"version": "3.0.3", "game": "26.3", "file": file("7")}, {"version": "3.0.2", "game": "26.3", "file": file("8")}]}]}
        self.assertEqual([(p["id"], p["version"]) for p in store.plan(other, BUILD, ["arm64-v8a"], None, "26.3")],
                         [("zoom", "3.0.3")])


class Register(unittest.TestCase):
    apk = "https://github.com/starling-client/starling/releases/download/nightly-1/starling-preview-arm64-v8a.apk"

    def test_a_build_comes_first_with_its_profile_and_apk(self):
        older = {"schema": 1, "builds": [{"build": "1" * 40, "date": "2026-11-19", "abis": {}}]}
        made = store.register(older, BUILD, "nightly-1", "quick", "arm64-v8a", self.apk, "2026-11-20")
        self.assertEqual([b["build"] for b in made["builds"]], [BUILD, "1" * 40])
        row = made["builds"][0]
        self.assertEqual((row["nightly"], row["profile"]), ("nightly-1", "quick"))
        self.assertEqual(row["abis"]["arm64-v8a"], {"target": "aarch64-linux-android", "apk": self.apk})
        again = store.register(made, BUILD, None, "quick", "x86_64", None, "2026-11-20")
        self.assertEqual(len(again["builds"]), 2)
        self.assertEqual(sorted(again["builds"][0]["abis"]), ["arm64-v8a", "x86_64"])
        self.assertEqual(again["builds"][0]["nightly"], "nightly-1", "kept")

    def test_what_is_not_a_build_or_an_apk_off_github_is_refused(self):
        with self.assertRaises(ValueError):
            store.register(None, "main", None, "quick", "arm64-v8a")
        with self.assertRaises(ValueError):
            store.register(None, BUILD, None, "release", "arm64-v8a")
        with self.assertRaises(ValueError):
            store.register(None, BUILD, None, "quick", "arm64-v8a", "https://example.com/x.apk")


if __name__ == "__main__":
    unittest.main()
