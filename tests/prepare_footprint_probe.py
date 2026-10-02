"""Print every image or PDF parser module that importing the Local controller loads; nothing is expected."""

import sys

import local.app

LOADED = sorted(
    name for name in sys.modules if name.split(".")[0] in {"PIL", "pypdf"} or name in {"prepare.image", "prepare.pdf"}
)

if __name__ == "__main__":
    print(",".join(LOADED) if local.app.__name__ else "")
