"""Tests for corpus.py. They use a fake GitHub, so no internet is needed.

Run with: python3 scripts/test_corpus.py
"""
import base64
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corpus  # noqa: E402

EZ = ["include/EZ-Template/api.hpp", "include/EZ-Template/drive/drive.hpp", "project.pros", "src/autons.cpp"]


def pros(version):
    return json.dumps({"py/state": {"templates": {"EZ-Template": {"py/state": {"version": version}}}}})


class FakeGitHub:
    def __init__(self):
        self.repos, self.trees, self.files, self.forks, self.calls = {}, {}, {}, {}, 0
        self.next_id = 1

    def add(self, full, tree=EZ, fork_of=None, topics=None, private=False, pushed="2026-10-10T00:00:00Z",
            language="C++", version="3.2.2"):
        owner = full.split("/")[0]
        r = {"id": self.next_id, "full_name": full, "html_url": "https://github.com/" + full,
             "owner": {"login": owner, "id": 1000 + sum(map(ord, owner))}, "default_branch": "main",
             "pushed_at": pushed, "topics": topics or [], "fork": bool(fork_of), "private": private,
             "language": language, "forks_count": 0}
        self.next_id += 1
        if fork_of:
            parent = self.repos[fork_of]
            r["parent"] = r["source"] = parent
            parent["forks_count"] += 1
            self.forks.setdefault(fork_of, []).append(r)
        self.repos[full] = r
        self.trees[full] = tree
        self.files[(full, "project.pros")] = pros(version) if tree is EZ else json.dumps({"templates": {}})
        return r

    def get(self, path, search=False):
        self.calls += 1
        path, _, query = urllib.parse.unquote(path).partition("?")
        parts = path.strip("/").split("/")
        if parts[0] == "search":
            qs = urllib.parse.parse_qs(query)["q"][0]
            if int(urllib.parse.parse_qs(query).get("page", ["1"])[0]) > 1:
                return {"items": []}
            hits = [r for r in self.repos.values() if not r["private"] and "ez" in r["full_name"].lower()
                    and (r["fork"] == ("fork:only" in qs))]
            return {"items": hits}
        if parts[0] == "users":
            owner = parts[1]
            mine = [r for r in self.repos.values() if r["owner"]["login"] == owner and not r["private"]]
            if len(parts) == 3:
                return mine
            return {"id": 1000 + sum(map(ord, owner))} if mine or owner in ("teamA", "teamB") else None
        full = parts[1] + "/" + parts[2]
        r = self.repos.get(full)
        if r is None or r["private"]:
            return None
        if len(parts) == 3:
            return r
        if parts[3] == "forks":
            return [f for f in self.forks.get(full, []) if not f["private"]]
        if parts[3] == "git":
            return {"tree": [{"path": p, "type": "blob"} for p in self.trees[full]], "truncated": False}
        if parts[3] == "contents":
            f = "/".join(parts[4:])
            if (full, f) in self.files:
                return {"encoding": "base64", "content": base64.b64encode(self.files[(full, f)].encode()).decode()}
            return None
        raise AssertionError(path)

    def is_personal_token(self):
        return False


def listed():
    return {r["repo"] for r in corpus.load_corpus()}


