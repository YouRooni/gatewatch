import argparse
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from build_release import metadata

ROOT = Path(__file__).resolve().parents[1]
LINKS = re.compile(r"\]\(([^\s)]+)\)|href=\"([^\"]+)\"")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-notes", type=Path)
    args = parser.parse_args()
    version = metadata(ROOT)["__version__"]
    files = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]
    failures = []
    for path in files:
        for markdown, html in LINKS.findall(path.read_text(encoding="utf-8")):
            link = urlsplit(markdown or html)
            if (
                not link.scheme
                and not link.netloc
                and link.path
                and not (path.parent / unquote(link.path)).exists()
            ):
                failures.append(f"{path.relative_to(ROOT)}: {link.path}")
    for name, expected in {
        "README.md": (f"v{version}",),
        "README.ru.md": (f"v{version}",),
        "docs/releases.md": (f"gatewatch-{version}-bundle.tar.gz", f"upgrade --version {version}"),
    }.items():
        content = (ROOT / name).read_text(encoding="utf-8")
        failures.extend(f"{name}: missing {value}" for value in expected if value not in content)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = re.search(rf"^## {re.escape(version)}\n(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    if not entry or not entry[1].strip():
        failures.append(f"CHANGELOG.md: missing notes for {version}")
    if failures:
        raise SystemExit("\n".join(failures))
    if args.release_notes:
        args.release_notes.write_text(entry[1].strip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
