"""Checks that config.json names the image of the release and that docker/Dockerfile.tmpl starts the app as config.json does.

Every release vX.Y.Z of the app has its own image, supervisely/export-pointclouds-to-las:<SDK>-X.Y.Z (the Supervisely
SDK pinned in dev_requirements.txt, then the release), which carries the app's code: the Release workflow builds it
from the release's commit. The SDK leads the tag, as in the other apps' images, so the release checks read it from
there.

    python3 docker/check_image.py                   # pull requests: config.json, dev_requirements.txt and the Dockerfile agree
    python3 docker/check_image.py --release v0.1.0  # the Release workflow, before it builds anything
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "supervisely/export-pointclouds-to-las"
# Where the agents run the app (the release they download is mounted over /app): the image's copy lives there too.
APP_DIR = "/app/repo"
VERSION = r"\d+\.\d+\.\d+"
SDK_PIN = re.compile(rf"supervisely(?:\[[^\]]*\])?==({VERSION})")


def instructions(dockerfile: Path) -> list:
    """(INSTRUCTION, arguments) of every instruction, continuation lines joined and comments dropped."""
    result, current = [], ""
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        keyword, _, arguments = (current + stripped).partition(" ")
        result.append((keyword.upper(), arguments.strip()))
        current = ""
    return result


def last(found: list, keyword: str):
    values = [arguments for name, arguments in found if name == keyword]
    return values[-1] if values else None


def sdk_version() -> str:
    for line in (ROOT / "dev_requirements.txt").read_text(encoding="utf-8").splitlines():
        match = SDK_PIN.fullmatch(line.split("#", 1)[0].strip())
        if match:
            return match.group(1)
    return ""


def check(config: dict, release) -> list:
    errors = []
    sdk = sdk_version()
    if not sdk:
        errors.append("dev_requirements.txt pins no supervisely==X.Y.Z")
    image = config.get("docker_image", "")
    match = re.fullmatch(rf"{re.escape(IMAGE)}:({VERSION})-({VERSION})", image)
    if match is None:
        errors.append(f'config.json: "docker_image" is {image!r}, not {IMAGE}:<SDK>-<release> (e.g. {IMAGE}:{sdk}-0.1.0)')
    elif sdk and match.group(1) != sdk:
        errors.append(f'config.json: "docker_image" {image!r} names SDK {match.group(1)}, dev_requirements.txt pins {sdk}')
    if release is not None:
        wanted = release[1:] if re.fullmatch(rf"v{VERSION}", release) else None
        if wanted is None:
            errors.append(f"release {release!r} is not vX.Y.Z")
        elif match is None or match.group(2) != wanted:
            errors.append(
                f"config.json's image {image!r} is not the image of release {release}: release a commit whose "
                f'config.json has "docker_image": "{IMAGE}:{sdk}-{wanted}"'
            )

    found = instructions(ROOT / "docker" / "Dockerfile.tmpl")
    workdir = last(found, "WORKDIR")
    if workdir != APP_DIR:
        errors.append(f"docker/Dockerfile.tmpl: the last WORKDIR is {workdir!r}, not {APP_DIR!r}")
    cmd = last(found, "CMD")
    main_script = ["python", config.get("main_script", "src/main.py")]
    try:
        cmd = json.loads(cmd) if cmd is not None else None
    except json.JSONDecodeError:
        pass
    if cmd != main_script:
        errors.append(f"docker/Dockerfile.tmpl: the last CMD is {cmd!r}, not config.json's main_script {json.dumps(main_script)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", help="the release's tag, vX.Y.Z")
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    errors = check(config, parser.parse_args().release)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"OK: {config['docker_image']}, WORKDIR {APP_DIR}, CMD python {config.get('main_script', 'src/main.py')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
