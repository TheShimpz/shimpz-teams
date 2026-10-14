"""Print every image or PDF parser or renderer module that importing the Local controller loads; none is expected."""

import sys

import local.app

LOADED = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in {"PIL", "pypdf", "pypdfium2", "pypdfium2_raw"}
    or name in {"prepare.image", "prepare.pdf", "prepare.page"}
)

if __name__ == "__main__":
    print(",".join(LOADED) if local.app.__name__ else "")
