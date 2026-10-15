#!/usr/bin/env python3
"""Keeps the EZ-Template-Corpus list up to date.

The list only holds links. This script never downloads or keeps anyone's code.
It looks at a repo's details, the names of its files, and its project.pros.

  python3 scripts/corpus.py update
      What the scheduled Action runs. Re-checks everything on the list, looks
      for new projects, and rewrites corpus.csv.

  python3 scripts/corpus.py remove owner/repo     (or just owner)
      Takes a project, or everything an account owns, off the list for good.
      The "Remove a project" Action runs this for you.

  python3 scripts/corpus.py unremove owner/repo   (or just owner)
      Undoes a removal, when the owner asks to come back.

The script won't run with a personal token, because it can see private repos.
It only ever prints totals, never names, since Action logs are public.
"""
import base64
import csv
import datetime
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "corpus.csv")
BLOCKLIST = os.path.join(ROOT, "blocklist.csv")
SEEN = os.path.join(ROOT, ".cache", "seen.json")  # Action cache only, never committed

API = "https://api.github.com"
OPTOUT_FILE = ".ez-corpus-optout"
MAINTAINERS = {"ssejrog", "ez-robotics"}
LIBRARY = "ez-robotics/ez-template"
SEARCHES = [
    '"EZ-Template" in:name,description,readme',
    '"EZ-Template" in:name,description,readme fork:only',
    "ez-template in:name",
    "eztemplate in:name,description,readme",
]
CODE_LANGUAGES = {None, "C++", "C", "Makefile"}
SAVE_EVERY = 50

HEADER_RE = re.compile(r"(^|/)EZ-Template/(api|PID|util|auton_selector|drive/drive)\.hpp$")
PROS_RE = re.compile(r"(^|/)project\.pros$")
FIELDS = ["repo", "url", "ez_template_versions", "fork_of", "listed_as", "last_commit", "checked"]


def q(s):
    return urllib.parse.quote(s, safe="")


def normalize(target):
    """https://github.com/Owner/Repo.git/ becomes Owner/Repo. A bare owner works too."""
    t = re.sub(r"^(https?://)?(www\.)?github\.com/", "", target.strip())
    t = t.split("?")[0].split("#")[0].strip("/")
    t = t[:-4] if t.endswith(".git") else t
    return "/".join([p for p in t.split("/") if p][:2])


# ---------------------------------------------------------------- GitHub
class GitHub:
    def __init__(self, token=None):
        self.token = token
        self.calls = 0

    def get(self, path, search=False):
        """The JSON, or None if GitHub won't show it publicly."""
        for attempt in range(6):
            req = urllib.request.Request(API + path, headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "EZ-Template-Corpus",
            })
            if self.token:
                req.add_header("Authorization", "Bearer " + self.token)
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                if search:
                    time.sleep(2.2)  # search allows 30 a minute
                return data
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    if e.headers.get("retry-after"):
                        time.sleep(min(int(e.headers["retry-after"]) + 1, 900))
                        continue
                    if e.headers.get("x-ratelimit-remaining") == "0":
                        reset = int(e.headers.get("x-ratelimit-reset") or time.time() + 60)
                        time.sleep(max(5, reset - int(time.time()) + 2))
                        continue
                    return None  # blocked for some other reason, like a disabled repo
                if e.code >= 500:
                    time.sleep(10 * (attempt + 1))
                    continue
                return None
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, ValueError):
                time.sleep(10 * (attempt + 1))
        raise RuntimeError("GitHub kept failing")

    def is_personal_token(self):
        """A personal token can read /user. The Action's own token can't."""
        if not self.token:
            return False
        req = urllib.request.Request(API + "/user", headers={
            "Authorization": "Bearer " + self.token, "User-Agent": "EZ-Template-Corpus"})
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            return False


# ---------------------------------------------------------------- blocklist
def id_hash(kind, value):
    """Blocklist entries are hashes so the file doesn't spell out names."""
    if kind == "name":
        value = str(value).lower()
    return hashlib.sha256(f"ez-corpus:{kind}:{value}".encode()).hexdigest()


def load_blocklist():
    if not os.path.exists(BLOCKLIST):
        return set()
    with open(BLOCKLIST, newline="") as fh:
        return {r["hash"] for r in csv.DictReader(fh) if r.get("hash")}


def save_blocklist(hashes):
    with open(BLOCKLIST, "w", newline="") as fh:
        fh.write("hash\n")
        for h in sorted(hashes):
            fh.write(h + "\n")


def family(repo):
    """The repo plus whatever it was forked from. A fork of a removed project is removed too."""
    return [repo] + [r for r in (repo.get("parent"), repo.get("source")) if r]


def is_blocked(repo, blocked):
    for r in family(repo):
        for k, v in (("repo", r["id"]), ("owner", r["owner"]["id"]), ("name", r["full_name"])):
            if id_hash(k, v) in blocked:
                return True
    return False


