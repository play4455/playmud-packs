"""Draft one-shot public NAS/PC -> native Git -> GitHub raw publication.

No checkout, unzip, third-party action, long-lived credential, credential file,
force push, server operation, or arbitrary source URL. The command-line entry
point only runs inside the reviewed manual GitHub Actions context. Tests call
the same core with synthetic data, an injected loopback opener and local Git.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterable
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

REPOSITORY = "play4455/playmud-packs"
BRANCH = "main"
TARGET_ROOT = "existing-20261111"
TARGET_GROUPS = {TARGET_ROOT, "image-bundle-20261111"}
REMOTE = "https://github.com/" + REPOSITORY + ".git"
ORIGINS = {
    "nas": "https://playmud.synology.me/playmud-packs",
    "pc": "https://packs.playmud.duckdns.org:9872/releases",
}
CONFIRMATION = "APPROVE_ONCE_REPO_CONTENTS_WRITE"
MAX_FILE = 100 * 1024 * 1024
MAX_TOTAL = 1_500_000_000
BLOCK = 65536
READONLY_PLACEHOLDERS = {
    "existing-20261111/README.md": (1, "01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b"),
    "image-bundle-20261111/README.md": (1, "01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b"),
}


@dataclass(frozen=True)
class ContentIdentity:
    bytes: int
    sha256: str


class Refused(Exception):
    def __init__(self, code, details=None):
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "UNSAFE_DIAGNOSTIC_REFUSED"
        self.code = code
        self.details = details
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise Refused(code)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def unique_json_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, "DUPLICATE_JSON_KEY")
        obj[key] = value
    return obj


@dataclass(frozen=True)
class Entry:
    name: str
    sha256: str
    bytes: int
    edition: str
    category: str
    uuid: str
    version: str
    source_releases: dict[str, str]
    target_group: str

    @property
    def target(self):
        return self.target_group + "/" + self.name

    def source(self, role):
        require(role in ORIGINS, "UNAPPROVED_ORIGIN")
        return ORIGINS[role] + "/" + self.source_releases[role] + "/" + self.name

    def raw(self, commit):
        require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "INVALID_COMMIT")
        return "https://raw.githubusercontent.com/" + REPOSITORY + "/" + commit + "/" + self.target


@dataclass(frozen=True)
class Catalog:
    entries: tuple[Entry, ...]
    sha256: str
    total_bytes: int


def load_catalog(path, expected_sha256, expected_count, expected_bytes):
    require(bool(re.fullmatch(r"[0-9a-f]{64}", expected_sha256)), "UNPINNED_MANIFEST")
    require(type(expected_count) is int and 1 <= expected_count <= 128, "BAD_EXPECTED_COUNT")
    require(type(expected_bytes) is int and 0 < expected_bytes < MAX_TOTAL, "BAD_EXPECTED_TOTAL")
    require(stat.S_ISREG(path.lstat().st_mode) and path.stat().st_size <= 262144, "INVALID_MANIFEST_FILE")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, "MANIFEST_SHA_MISMATCH")
    try:
        obj = json.loads(raw, object_pairs_hook=unique_json_object)
        require(set(obj) == {"schema", "repository", "branch", "files"}, "MANIFEST_FIELDS")
        require(obj["schema"] == 1 and type(obj["schema"]) is int, "MANIFEST_SCHEMA")
        require(obj["repository"] == REPOSITORY and obj["branch"] == BRANCH, "MANIFEST_DESTINATION")
        require(type(obj["files"]) is list and len(obj["files"]) == expected_count, "MANIFEST_COUNT")
        rows, names, ids = [], set(), set()
        fields = set(Entry.__dataclass_fields__)
        for item in obj["files"]:
            require(type(item) is dict and set(item) == fields, "ENTRY_FIELDS")
            require(all(type(item[k]) is str for k in fields - {"bytes", "source_releases"}), "ENTRY_TYPES")
            require(type(item["bytes"]) is int and 0 < item["bytes"] < MAX_FILE, "ENTRY_SIZE")
            name, digest = item["name"], item["sha256"]
            require(bool(re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,180}-[0-9a-f]{16}\.zip", name)), "ENTRY_FILENAME")
            require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)) and name.endswith("-" + digest[:16] + ".zip"), "ENTRY_HASH_NAME")
            require(item["edition"] in ("java", "bedrock") and item["category"] in ("music", "frames", "integrated"), "ENTRY_CLASS")
            require(type(item["source_releases"]) is dict and set(item["source_releases"]) == set(ORIGINS), "ENTRY_SOURCE_ROLES")
            require(all(type(value) is str and bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value))
                        and ".." not in value for value in item["source_releases"].values()), "ENTRY_SOURCE_RELEASE")
            require(item["target_group"] in TARGET_GROUPS, "ENTRY_TARGET_GROUP")
            require(item["target_group"] != "image-bundle-20261111" or item["category"] == "frames", "ENTRY_IMAGE_GROUP_CLASS")
            require(str(uuid.UUID(item["uuid"])) == item["uuid"], "ENTRY_UUID")
            require(item["version"] == "" and item["edition"] == "java" or bool(re.fullmatch(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}", item["version"])), "ENTRY_VERSION")
            require(name.casefold() not in names and item["uuid"] not in ids, "DUPLICATE_ENTRY")
            names.add(name.casefold())
            ids.add(item["uuid"])
            rows.append(Entry(**item))
        total = sum(row.bytes for row in rows)
        require(total == expected_bytes, "MANIFEST_TOTAL")
        return Catalog(tuple(rows), expected_sha256, total)
    except Refused:
        raise
    except Exception:
        raise Refused("MALFORMED_MANIFEST") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("HTTP_REDIRECT_REFUSED")


class PublicNetwork:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def repository_check(self):
        request = urllib.request.Request("https://api.github.com/repos/" + REPOSITORY, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "PlayMud-one-shot-publisher",
            "X-GitHub-Api-Version": "2026-03-10"})
        try:
            with self.opener.open(request, timeout=15) as response:
                require(response.status == 200, "REPOSITORY_HTTP")
                raw = response.read(131073)
                require(len(raw) <= 131072, "REPOSITORY_RESPONSE_TOO_LARGE")
                obj = json.loads(raw)
            require(obj.get("full_name", "").casefold() == REPOSITORY and obj.get("private") is False,
                    "PUBLIC_EXPECTED_REPOSITORY_REQUIRED")
            require(obj.get("default_branch") == BRANCH and not obj.get("archived") and not obj.get("disabled"), "REPOSITORY_NOT_WRITABLE_PLAN")
        except Refused:
            raise
        except Exception:
            raise Refused("PUBLIC_REPOSITORY_CHECK_FAILED") from None

    def stream(self, address, entry, output=None):
        # Only URLs produced from fixed roots and reviewed fields reach this method.
        uri = urllib.parse.urlsplit(address)
        require(uri.scheme == "https" and uri.username is None and uri.password is None
                and not uri.query and not uri.fragment and "%" not in uri.path, "UNSAFE_SOURCE_URL")
        source_allowed = any(address == entry.source(role) for role in ORIGINS)
        raw_prefix = "https://raw.githubusercontent.com/" + REPOSITORY + "/"
        raw_allowed = address.startswith(raw_prefix) and bool(re.fullmatch(
            re.escape(raw_prefix) + r"[0-9a-f]{40}/" + re.escape(entry.target), address))
        require(source_allowed or raw_allowed, "UNAPPROVED_URL")
        request = urllib.request.Request(address, headers={"Accept-Encoding": "identity", "User-Agent": "PlayMud-one-shot-publisher"})
        try:
            with self.opener.open(request, timeout=30) as response:
                require(response.status == 200, "PACK_HTTP_STATUS")
                require(response.headers.get_content_type().lower() == "application/zip", "PACK_MIME")
                require(int(response.headers.get("Content-Length", "-1")) == entry.bytes, "PACK_LENGTH")
                require(response.headers.get("Content-Encoding", "identity").lower() == "identity", "PACK_ENCODING")
                digest, count, begin = hashlib.sha256(), 0, time.monotonic()
                while block := response.read(BLOCK):
                    require(time.monotonic() - begin < 180, "PACK_TRANSFER_DEADLINE")
                    count += len(block)
                    require(count <= entry.bytes, "PACK_OVERSIZE")
                    digest.update(block)
                    if output is not None:
                        output.write(block)
                require(count == entry.bytes and digest.hexdigest() == entry.sha256, "PACK_SHA_OR_SIZE")
                return count
        except Refused:
            raise
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise Refused("PACK_HTTP_STATUS_" + str(int(code))) from None
        except Exception:
            raise Refused("PACK_NETWORK_OR_READ_FAILURE") from None

    def download(self, entry, directory, preferred="nas"):
        require(preferred in ORIGINS, "UNAPPROVED_ORIGIN")
        final = directory / entry.name
        require(not final.exists(), "SCRATCH_TARGET_EXISTS")
        errors = []
        for role in (preferred, "pc" if preferred == "nas" else "nas"):
            partial = directory / (entry.name + "." + role + ".partial")
            try:
                with partial.open("xb") as stream:
                    self.stream(entry.source(role), entry, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                require(zipfile.is_zipfile(partial), "PACK_ZIP_STRUCTURE")
                partial.replace(final)
                return final, {"name": entry.name, "origin": role, "bytes": entry.bytes, "sha256": entry.sha256, "failed_attempts": errors}
            except Refused as error:
                errors.append({"origin": role, "code": error.code})
                if partial.exists():
                    partial.unlink()
        raise Refused("BOTH_APPROVED_ORIGINS_FAILED", {"file": entry.name, "attempts": errors})


def git_environment():
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("GIT_") and not k.upper().endswith(("TOKEN", "SECRET"))
           and k not in ("GH_TOKEN", "GCM_INTERACTIVE")
           and k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_COUNT="0", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never",
               GIT_ALLOW_PROTOCOL="https:file")
    return env


class GitStore:
    def __init__(self, directory, remote=REMOTE, *, local_fixture=False):
        require(local_fixture or remote == REMOTE, "UNAPPROVED_GIT_REMOTE")
        self.directory, self.remote, self.local_fixture = directory, remote, local_fixture
        self.empty_hooks = directory.parent / "disabled-hooks"
        self.empty_hooks.mkdir(exist_ok=True)
        self.env = git_environment()
        init = subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "--bare",
                               "--template=" + str(self.empty_hooks), str(directory)],
                              env=self.env, capture_output=True, timeout=30)
        require(init.returncode == 0, "GIT_INIT_FAILED")

    def command(self, args):
        return ["git", "-c", "core.hooksPath=" + str(self.empty_hooks), "-c", "commit.gpgsign=false",
                "-c", "http.sslVerify=true", "-c", "http.proxy=", "-c", "credential.helper=",
                "--git-dir=" + str(self.directory), *args]

    def run(self, args, *, input=None, stdin=None, extra_env=None, timeout=300):
        env = dict(self.env)
        env.update(extra_env or {})
        try:
            proc = subprocess.run(self.command(args), input=input, stdin=stdin, capture_output=True, env=env, timeout=timeout)
            require(proc.returncode == 0, "GIT_" + re.sub(r"[^A-Z0-9]", "_", args[0].upper()) + "_FAILED")
            require(len(proc.stdout) <= 4_000_000, "GIT_METADATA_TOO_LARGE")
            return proc.stdout
        except Refused:
            raise
        except Exception:
            raise Refused("GIT_OPERATION_OUTCOME_UNKNOWN") from None

    def remote_head(self):
        out = self.run(["ls-remote", "--refs", self.remote, "refs/heads/" + BRANCH]).decode("ascii").strip()
        require(bool(re.fullmatch(r"[0-9a-f]{40}\s+refs/heads/main", out)), "EXPECTED_BRANCH_REQUIRED")
        return out.split()[0]

    def fetch(self, expected):
        require(bool(re.fullmatch(r"[0-9a-f]{40}", expected)), "INVALID_BASE_COMMIT")
        self.run(["fetch", "--no-tags", "--depth=1", self.remote, "refs/heads/" + BRANCH])
        base = self.run(["rev-parse", "FETCH_HEAD"]).decode().strip()
        require(base == expected, "BRANCH_MOVED_REVIEW_NEW_BASE")
        return base

    def paths(self, commit):
        result = {}
        for row in self.run(["ls-tree", "-r", "-z", commit]).split(b"\0"):
            if not row:
                continue
            meta, path = row.split(b"\t", 1)
            mode, kind, oid = meta.decode("ascii").split()
            # Unrelated non-UTF8 paths remain opaque and unmodified.
            result[path] = (mode, kind, oid)
        return result

    def verify_blob(self, oid, entry):
        require(int(self.run(["cat-file", "-s", oid]).strip()) == entry.bytes, "EXISTING_TARGET_SIZE_CONFLICT")
        proc = subprocess.Popen(self.command(["cat-file", "blob", oid]), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env)
        try:
            digest, count = hashlib.sha256(), 0
            while block := proc.stdout.read(BLOCK):
                count += len(block)
                require(count <= entry.bytes, "EXISTING_TARGET_SIZE_CONFLICT")
                digest.update(block)
            require(proc.wait(timeout=10) == 0, "GIT_BLOB_READ_FAILED")
            require(count == entry.bytes and digest.hexdigest() == entry.sha256, "EXISTING_TARGET_HASH_CONFLICT")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def existing(self, base, catalog):
        paths, missing = self.paths(base), []
        roots = {row.target_group for row in catalog.entries}
        require(not any(root.encode() in paths for root in roots), "DESTINATION_ROOT_NOT_DIRECTORY")
        approved = {entry.target.encode() for entry in catalog.entries} | {name.encode() for name in READONLY_PLACEHOLDERS}
        require(not any(any(path.startswith((root + "/").encode()) for root in roots)
                        and path not in approved for path in paths), "UNREVIEWED_DESTINATION_ENTRY")
        # Exactly the two coordinator-approved newline placeholders. They are
        # verified as bytes, never deleted, rewritten, or accepted by filename alone.
        for name, (size, digest) in READONLY_PLACEHOLDERS.items():
            if name.encode() in paths:
                mode, kind, oid = paths[name.encode()]
                require(mode == "100644" and kind == "blob", "PLACEHOLDER_MODE_CONFLICT")
                self.verify_blob(oid, ContentIdentity(size, digest))
        for entry in catalog.entries:
            known = paths.get(entry.target.encode())
            if known is None:
                missing.append(entry)
                continue
            mode, kind, oid = known
            require(mode == "100644" and kind == "blob", "EXISTING_TARGET_MODE_CONFLICT")
            self.verify_blob(oid, entry)
        return missing

    def candidate(self, base, catalog, downloaded):
        missing = self.existing(base, catalog)
        require(set(downloaded) == {row.name for row in missing}, "DOWNLOADED_SET_MISMATCH")
        if not missing:
            return base
        index = self.directory.parent / "import.index"
        require(not index.exists(), "PRIVATE_INDEX_ALREADY_EXISTS")
        env = {"GIT_INDEX_FILE": str(index)}
        self.run(["read-tree", base], extra_env=env)
        for row in missing:
            path = downloaded[row.name]
            require(stat.S_ISREG(path.lstat().st_mode) and path.stat().st_size == row.bytes
                    and sha256_file(path) == row.sha256, "DOWNLOADED_FILE_CHANGED")
            with path.open("rb") as source:
                oid = self.run(["hash-object", "-w", "--no-filters", "--stdin"], stdin=source).decode().strip()
            require(bool(re.fullmatch(r"[0-9a-f]{40}", oid)), "GIT_BLOB_ID_INVALID")
            self.run(["update-index", "--add", "--cacheinfo", "100644", oid, row.target], extra_env=env)
        tree = self.run(["write-tree"], extra_env=env).decode().strip()
        author = {"GIT_AUTHOR_NAME": "github-actions[bot]", "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
                  "GIT_COMMITTER_NAME": "github-actions[bot]", "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com"}
        commit = self.run(["commit-tree", tree, "-p", base], input=b"Import exact approved public resource pack bytes\n", extra_env=author).decode().strip()
        require(self.run(["rev-parse", commit + "^1"]).decode().strip() == base, "CANDIDATE_PARENT_MISMATCH")
        changed = {p for p in self.run(["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", base, commit]).split(b"\0") if p}
        require(changed == {row.target.encode() for row in missing}, "CANDIDATE_CHANGED_UNAPPROVED_PATHS")
        require(not self.existing(commit, catalog), "CANDIDATE_INCOMPLETE")
        return commit

    def push(self, base, candidate):
        require(self.remote_head() == base, "BRANCH_MOVED_BEFORE_PUSH")
        if candidate == base:
            return {"status": "NOOP_ALL_APPROVED_BYTES_ALREADY_PRESENT", "commit": base}
        require(self.run(["rev-parse", candidate + "^1"]).decode().strip() == base, "NOT_A_SINGLE_FAST_FORWARD")
        extra = {}
        if not self.local_fixture:
            # Supplied by GitHub for this job only, never a human PAT. Not read
            # until after verification. No URL/argv/config-file credential.
            token = os.environ.get("GITHUB_TOKEN", "")
            require(bool(token) and "\n" not in token and "\r" not in token, "JOB_TOKEN_REQUIRED")
            header = "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
            extra = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                     "GIT_CONFIG_VALUE_0": header}
        try:
            self.run(["push", "--atomic", self.remote, candidate + ":refs/heads/" + BRANCH], extra_env=extra)
        except Refused:
            # A lost acknowledgement is not permission to reset or force push.
            if self.remote_head() != candidate:
                raise Refused("PUSH_FAILED_OR_OUTCOME_UNKNOWN_REVIEW") from None
        finally:
            extra.clear()
        require(self.remote_head() == candidate, "BRANCH_CHANGED_AFTER_PUSH_REVIEW")
        return {"status": "PUSHED_SINGLE_FAST_FORWARD", "commit": candidate}


def prepare_sources(catalog, network, directory, entries=None, preferred="nas"):
    directory.mkdir(mode=0o700)
    paths, receipts = {}, []
    for entry in catalog.entries if entries is None else entries:
        path, receipt = network.download(entry, directory, preferred)
        paths[entry.name] = path
        receipts.append(receipt)
    return paths, receipts


def output(name, value):
    require(name in ("published_commit", "manifest_sha256") and bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)), "UNSAFE_JOB_OUTPUT")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(name + "=" + value + "\n")


def main():
    require(len(sys.argv) == 3 and sys.argv[1] in ("verify", "publish", "verify-raw"), "USAGE_MODE_AND_MANIFEST")
    mode, manifest_path = sys.argv[1], Path(sys.argv[2])
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "MANUAL_GITHUB_ACTIONS_ONLY")
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY and os.environ.get("GITHUB_REF") == "refs/heads/" + BRANCH, "EXPECTED_REPOSITORY_BRANCH_ONLY")
    require(os.environ.get("IMPORT_CONFIRMATION") == CONFIRMATION, "EXPLICIT_WRITE_CONSENT_REQUIRED")
    base = os.environ.get("IMPORT_BASE_SHA", "")
    require(base == os.environ.get("GITHUB_SHA") and bool(re.fullmatch(r"[0-9a-f]{40}", base)), "PINNED_DISPATCH_COMMIT_REQUIRED")
    require(sha256_file(Path(__file__)) == os.environ.get("IMPORT_SCRIPT_SHA256"), "SCRIPT_PIN_MISMATCH")
    catalog = load_catalog(manifest_path, os.environ.get("IMPORT_MANIFEST_SHA256", ""),
                           int(os.environ.get("IMPORT_EXPECTED_COUNT", "0")), int(os.environ.get("IMPORT_EXPECTED_BYTES", "0")))
    network = PublicNetwork()
    network.repository_check()
    if mode == "verify-raw":
        commit = os.environ.get("IMPORT_PUBLISHED_COMMIT", "")
        total = sum(network.stream(entry.raw(commit), entry) for entry in catalog.entries)
        print(json.dumps({"status": "RAW_ALL_APPROVED_FILES_FULL_SHA_VERIFIED", "commit": commit,
                          "files": len(catalog.entries), "bytes": total, "mime": "application/zip"}))
        return
    with tempfile.TemporaryDirectory(prefix="playmud-import-", dir=os.environ.get("RUNNER_TEMP")) as temp:
        workspace = Path(temp)
        git = GitStore(workspace / "repository.git")
        git.fetch(base)
        missing = git.existing(base, catalog)
        if mode == "verify":
            _, receipts = prepare_sources(catalog, network, workspace / "downloads", missing)
            output("manifest_sha256", catalog.sha256)
            print(json.dumps({"status": "CATALOG_EXISTING_GIT_AND_MISSING_SOURCES_VERIFIED_NO_WRITE",
                              "files": len(catalog.entries), "bytes": catalog.total_bytes,
                              "existing_git_files_full_sha_verified": len(catalog.entries) - len(missing),
                              "missing_files_sources_full_sha_verified": len(missing),
                              "source_choices": {role: sum(row["origin"] == role for row in receipts) for role in ORIGINS}}))
            return
        require(os.environ.get("IMPORT_VERIFIED_MANIFEST_SHA256") == catalog.sha256, "READ_ONLY_VERIFY_JOB_REQUIRED")
        paths, receipts = prepare_sources(catalog, network, workspace / "downloads", missing)
        candidate = git.candidate(base, catalog, paths)
        # Nonsecret recovery coordinates survive a lost push acknowledgement.
        print(json.dumps({"status": "VERIFIED_CANDIDATE_BEFORE_SINGLE_PUSH", "base": base,
                          "candidate": candidate, "new_files": len(missing)}), flush=True)
        # Recheck original branch before handing the credential to Git.
        published = git.push(base, candidate)
        output("published_commit", published["commit"])
        print(json.dumps({**published, "new_files": len(missing), "approved_total_files": len(catalog.entries),
                          "raw_full_verification_required_next": True}))


if __name__ == "__main__":
    try:
        main()
    except Refused as error:
        result = {"status": "REFUSED_NO_FORCE_OR_ROLLBACK", "code": error.code}
        if error.details is not None:
            result["details"] = error.details
        print(json.dumps(result))
        raise SystemExit(1) from None
    except Exception:
        # Never print a urllib/subprocess exception that might include headers.
        print(json.dumps({"status": "FAILED_REVIEW_STATE_BEFORE_RETRY", "code": "UNEXPECTED_FAILURE"}))
        raise SystemExit(1) from None
