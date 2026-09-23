"""Service-supplied HTTP routes, served on the metrics port rather than on a second server.

A route is the one place a worker may speak HTTP as workload. The framework reads the body
and bounds it; what the route means, and every status it returns, is the service's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Tuple

MAX_REQUEST_BODY = 2 * 1024 * 1024


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: Tuple[Tuple[str, str], ...] = ()


def json_response(status: int, value: Any, headers: Tuple[Tuple[str, str], ...] = ()) -> Response:
    return Response(status, json.dumps(value).encode("utf-8"), headers=headers)


# (the manager, the HTTP method, the request body) -> the response. Called on the HTTP
# server's thread, so anything slow must go through `ModelManager.run`, never around it.
Route = Callable[[Any, str, bytes], Response]
