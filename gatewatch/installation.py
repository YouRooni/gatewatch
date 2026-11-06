#!/usr/bin/env python3
import argparse
import ast
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:(a|b|rc)([0-9]+))?")
ROLES = ("hub", "agent", "demo")
REPOSITORY = "YouRooni/gatewatch"


def version_key(value):
    match = VERSION_PATTERN.fullmatch(value.removeprefix("v")) if isinstance(value, str) else None
    if not match:
        raise ValueError("Use an exact version such as 1.0.0 or 1.1.0rc1")
    return (
        *map(int, match.group(1, 2, 3)),
        {"a": -3, "b": -2, "rc": -1, None: 0}[match[4]],
        int(match[5] or 0),
    )


def source_version(source):
    tree = ast.parse((Path(source) / "gatewatch/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            version_key(value)
            return value
    raise ValueError("Source version is missing")


def atomic_write(path, data, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.lstat() if path.exists() else None
    if previous and not stat.S_ISREG(previous.st_mode):
        raise ValueError("Refusing a non-regular installation file")
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(name, mode if mode is not None else stat.S_IMODE(previous.st_mode) if previous else 0o600)
        if previous and os.name == "posix" and os.geteuid() == 0:
            os.chown(name, previous.st_uid, previous.st_gid)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def safe_extract(archive, destination):
    destination = Path(destination).resolve()
    total = 0
    roots = set()
    with tarfile.open(archive, "r:gz") as source:
        for count, member in enumerate(source, 1):
            path = PurePosixPath(member.name)
            if (
                count > 10000
                or len(member.name) > 512
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or ":" in member.name
                or not path.parts
            ):
                raise ValueError("Unsafe release archive path")
            if not member.isfile() and not member.isdir():
                raise ValueError("Release archives cannot contain links or special files")
            total += member.size
            if total > 128 * 1024 * 1024 or member.size > 16 * 1024 * 1024:
                raise ValueError("Release archive exceeds size limits")
            roots.add(path.parts[0])
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                content = source.extractfile(member)
                if content is None:
                    raise ValueError("Missing archive member")
                with content, target.open("xb") as output:
                    shutil.copyfileobj(content, output)
                target.chmod(0o644)
    if len(roots) != 1:
        raise ValueError("Expected one release archive root")
    root = destination / roots.pop()
    version = source_version(root)
    manifest = root / "release.json"
    if manifest.exists():
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            metadata.get("version") != version
            or metadata.get("protocol") != 1
            or type(metadata.get("database_schema")) is not int
            or metadata["database_schema"] > 1
            or metadata.get("config_schema") != 1
        ):
            raise ValueError("Release requires a newer installation manager")
    return root


def verify_checksum(path, expected):
    if not isinstance(expected, str) or not re.fullmatch("[a-fA-F0-9]{64}", expected):
        raise ValueError("A valid SHA-256 checksum is required")
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        raise ValueError("Release checksum mismatch")


class Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("Release downloads require HTTPS")
        redirected = super().redirect_request(request, fp, code, message, headers, url)
        if urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(url).netloc:
            redirected.remove_header("Authorization")
        return redirected


class Releases:
    def __init__(self, repository=REPOSITORY, token=None):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository = repository
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.opener = urllib.request.build_opener(Redirects())

    def download(self, url, destination, limit, binary=False):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("Release downloads require HTTPS")
        headers = {
            "User-Agent": "Gatewatch installer",
            "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
        }
        if self.token and urllib.parse.urlsplit(url).netloc == "api.github.com":
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers)
        try:
            with self.opener.open(request, timeout=60) as response, Path(destination).open("wb") as target:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("Release download exceeds size limit")
                    target.write(chunk)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 404):
                raise ValueError(
                    "Release not found or access denied; private repositories require GH_TOKEN"
                ) from None
            raise ValueError(f"Release server returned HTTP {error.code}") from None
        except urllib.error.URLError:
            raise ValueError("Release server unavailable") from None

    def fetch(self, version, workspace):
        endpoint = "latest" if version == "latest" else "tags/v" + version.removeprefix("v")
        if version != "latest":
            version_key(version)
        metadata = Path(workspace) / "github-release.json"
        self.download(
            f"https://api.github.com/repos/{self.repository}/releases/{endpoint}", metadata, 2 * 1024 * 1024
        )
        release = json.loads(metadata.read_text())
        resolved = release["tag_name"].removeprefix("v")
        version_key(resolved)
        if release.get("draft") or (version == "latest" and release.get("prerelease")):
            raise ValueError("Requested release is not stable")
        if version != "latest" and resolved != version.removeprefix("v"):
            raise ValueError("Release version mismatch")
        assets = {asset["name"]: asset for asset in release.get("assets", [])}
        filename = f"gatewatch-{resolved}-bundle.tar.gz"
        if filename not in assets or "SHA256SUMS.txt" not in assets:
            raise ValueError("Release assets are incomplete")
        checksums = Path(workspace) / "SHA256SUMS.txt"
        self.download(assets["SHA256SUMS.txt"]["url"], checksums, 1024 * 1024, True)
        expected = checksum_from_file(checksums, filename)
        bundle = Path(workspace) / filename
        self.download(assets[filename]["url"], bundle, 32 * 1024 * 1024, True)
        verify_checksum(bundle, expected)
        root = safe_extract(bundle, Path(workspace) / "source")
        if source_version(root) != resolved:
            raise ValueError("Bundle version does not match its release")
        return root


