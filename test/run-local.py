"""Run pytest with local packages, an isolated config, and a disposable fixture.

Use the Python interpreter from the installation to test, for example:
    .tox/gix/bin/python test/run-local.py --backend-report=.cache/gix-coverage.json

Package-index access is disabled, and packages come from .cache/gix-wheels or PIP_FIND_LINKS,
unless --allow-index is the first argument, in which case pip is left to use its own
configuration.
"""

import os
from hashlib import sha256
from pathlib import Path
import shutil
import socket
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent.parent
    allow_index = sys.argv[1:2] == ["--allow-index"]
    pytest_args = sys.argv[2:] if allow_index else sys.argv[1:]
    sys.path.insert(0, str(root))
    from test.lib.fixtures import writable_fixture

    aliases = sha256((root / "test/fixtures/.gitconfig").read_bytes()).hexdigest()
    with writable_fixture("local-environment.sh", root, sys.platform, aliases) as fixture:
        temporary = fixture.path()
        config = temporary / "gitconfig"
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(config),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "UV_OFFLINE": "1",
        }
        if not allow_index:
            env["PIP_NO_INDEX"] = "1"
            env["PIP_FIND_LINKS"] = os.environ.get("PIP_FIND_LINKS", str(root / ".cache/gix-wheels"))
        if sys.platform == "win32" and "GIT_PYTHON_GIT_EXECUTABLE" not in env:
            executable = shutil.which("git")
            if executable is None:
                raise RuntimeError("Git executable not found in PATH")
            # Windows can share minimum-version checks for an absolute path.
            env["GIT_PYTHON_GIT_EXECUTABLE"] = os.path.abspath(executable)
        env["GIT_PYTHON_TEST_GIT_REPO_BASE"] = str(root)
        # Separate CLI and gix runs can use their own localhost daemon.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            env["GIT_PYTHON_TEST_GIT_DAEMON_PORT"] = str(listener.getsockname()[1])
        return subprocess.call(
            [sys.executable, "-m", "pytest", "--basetemp", str(temporary / "pytest"), *pytest_args],
            cwd=root,
            env=env,
        )


if __name__ == "__main__":
    sys.exit(main())
