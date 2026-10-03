import asyncio
from typing import TypedDict

from shimpz import Context, File, action, text


class Result(TypedDict):
    bytes: int


@action(human_requests=["approval"])
async def run(document: File, *, ctx: Context) -> Result:
    ctx.request_approval(title=text("Hold the document"), description=text("Hold the selected document."))
    size = len(document.read())
    # Never answers in time, so Stop and the delivery deadline must end it.
    await asyncio.sleep(3600)
    return {"bytes": size}