def checksum_from_file(path, filename):
    matches = []
    for line in Path(path).read_text().splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?([^/\\]+)", line.strip())
        if match and match[2] == filename:
            matches.append(match[1])
    if len(matches) != 1:
        raise ValueError("Missing or duplicate release checksum")
    return matches[0]


class Layout:
    def __init__(self, root=Path("/")):
        self.root = Path(root).absolute()
        self.current = self.root / "opt/gatewatch"
        self.releases = self.root / "opt/gatewatch-releases"
        self.config = self.root / "etc/gatewatch"
        self.data = self.root / "var/lib/gatewatch"
        self.backups = self.root / "var/backups/gatewatch"
        self.units = self.root / "etc/systemd/system"
        self.state = self.config / "install-state.json"

    def config_for(self, role):
        return self.config / f"{role}.secret.json"

    def roles(self):
        return [
            role
            for role in ROLES
            if self.config_for(role).is_file() and (self.units / f"gatewatch-{role}.service").exists()
        ]

    def load_state(self):
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def save_state(self, state):
        atomic_write(self.state, json.dumps(state, indent=2).encode() + b"\n", 0o600)


class Manager:
    def __init__(self, layout=None, no_start=False, no_apt=False):
        self.layout = layout or Layout()
        self.no_start = no_start
        self.no_apt = no_apt

    def run(self, command, capture=False, check=True, **options):
        result = subprocess.run(
            [str(value) for value in command],
            check=False,
            text=True,
            capture_output=capture,
            cwd="/",
            **options,
        )
        if check and result.returncode:
            raise RuntimeError(f"Command failed: {Path(str(command[0])).name} (exit {result.returncode})")
        return result

    def preflight(self):
        if sys.version_info < (3, 10):
            raise ValueError("Python 3.10 or newer is required")
        if os.name != "posix" or os.geteuid() != 0:
            raise ValueError("Installation management requires Linux root")
        release = Path("/etc/os-release").read_text()
        if not any(value in release for value in ("ID=debian", "ID=ubuntu", 'ID="debian"', 'ID="ubuntu"')):
            raise ValueError("Automatic installation supports Debian and Ubuntu")
        if not self.no_start and not Path("/run/systemd/system").is_dir():
            raise ValueError("systemd is not running; --no-start is available for image builds")
        for path in (self.layout.backups, self.layout.config, self.layout.releases):
            if path.exists() and (
                path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022
            ):
                raise ValueError(
                    "Installation directories must be root-owned and not writable by other users"
                )
        self.layout.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.layout.backups.chmod(0o700)
        self.layout.config.mkdir(parents=True, exist_ok=True, mode=0o750)

    @contextlib.contextmanager
    def lock(self):
        import fcntl

        self.preflight()
        fd = os.open(self.layout.backups / "install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != 0:
                raise ValueError("Unsafe installation lock owner")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another installation is in progress") from None
            yield
        finally:
            os.close(fd)

    def provision(self):
        import pwd

        if not self.no_apt:
            environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
            self.run(["apt-get", "update", "-qq"], env=environment)
            self.run(
                ["apt-get", "install", "-y", "--no-install-recommends", "python3-venv", "ca-certificates"],
                env=environment,
            )
        try:
            user = pwd.getpwnam("gatewatch")
        except KeyError:
            self.run(
                [
                    "useradd",
                    "--system",
                    "--home-dir",
                    self.layout.data,
                    "--shell",
                    "/usr/sbin/nologin",
                    "gatewatch",
                ]
            )
            user = pwd.getpwnam("gatewatch")
        self.layout.data.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chown(self.layout.data, user.pw_uid, user.pw_gid)
        os.chown(self.layout.config, 0, user.pw_gid)
        self.layout.config.chmod(0o750)
        return user

    def build(self, source, packages=None):
        version = source_version(source)
        geo = False
        hub = self.layout.config / "hub.secret.json"
        if hub.exists():
            geo = any(
                options.get("geo") for options in json.loads(hub.read_text()).get("channels", {}).values()
            )
        profile = hashlib.sha256(
            json.dumps({"geo": geo, "packages": sorted(packages or [])}, sort_keys=True).encode()
        ).hexdigest()[:10]
        target = self.layout.releases / (version + "-" + profile)
        if target.exists():
            manifest = target / "installed.json"
            if manifest.exists() and json.loads(manifest.read_text()).get("complete"):
                return target
            raise ValueError("An incomplete version directory exists; inspect it before retrying")
        target.mkdir(parents=True, mode=0o755)
        try:
            ignored = shutil.ignore_patterns(
                ".git",
                ".venv",
                "tmp",
                "__pycache__",
                "*.pyc",
                "*.egg-info",
                "*.secret.json",
                ".env",
                "agent_diary.md",
                "dist",
                "build",
            )
            shutil.copytree(source, target / "source", ignore=ignored)
            venv = target / "venv"
            self.run([sys.executable, "-m", "venv", venv])
            python = venv / "bin/python"
            constraints = target / "source/constraints.txt"
            constraint_args = ["--constraint", constraints] if constraints.exists() else []
            self.run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    *constraint_args,
                    "--upgrade",
                    "pip>=26.2",
                    "setuptools>=84.1",
                    "wheel",
                ]
            )
            package = str(target / "source") + ("[geo]" if geo else "")
            self.run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-build-isolation",
                    *constraint_args,
                    package,
                    *(packages or []),
                ]
            )
            installed = self.run(
                [python, "-I", "-c", "import gatewatch; print(gatewatch.__version__)"], capture=True
            ).stdout.strip()
            if installed != version:
                raise ValueError("Installed version does not match its source")
            for directory, _folders, files in os.walk(target):
                Path(directory).chmod(0o755)
                for name in files:
                    path = Path(directory) / name
                    if not path.is_symlink():
                        path.chmod(0o755 if os.access(path, os.X_OK) else 0o644)
            atomic_write(
                target / "installed.json",
                json.dumps(
                    {"version": version, "complete": True, "schema": 1, "packages": packages or []}
                ).encode(),
                0o644,
            )
            return target
        except BaseException:
            if target.parent.resolve() != self.layout.releases.resolve() or target.is_symlink():
                raise ValueError("Unsafe incomplete release directory") from None
            shutil.rmtree(target)
            raise

    def run_as_service(self, executable, role, command):
        return self.run(
            [
                "runuser",
                "-u",
                "gatewatch",
                "--",
                executable,
                "--config",
                self.layout.config_for(role),
                *command,
            ]
        )

    def active_roles(self):
        return [
            role
            for role in self.layout.roles()
            if self.run(["systemctl", "is-active", "--quiet", f"gatewatch-{role}"], check=False).returncode
            == 0
        ]

    def snapshot(self):
        destination = self.layout.backups / (
            time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns() % 1000000)
        )
        destination.mkdir(mode=0o700)
        for path in self.layout.config.glob("*.json"):
            if path.is_file() and not path.is_symlink():
                shutil.copy2(path, destination / path.name)
                (destination / path.name).chmod(0o600)
        files = [self.layout.units / f"gatewatch-{role}.service" for role in ROLES]
        files += [
            self.layout.units / "gatewatch-actions.socket",
            self.layout.units / "gatewatch-actions@.service",
            self.layout.root / "usr/local/libexec/gatewatch-action-helper.py",
            self.layout.root / "usr/local/lib/security/pam_gatewatch.so",
        ]
        for index, path in enumerate(files):
            if path.is_file():
                shutil.copy2(path, destination / f"file-{index}")
        atomic_write(destination / "files.json", json.dumps([str(path) for path in files]).encode())
        databases = set()
        for role in self.layout.roles():
            config = json.loads(self.layout.config_for(role).read_text())
            database = Path(config["database"])
            if database in databases or not database.exists():
                continue
            databases.add(database)
            source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            target = sqlite3.connect(destination / f"{role}.sqlite")
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            (destination / f"{role}.sqlite").chmod(0o600)
        return destination

    def switch(self, venv):
        if self.layout.current.exists() and not self.layout.current.is_symlink():
            raise ValueError("Legacy directory must be preserved before switching")
        temporary = self.layout.current.with_name(".gatewatch-link-" + str(time.time_ns()))
        temporary.symlink_to(venv, target_is_directory=True)
        os.replace(temporary, self.layout.current)

    def write_units(self, source, roles):
        self.layout.units.mkdir(parents=True, exist_ok=True)
        for role in roles:
            data = (source / f"deploy/gatewatch-{role}.service").read_bytes()
            atomic_write(self.layout.units / f"gatewatch-{role}.service", data, 0o644)
        if (self.layout.units / "gatewatch-actions.socket").exists():
            for name in ("gatewatch-actions.socket", "gatewatch-actions@.service"):
                atomic_write(self.layout.units / name, (source / "deploy" / name).read_bytes(), 0o644)
            atomic_write(
                self.layout.root / "usr/local/libexec/gatewatch-action-helper.py",
                (source / "integrations/actions/helper.py").read_bytes(),
                0o644,
            )

    def prepare_pam(self, source, release):
        if not (self.layout.root / "usr/local/lib/security/pam_gatewatch.so").exists():
            return None
        binary = release / "pam_gatewatch.so"
        self.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fstack-protector-strong",
                "-D_FORTIFY_SOURCE=2",
                "-Wl,-z,relro,-z,now",
                source / "integrations/pam/pam_gatewatch.c",
                "-lpam",
                "-o",
                binary,
            ]
        )
        return binary

    def restore_files(self, backup):
        paths = json.loads((backup / "files.json").read_text())
        allowed = {str(self.layout.units / f"gatewatch-{role}.service") for role in ROLES} | {
            str(self.layout.units / "gatewatch-actions.socket"),
            str(self.layout.units / "gatewatch-actions@.service"),
            str(self.layout.root / "usr/local/libexec/gatewatch-action-helper.py"),
            str(self.layout.root / "usr/local/lib/security/pam_gatewatch.so"),
        }
        if set(paths) != allowed:
            raise ValueError("Backup path manifest is invalid")
        for index, name in enumerate(paths):
            path = Path(name)
            snapshot = backup / f"file-{index}"
            if snapshot.exists():
                atomic_write(path, snapshot.read_bytes(), 0o644)
            elif path.exists():
                path.unlink()

    def restart(self, roles, socket_enabled=False):
        if self.no_start:
            return
        self.run(["systemctl", "daemon-reload"])
        if socket_enabled:
            self.run(["systemctl", "start", "gatewatch-actions.socket"])
        for role in roles:
            self.run(["systemctl", "start", f"gatewatch-{role}"])
        for _ in range(10):
            time.sleep(0.5)
            if all(
                self.run(["systemctl", "is-active", "--quiet", f"gatewatch-{role}"], check=False).returncode
                == 0
                for role in roles
            ):
                break
        else:
            raise RuntimeError("A Gatewatch service did not start")
        time.sleep(2)
        for role in roles:
            self.run(["systemctl", "is-active", "--quiet", f"gatewatch-{role}"])

    def activate(self, release, roles, packages=None, start=()):
        version = source_version(release / "source")
        previous_state = self.layout.load_state()
        old_version = previous_state.get("version")
        if not old_version and self.layout.current.exists():
            old_version = self.run(
                ["runuser", "-u", "gatewatch", "--", self.layout.current / "bin/gatewatch", "--version"],
                capture=True,
            ).stdout.strip()
        if old_version and version_key(version) < version_key(old_version):
            raise ValueError("Downgrades use rollback, not upgrade")
        executable = release / "venv/bin/gatewatch"
        for role in roles:
            self.run_as_service(executable, role, ["check"])
            self.run_as_service(executable, role, ["doctor"])
        pam = self.prepare_pam(release / "source", release)
        active = self.active_roles() if not self.no_start else []
        wanted = list(dict.fromkeys([*active, *start])) if not self.no_start else []
        socket_active = (
            not self.no_start
            and self.run(
                ["systemctl", "is-active", "--quiet", "gatewatch-actions.socket"], check=False
            ).returncode
            == 0
        )
        old_target = self.layout.current.resolve() if self.layout.current.is_symlink() else None
        legacy = None
        backup = self.snapshot()
        try:
            if not self.no_start:
                if socket_active:
                    self.run(["systemctl", "stop", "gatewatch-actions.socket"])
                for role in active:
                    self.run(["systemctl", "stop", f"gatewatch-{role}"])
            for role in roles:
                self.run_as_service(executable, role, ["migrate"])
            if self.layout.current.exists() and not self.layout.current.is_symlink():
                legacy = self.layout.current.with_name("gatewatch-legacy-" + str(time.time_ns()))
                self.layout.current.rename(legacy)
            self.switch(release / "venv")
            self.write_units(release / "source", roles)
            if pam:
                atomic_write(
                    self.layout.root / "usr/local/lib/security/pam_gatewatch.so", pam.read_bytes(), 0o644
                )
            self.restart(wanted, socket_active)
            for role in start:
                if not self.no_start:
                    self.run(["systemctl", "enable", f"gatewatch-{role}"])
            self.layout.save_state(
                {
                    "version": version,
                    "release": str(release),
                    "previous": {
                        "version": old_version,
                        "target": str(old_target) if old_target else None,
                        "legacy": str(legacy) if legacy else None,
                        "backup": str(backup),
                        "state": previous_state,
                        "active_roles": active,
                        "socket_active": socket_active,
                    },
                    "roles": roles,
                    "packages": packages or [],
                }
            )
        except BaseException:
            if not self.no_start:
                for role in roles:
                    self.run(["systemctl", "stop", f"gatewatch-{role}"], check=False)
                for role in start:
                    self.run(["systemctl", "disable", f"gatewatch-{role}"], check=False)
            if old_version and old_version.startswith("0.1."):
                for role in active:
                    self.run_as_service(executable, role, ["prepare-rollback", "--target", old_version])
            if old_target:
                self.switch(old_target)
            elif legacy:
                if self.layout.current.is_symlink():
                    self.layout.current.unlink()
                legacy.rename(self.layout.current)
            elif self.layout.current.is_symlink():
                self.layout.current.unlink()
            self.restore_files(backup)
            self.restart(active, socket_active)
            raise
        print(f"Gatewatch {version} active; backup: {backup}")

    def install(self, source, role, config=None, packages=None):
        existing = self.layout.roles()
        if role in existing:
            raise ValueError("Role already installed; use upgrade")
        user = self.provision()
        target_config = self.layout.config_for(role)
        if config:
            if target_config.exists():
                raise ValueError("Config already exists; refusing to overwrite it")
            atomic_write(target_config, Path(config).read_bytes(), 0o640)
            os.chown(target_config, 0, user.pw_gid)
        if role != "hub" and not target_config.exists():
            raise ValueError("Agent and demo installation require --config")
        release = self.build(source, packages)
        if not target_config.exists():
            self.run([release / "venv/bin/gatewatch", "--config", target_config, "init"])
            target_config.chmod(0o640)
            os.chown(target_config, 0, user.pw_gid)
        if role == "agent":
            for group in ("systemd-journal", "adm"):
                if self.run(["getent", "group", group], capture=True, check=False).returncode == 0:
                    self.run(["usermod", "-a", "-G", group, "gatewatch"])
        self.activate(release, [*existing, role], packages, start=[role])

    def upgrade(self, source, packages=None):
        roles = self.layout.roles()
        if not roles:
            raise ValueError("No installed roles; use install")
        state = self.layout.load_state()
        version = source_version(source)
        packages = packages if packages is not None else state.get("packages", [])
        self.provision()
        release = self.build(source, packages)
        if state.get("version") == version and state.get("release") == str(release):
            print(f"Gatewatch {version} is already installed")
            return
        self.activate(release, roles, packages)

    def rollback(self):
        state = self.layout.load_state()
        previous = state.get("previous")
        if not previous or not previous.get("version"):
            raise ValueError("No previous installation to restore")
        backup = Path(previous["backup"])
        if backup.parent.resolve() != self.layout.backups.resolve():
            raise ValueError("Unsafe rollback backup")
        if not (backup / "files.json").is_file():
            raise ValueError("Rollback backup is unavailable")
        if previous.get("legacy"):
            legacy = Path(previous["legacy"])
            if (
                legacy.parent != self.layout.current.parent
                or not legacy.name.startswith("gatewatch-legacy-")
                or not legacy.is_dir()
            ):
                raise ValueError("Previous legacy installation is unavailable")
        else:
            target = Path(previous["target"])
            if self.layout.releases.resolve() not in target.resolve().parents or not target.is_dir():
                raise ValueError("Previous release is unavailable")
        supported = 1
        if not previous.get("legacy"):
            metadata = target.parent / "installed.json"
            if not metadata.exists():
                raise ValueError("Previous release metadata is unavailable")
            supported = json.loads(metadata.read_text()).get("schema", 0)
        for role in self.layout.roles():
            database = Path(json.loads(self.layout.config_for(role).read_text())["database"])
            if database.exists():
                db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    if db.execute("PRAGMA user_version").fetchone()[0] > supported:
                        raise ValueError(
                            "Previous version cannot read this database; restore a matching backup manually"
                        )
                finally:
                    db.close()
        active = self.active_roles() if not self.no_start else []
        socket_active = (
            not self.no_start
            and self.run(
                ["systemctl", "is-active", "--quiet", "gatewatch-actions.socket"], check=False
            ).returncode
            == 0
        )
        recovery = self.snapshot()
        current_target = self.layout.current.resolve()
        if not self.no_start:
            if socket_active:
                self.run(["systemctl", "stop", "gatewatch-actions.socket"])
            for role in active:
                self.run(["systemctl", "stop", f"gatewatch-{role}"])
        try:
            if previous["version"].startswith("0.1."):
                for role in self.layout.roles():
                    self.run_as_service(
                        self.layout.current / "bin/gatewatch",
                        role,
                        ["prepare-rollback", "--target", previous["version"]],
                    )
            if previous.get("legacy"):
                self.layout.current.unlink()
                legacy.rename(self.layout.current)
            else:
                self.switch(target)
            self.restore_files(backup)
            previous_active = previous.get(
                "active_roles",
                [role for role in active if (self.layout.units / f"gatewatch-{role}.service").exists()],
            )
            self.restart(previous_active, previous.get("socket_active", socket_active))
            self.layout.save_state(
                previous.get("state") or {"version": previous["version"], "roles": state.get("roles", [])}
            )
        except BaseException:
            if not self.no_start:
                for role in set(active) | set(previous.get("active_roles", [])):
                    self.run(["systemctl", "stop", f"gatewatch-{role}"], check=False)
            if not self.layout.current.is_symlink() and previous.get("legacy"):
                self.layout.current.rename(legacy)
            self.switch(current_target)
            self.restore_files(recovery)
            self.restart(active, socket_active)
            raise
        print(f"Rolled back to {previous['version']}; database data was preserved")

    def status(self):
        state = self.layout.load_state()
        print(
            json.dumps(
                {
                    "version": state.get("version", "legacy" if self.layout.current.exists() else None),
                    "roles": self.layout.roles(),
                    "path": str(self.layout.current),
                },
                indent=2,
            )
        )

    def uninstall(self, role):
        roles = self.layout.roles() if role == "all" else [role]
        self.snapshot()
        if "agent" in roles:
            pam = self.layout.root / "etc/pam.d/sshd"
            if pam.exists():
                data = (
                    "\n".join(
                        line
                        for line in pam.read_text().splitlines()
                        if line.strip() != "auth optional /usr/local/lib/security/pam_gatewatch.so"
                    )
                    + "\n"
                )
                atomic_write(pam, data.encode())
            if not self.no_start:
                self.run(["systemctl", "disable", "--now", "gatewatch-actions.socket"], check=False)
            for name in ("gatewatch-actions.socket", "gatewatch-actions@.service"):
                (self.layout.units / name).unlink(missing_ok=True)
        for item in roles:
            if not self.no_start:
                self.run(["systemctl", "disable", "--now", f"gatewatch-{item}"], check=False)
            (self.layout.units / f"gatewatch-{item}.service").unlink(missing_ok=True)
        if not self.no_start:
            self.run(["systemctl", "daemon-reload"])
        state = self.layout.load_state()
        state["roles"] = self.layout.roles()
        self.layout.save_state(state)
        print("Services removed. Configs, data, release directories and backups were kept.")


