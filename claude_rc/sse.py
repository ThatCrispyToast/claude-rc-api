"""A tiny, dependency-free Server-Sent Events (SSE) frame parser.

Works over any iterator of decoded text lines (e.g. ``httpx.Response.iter_lines()``).
Yields one :class:`SSEFrame` per event block. Enough of the SSE spec is
implemented for the Anthropic sessions stream: ``event:``, ``data:`` (possibly
multi-line, joined with ``\\n``), ``id:`` and ``retry:``; comment lines starting
with ``:`` are ignored (they are used as heartbeats).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Optional


@dataclass
class SSEFrame:
    event: Optional[str]
    data: str
    id: Optional[str] = None
    retry: Optional[int] = None

    @property
    def is_heartbeat(self) -> bool:
        return not self.event and not self.data


def parse_sse(lines: Iterable[str]) -> Iterator[SSEFrame]:
    """Turn a stream of text lines into SSE frames."""
    event: Optional[str] = None
    data: list[str] = []
    ident: Optional[str] = None
    retry: Optional[int] = None

    for raw in lines:
        # httpx strips the trailing newline already; normalise CR just in case.
        line = raw.rstrip("\r")

        if line == "":
            # Dispatch the accumulated frame (blank line = frame boundary).
            if event is not None or data:
                yield SSEFrame(event=event, data="\n".join(data), id=ident, retry=retry)
            event, data, ident, retry = None, [], None, None
            continue

        if line.startswith(":"):
            # Comment / heartbeat line — ignore.
            continue

        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            ident = value
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                pass

    # Flush a trailing frame that wasn't terminated by a blank line.
    if event is not None or data:
        yield SSEFrame(event=event, data="\n".join(data), id=ident, retry=retry)