def block_repo(repo, blocked):
    blocked.update({id_hash("repo", repo["id"]), id_hash("name", repo["full_name"])})


# ---------------------------------------------------------------- checking one repo
def pros_version(text):
    """The EZ-Template version in a project.pros, or None if EZ-Template isn't in it."""
    try:
        d = json.loads(text)
    except ValueError:
        return "" if "EZ-Template" in text else None
    if not isinstance(d, dict):
        return None
    d = d.get("py/state", d)
    t = (d.get("templates") or {}).get("EZ-Template") if isinstance(d, dict) else None
    if t is None:
        return None
    t = t.get("py/state", t) if isinstance(t, dict) else {}
    return str(t.get("version", "")) if isinstance(t, dict) else ""


def look_inside(gh, repo):
    """Returns (uses_ez, versions, opted_out) from file names and project.pros."""
    name, branch = repo["full_name"], repo.get("default_branch") or "HEAD"
    tree = gh.get(f"/repos/{name}/git/trees/{q(branch)}?recursive=1") or {}
    paths = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]
    if OPTOUT_FILE in paths or (tree.get("truncated") and gh.get(f"/repos/{name}/contents/{OPTOUT_FILE}")):
        return False, [], True
    has_headers = any(HEADER_RE.search(p) for p in paths)
    pros = sorted((p for p in paths if PROS_RE.search(p)), key=lambda p: (p.count("/"), p))
    if tree.get("truncated") and "project.pros" not in pros:
        pros.insert(0, "project.pros")
    versions = set()
    for p in pros[:2]:
        c = gh.get(f"/repos/{name}/contents/{urllib.parse.quote(p)}?ref={q(branch)}")
        if c and c.get("encoding") == "base64":
            v = pros_version(base64.b64decode(c["content"]).decode("utf-8", "replace"))
            if v is not None:
                versions.add(v)
        if versions and has_headers:
            break
    return has_headers or bool(versions), sorted(v for v in versions if v), False


def row_for(repo, versions, today):
    up = (repo.get("parent") or {}) if repo.get("fork") else {}
    root = ((repo.get("source") or up).get("full_name") or "").lower()
    listed_as = ("library-fork" if root == LIBRARY else
                 "maintainer" if repo["owner"]["login"].lower() in MAINTAINERS else "team")
    return {"repo": repo["full_name"], "url": repo["html_url"], "ez_template_versions": " ".join(versions),
            "fork_of": up.get("full_name", ""), "listed_as": listed_as,
            "last_commit": (repo.get("pushed_at") or "")[:10], "checked": today}


def judge(gh, repo, blocked, seen, today):
    """Returns ('listed', row), or a reason it isn't listed."""
    if repo.get("visibility", "private" if repo.get("private") else "public") != "public":
        return "gone", None
    if is_blocked(repo, blocked):
        return "blocked", None
    key, pushed = str(repo["id"]), repo.get("pushed_at")
    s = seen.get(key)
    if s and s.get("pushed") == pushed:  # nothing changed since we last looked
        if not s.get("ez"):
            return "no_ez", None
        return "listed", row_for(repo, s.get("versions", []), today)
    uses, versions, opted = look_inside(gh, repo)
    if opted:
        block_repo(repo, blocked)
        return "opted_out", None
    seen[key] = {"pushed": pushed, "ez": uses, "versions": versions}
    return ("listed", row_for(repo, versions, today)) if uses else ("no_ez", None)


# ---------------------------------------------------------------- files
def load_corpus():
    if not os.path.exists(CORPUS):
        return []
    with open(CORPUS, newline="") as fh:
        return list(csv.DictReader(fh))