def parser():
    root = argparse.ArgumentParser(description="Gatewatch versioned installation manager")
    root.add_argument("--no-start", action="store_true", help="Prepare an image without running systemd")
    root.add_argument("--no-apt", action="store_true", help="Use already installed system dependencies")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("install", "upgrade"):
        item = commands.add_parser(command)
        item.add_argument("--no-start", action="store_true", default=argparse.SUPPRESS)
        item.add_argument("--no-apt", action="store_true", default=argparse.SUPPRESS)
        if command == "install":
            item.add_argument("role", choices=ROLES)
            item.add_argument("--config", help="Existing secret config to import")
        source = item.add_mutually_exclusive_group()
        source.add_argument("--source", type=Path)
        source.add_argument("--bundle", type=Path)
        source.add_argument("--version", default=None, help="Exact release version or latest")
        item.add_argument("--sha256")
        item.add_argument("--repo", default=REPOSITORY)
        item.add_argument("--package", action="append", default=None, help="Additional pinned plugin package")
    commands.add_parser("rollback")
    commands.add_parser("status")
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("role", choices=(*ROLES, "all"))
    return root


def main():
    args = parser().parse_args()
    manager = Manager(no_start=args.no_start, no_apt=args.no_apt)
    try:
        if args.command == "status":
            manager.status()
            return
        with manager.lock():
            if args.command == "rollback":
                manager.rollback()
                return
            if args.command == "uninstall":
                manager.uninstall(args.role)
                return
            if args.package and any(
                package.startswith("-") or any(ord(char) < 32 for char in package) for package in args.package
            ):
                raise ValueError("Invalid plugin package")
            with tempfile.TemporaryDirectory(prefix="gatewatch-release-") as workspace:
                if args.source:
                    source = args.source.resolve()
                elif args.bundle:
                    expected = args.sha256 or checksum_from_file(
                        args.bundle.parent / "SHA256SUMS.txt", args.bundle.name
                    )
                    verify_checksum(args.bundle, expected)
                    source = safe_extract(args.bundle, Path(workspace) / "source")
                else:
                    source = Releases(args.repo).fetch(args.version or "latest", workspace)
                if args.command == "install":
                    manager.install(source, args.role, args.config, args.package)
                else:
                    manager.upgrade(source, args.package)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"Gatewatch installer: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
