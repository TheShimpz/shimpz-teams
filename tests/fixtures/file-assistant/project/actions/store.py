import hashlib
from typing import TypedDict

from shimpz import Context, File, action, text


class Result(TypedDict):
    bytes: int
    sha256: str


@action(human_requests=["approval"])
async def run(document: File, *, ctx: Context) -> Result:
    ctx.request_approval(title=text("Store the document"), description=text("Store the selected document."))
    data = document.read()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