def main():
    tmp = tempfile.mkdtemp()
    try:
        corpus.CORPUS = os.path.join(tmp, "corpus.csv")
        corpus.BLOCKLIST = os.path.join(tmp, "blocklist.csv")
        corpus.SEEN = os.path.join(tmp, ".cache", "seen.json")
        gh = FakeGitHub()
        gh.add("teamA/push-back")
        gh.add("teamA/new-season", pushed="2026-10-12T00:00:00Z")            # found through its owner
        gh.add("student/push-back", fork_of="teamA/push-back")               # found as a fork
        gh.add("teamB/ez-robot")                                             # found by search
        gh.add("teamD/ez-file", tree=EZ + [".ez-corpus-optout"])             # opted out with the file
        gh.add("teamF/ez-lemlib", tree=["include/lemlib/api.hpp", "project.pros"])  # not EZ-Template
        gh.add("teamG/ez-private", private=True)                             # never seen
        gh.add("teamA/website", tree=["index.html"], language="HTML")         # skipped by language
        gh.add("EZ-Robotics/EZ-Template")
        gh.add("someone/EZ-Template", fork_of="EZ-Robotics/EZ-Template")
        corpus.save_corpus([{"repo": "teamA/push-back", "url": ""}, {"repo": "EZ-Robotics/EZ-Template", "url": ""}])

        c = corpus.update(gh, today="2026-10-15")
        assert listed() == {"teamA/push-back", "teamA/new-season", "student/push-back", "teamB/ez-robot",
                            "EZ-Robotics/EZ-Template", "someone/EZ-Template"}, listed()
        assert c["opted_out"] == 1, c
        rows = {r["repo"]: r for r in corpus.load_corpus()}
        assert rows["student/push-back"]["fork_of"] == "teamA/push-back"
        assert rows["someone/EZ-Template"]["listed_as"] == "library-fork"
        assert rows["EZ-Robotics/EZ-Template"]["listed_as"] == "maintainer"
        assert rows["teamB/ez-robot"]["ez_template_versions"] == "3.2.2"

        # Opting out sticks even after the file is deleted later.
        gh.trees["teamD/ez-file"] = EZ
        gh.repos["teamD/ez-file"]["pushed_at"] = "2026-10-16T00:00:00Z"
        corpus.update(gh, today="2026-10-16")
        assert "teamD/ez-file" not in listed()

        # Second run uses the cache, so unchanged repos cost one lookup each.
        before = gh.calls
        corpus.update(gh, today="2026-10-17")
        assert gh.calls - before < 40, gh.calls - before

        # Remove by pasted link: the project and its fork come off and stay off.
        corpus.remove(gh, "https://github.com/teamA/push-back.git/")
        assert "teamA/push-back" not in listed() and "student/push-back" not in listed()
        corpus.update(gh, today="2026-10-18")
        assert "teamA/push-back" not in listed() and "student/push-back" not in listed()
        text = open(corpus.BLOCKLIST).read()
        assert "teamA" not in text and "push-back" not in text

        # A renamed repo keeps its ID, so it stays off.
        r = gh.repos.pop("teamA/push-back")
        r["full_name"] = "teamA/push-back-2"
        gh.repos["teamA/push-back-2"], gh.trees["teamA/push-back-2"] = r, EZ
        corpus.update(gh, today="2026-10-19")
        assert "teamA/push-back-2" not in listed()

        # Removing an account takes off everything it owns, including later repos.
        corpus.remove(gh, "teamB")
        gh.add("teamB/ez-next-season", pushed="2026-10-20T00:00:00Z")
        corpus.update(gh, today="2026-10-21")
        assert not any(n.startswith("teamB/") for n in listed()), listed()

        # Removing a repo while it's private still holds once it goes public.
        gh.add("teamH/ez-secret", private=True)
        corpus.remove(gh, "teamH/ez-secret")
        gh.repos["teamH/ez-secret"]["private"] = False
        corpus.update(gh, today="2026-10-22")
        assert "teamH/ez-secret" not in listed()

        # Unremove puts it back at the next update.
        corpus.unremove(gh, "teamH/ez-secret")
        corpus.update(gh, today="2026-10-23")
        assert "teamH/ez-secret" in listed(), listed()

        # Going private takes it off.
        gh.repos["teamA/new-season"]["private"] = True
        corpus.update(gh, today="2026-10-24")
        assert "teamA/new-season" not in listed()

        # A dropped connection is retried instead of crashing the run.
        real_urlopen, real_sleep, drops = urllib.request.urlopen, corpus.time.sleep, []

        def flaky_urlopen(req, timeout=None):
            if not drops:
                drops.append(1)
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            return io.BytesIO(b'{"ok": true}')

        urllib.request.urlopen, corpus.time.sleep = flaky_urlopen, lambda s: None
        try:
            assert corpus.GitHub().get("/x") == {"ok": True}
        finally:
            urllib.request.urlopen, corpus.time.sleep = real_urlopen, real_sleep
        assert drops

        assert corpus.normalize("github.com/a/b/tree/main") == "a/b"
        assert corpus.pros_version(pros("2.1.2")) == "2.1.2"
        assert corpus.pros_version("[1, 2]") is None
        print("all tests passed")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
