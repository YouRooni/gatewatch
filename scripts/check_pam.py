import asyncio
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from gatewatch.model import Event
from gatewatch.sources.passwords import PasswordCapture


async def main():
    if os.geteuid() != 0:
        raise SystemExit("Run this optional PAM integration check as root in a disposable test container.")
    workspace = tempfile.TemporaryDirectory(prefix="gatewatch-pam-")
    root = Path(workspace.name)
    source = Path(__file__).resolve().parents[1] / "integrations" / "pam"
    module = root / "pam_gatewatch.so"
    binary = root / "pam-selftest"
    for src, output, extra in [("pam_gatewatch.c", module, ["-shared", "-fPIC"]), ("selftest.c", binary, [])]:
        subprocess.run(
            [
                "cc",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                *extra,
                str(source / src),
                "-lpam",
                "-o",
                str(output),
            ],
            check=True,
        )
    username = "gatewatch-pam-test"
    if subprocess.run(["id", username], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError("Temporary test user already exists")
    password = secrets.token_urlsafe(32)
    pam = Path("/etc/pam.d/gatewatch-test")
    if pam.exists():
        raise RuntimeError("PAM test service already exists")
    capture = PasswordCapture(str(root / "passwords.sock"))
    subprocess.run(
        ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", username], check=True
    )
    try:
        subprocess.run(["chpasswd"], input=f"{username}:{password}\n", text=True, check=True)
        pam.write_text(f"auth optional {module} socket={root}/passwords.sock\n@include common-auth\n")
        await capture.start()
        for label, supplied, expected in [
            ("correct", password, True),
            ("incorrect", "gatewatch-test-wrong", False),
        ]:
            env = {**os.environ, "GATEWATCH_TEST_USER": username, "GATEWATCH_TEST_PASSWORD": supplied}
            process = await asyncio.create_subprocess_exec(
                str(binary), env=env, stdout=asyncio.subprocess.PIPE
            )
            pid = process.pid
            result = int((await process.communicate())[0].strip())
            assert (result == 0) == expected, (label, result)
            event = Event.new(
                "auth.success" if expected else "auth.failure",
                "203.0.113.42",
                user=username,
                method="password",
            )
            enriched = await capture.apply({"_PID": str(pid)}, event)
            assert enriched.password == (None if expected else supplied), label
            assert not capture.pending
            print(label, "authentication unchanged; captured only on failure")
        await capture.close()
        process = await asyncio.create_subprocess_exec(
            str(binary),
            env={**os.environ, "GATEWATCH_TEST_USER": username, "GATEWATCH_TEST_PASSWORD": password},
            stdout=asyncio.subprocess.PIPE,
        )
        result = int((await process.communicate())[0].strip())
        assert result == 0
        print("agent offline: successful authentication preserved")
    finally:
        await capture.close()
        pam.unlink(missing_ok=True)
        subprocess.run(["userdel", username], check=True)
    workspace.cleanup()
    print("PAM self-test passed; temporary account and PAM service removed")


asyncio.run(main())
