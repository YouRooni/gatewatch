import argparse
import ast
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments])


def metadata(root):
    tree = ast.parse((root / "gatewatch/__init__.py").read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("__version__", "PROTOCOL_VERSION"):
                    values[target.id] = ast.literal_eval(node.value)
    return values


def build(root, output, tag=None):
    if git(root, "status", "--porcelain").strip():
        raise ValueError("Commit changes before building a release")
    info = metadata(root)
    version = info["__version__"]
    if tag and tag != "v" + version:
        raise ValueError("Release tag does not match package version")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Release output directory must be empty")
    environment = {**os.environ, "PIP_CONSTRAINT": str(root / "constraints.txt")}
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(output)], cwd=root, env=environment, check=True
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "twine",
            "check",
            *map(str, output.glob("*.whl")),
            *map(str, output.glob("*.tar.gz")),
        ],
        cwd=root,
        check=True,
    )
    commit = git(root, "rev-parse", "HEAD").decode().strip()
    epoch = int(git(root, "log", "-1", "--format=%ct").decode().strip())
    manifest = {
        "version": version,
        "commit": commit,
        "protocol": info["PROTOCOL_VERSION"],
        "database_schema": 1,
        "config_schema": 1,
        "python": ">=3.10",
        "platforms": ["Debian 12+", "Ubuntu 22.04+"],
        "upgrade_from": ["0.1.0", "1.x"],
        "rollback": "binary and integration files; queued data preserved",
    }
    (output / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    prefix = f"gatewatch-{version}"
    source = git(root, "archive", "--format=tar", "HEAD")
    destination = output / f"{prefix}-bundle.tar.gz"
    with (
        destination.open("wb") as target,
        gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=epoch) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as original:
            for member in original:
                if not member.isfile() and not member.isdir():
                    raise ValueError("Release source cannot contain links")
                if (
                    any(part in ("tmp", ".venv", ".git") for part in Path(member.name).parts)
                    or ".secret." in member.name
                    or member.name == ".env"
                ):
                    raise ValueError("Unsafe release source file")
                data = original.extractfile(member) if member.isfile() else None
                member.name = prefix + "/" + member.name
                member.uid = member.gid = 0
                member.uname = member.gname = "root"
                member.mtime = epoch
                archive.addfile(member, data)
        content = (json.dumps(manifest, indent=2) + "\n").encode()
        entry = tarfile.TarInfo(prefix + "/release.json")
        entry.size = len(content)
        entry.mode = 0o644
        entry.mtime = epoch
        archive.addfile(entry, io.BytesIO(content))
    (output / "gatewatch-install.py").write_bytes((root / "gatewatch/installation.py").read_bytes())
    checksums = []
    for path in sorted(output.iterdir()):
        if path.is_file():
            checksums.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name)
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="ascii")
    print(f"Gatewatch {version}: {len(checksums)} release assets")


def main():
    parser = argparse.ArgumentParser(description="Build verified Gatewatch release assets")
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--tag")
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1], args.out.resolve(), args.tag)


if __name__ == "__main__":
    main()