def save_corpus(rows):
    out, seen = [], set()
    for r in sorted(rows, key=lambda r: r["repo"].lower()):
        if r["repo"].lower() not in seen:
            seen.add(r["repo"].lower())
            out.append(r)
    with open(CORPUS, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in out:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def load_seen():
    try:
        with open(SEEN) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_seen(seen):
    os.makedirs(os.path.dirname(SEEN), exist_ok=True)
    with open(SEEN, "w") as fh:
        json.dump(seen, fh)


# ---------------------------------------------------------------- the scheduled update
def find_new(gh, listed, since):
    """Names of repos worth checking. Searches, listed teams' other repos, and forks."""
    found = []
    for query in SEARCHES:
        for page in range(1, 11):
            r = gh.get(f"/search/repositories?q={q(query + ' pushed:>=' + since)}&per_page=100&page={page}",
                       search=True)
            items = (r or {}).get("items", [])
            found += [i["full_name"] for i in items]
            if len(items) < 100:
                break
    for owner in sorted({r["repo"].split("/")[0] for r in listed}):
        repos = gh.get(f"/users/{q(owner)}/repos?type=owner&sort=pushed&per_page=50") or []
        found += [r["full_name"] for r in repos
                  if (r.get("pushed_at") or "") >= since and r.get("language") in CODE_LANGUAGES]
    return found


def update(gh, today=None):
    today = today or datetime.date.today().isoformat()
    blocked, seen, old = load_blocklist(), load_seen(), load_corpus()
    since = seen.get("_last_run") or (datetime.date.fromisoformat(today) - datetime.timedelta(days=40)).isoformat()
    counts = dict(still_listed=0, added=0, gone=0, blocked=0, opted_out=0, no_ez=0)
    kept, done_ids = [], set()

    def consider(name, is_new):
        """Checks one repo. Returns the repo's details if it ended up listed."""
        repo = gh.get(f"/repos/{normalize(name)}")
        if repo is None:
            counts["gone"] += 0 if is_new else 1
            return None
        if repo["id"] in done_ids:
            return None
        done_ids.add(repo["id"])
        result, row = judge(gh, repo, blocked, seen, today)
        if result == "listed":
            kept.append(row)
            counts["added" if is_new else "still_listed"] += 1
            return repo
        if not is_new or result != "no_ez":
            counts[result] += 1
        return None

    old_names = {r["repo"].lower() for r in old}
    with_forks = []
    for i, r in enumerate(old):
        if i and i % SAVE_EVERY == 0:
            save_corpus(kept + old[i:]); save_blocklist(blocked); save_seen(seen)
        repo = consider(r["repo"], False)
        if repo and repo.get("forks_count"):
            with_forks.append(repo["full_name"])

    candidates = find_new(gh, kept, since)
    for name in with_forks:
        candidates += [f["full_name"] for f in gh.get(f"/repos/{name}/forks?sort=newest&per_page=100") or []]
    for i, name in enumerate(dict.fromkeys(candidates)):
        if name.lower() in old_names:
            continue
        if i and i % SAVE_EVERY == 0:
            save_corpus(kept); save_blocklist(blocked); save_seen(seen)
        consider(name, True)

    # A fork whose original was removed during this run comes off too.
    kept = [k for k in kept if not k["fork_of"] or id_hash("name", k["fork_of"]) not in blocked]
    seen["_last_run"] = (datetime.date.fromisoformat(today) - datetime.timedelta(days=7)).isoformat()
    save_corpus(kept)
    save_blocklist(blocked)
    save_seen(seen)
    return counts


# ---------------------------------------------------------------- remove and unremove
def keys_for(gh, target):
    target = normalize(target)
    if "/" in target:
        keys = {id_hash("name", target)}
        repo = gh.get(f"/repos/{target}")
        if repo:
            keys |= {id_hash("repo", repo["id"]), id_hash("name", repo["full_name"])}
        return target, keys
    account = gh.get(f"/users/{q(target)}")
    if account is None:
        sys.exit("Couldn't find that account. Check the spelling.")
    return target, {id_hash("owner", account["id"])}


def remove(gh, target):
    target, keys = keys_for(gh, target)
    save_blocklist(load_blocklist() | keys)
    t = target.lower()

    def matches(r):
        names = [r["repo"].lower(), (r.get("fork_of") or "").lower()]
        return any(n == t or ("/" not in t and n.split("/")[0] == t) for n in names if n)

    rows = load_corpus()
    kept = [r for r in rows if not matches(r)]
    save_corpus(kept)
    return len(rows) - len(kept)


def unremove(gh, target):
    target, keys = keys_for(gh, target)
    before = load_blocklist()
    save_blocklist(before - keys)
    if "/" in target:  # put it back on the list so the next update checks it
        save_corpus(load_corpus() + [{"repo": target, "url": "https://github.com/" + target}])
    return len(before & keys)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd not in ("update", "remove", "unremove") or (cmd != "update" and len(argv) < 3):
        print(__doc__)
        return 2
    gh = GitHub(os.environ.get("GITHUB_TOKEN"))
    if gh.is_personal_token():
        print("This won't run with a personal token, because it can see private repos.\n"
              "Unset GITHUB_TOKEN and run it again.")
        return 1
    if cmd == "update":
        c = update(gh)
        summary = (f"{c['still_listed']} still listed and {c['added']} added. {c['gone']} went private or were deleted, "
                   f"{c['blocked']} were removed on request, {c['opted_out']} opted out, and {c['no_ez']} "
                   "stopped using EZ-Template.")
        print(summary)
        if os.environ.get("CHECK_SUMMARY"):
            with open(os.environ["CHECK_SUMMARY"], "w") as fh:
                fh.write(summary + "\n")
    elif cmd == "remove":
        n = remove(gh, argv[2])
        print(f"Done. {n} row{'s' if n != 1 else ''} taken off. Commit corpus.csv and blocklist.csv, "
              "and keep the name out of the commit message.")
    else:
        n = unremove(gh, argv[2])
        print(f"Done. {n} blocklist entr{'y' if n == 1 else 'ies'} taken out. It'll stay listed after the next update "
              "if it's public and uses EZ-Template.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
