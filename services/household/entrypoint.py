"""Initialize only the dedicated household directory, then permanently drop root."""
import os
from pathlib import Path
import sys


def main():
    if os.getuid() == 0:
        # Never recursively chown a mount or follow an operator-supplied path.
        if os.environ.get("HOUSEHOLD_DATA_DIR") != "/data/household":
            raise SystemExit("Container requires HOUSEHOLD_DATA_DIR=/data/household")
        data = Path("/data/household")
        if Path("/data").is_symlink() or data.is_symlink():
            raise SystemExit("Household storage must not be a symbolic link")
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(data, 10001, 10001)
        os.chmod(data, 0o700)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
    os.umask(0o077)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()