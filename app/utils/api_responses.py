"""
Standardized REST API response helpers.

- RFC 7807 "Problem Details for HTTP APIs" error responses
  (https://datatracker.ietf.org/doc/html/rfc7807)
- Consistent pagination metadata builder for list endpoints.

These helpers keep the External API contract uniform so third-party
integrators can rely on a single, predictable response shape.
"""
from flask import jsonify, request

# Base URI namespace for machine-readable error "type" values. Integrators can
# match on the trailing slug (e.g. ".../invalid-api-key") regardless of host.
PROBLEM_TYPE_BASE = "https://docs.boondock.local/errors"


def problem(status, title, detail=None, error_type=None, **extra):
    """
    Build an RFC 7807 problem-details response.

    Args:
        status:     HTTP status code (int).
        title:      Short, human-readable summary of the problem type.
        detail:     Optional human-readable explanation specific to this occurrence.
        error_type: Optional slug or absolute URI identifying the problem type.
                    A bare slug is expanded against PROBLEM_TYPE_BASE.
        **extra:    Additional members to include in the problem object
                    (e.g. invalid_params, code).

    Returns:
        A Flask response with content-type application/problem+json.
    """
    if error_type is None:
        type_uri = "about:blank"
    elif error_type.startswith("http://") or error_type.startswith("https://"):
        type_uri = error_type
    else:
        type_uri = f"{PROBLEM_TYPE_BASE}/{error_type}"

    body = {
        "type": type_uri,
        "title": title,
        "status": status,
        "instance": request.path,
    }
    if detail:
        body["detail"] = detail
    body.update(extra)

    response = jsonify(body)
    response.status_code = status
    response.headers["Content-Type"] = "application/problem+json"
    return response


def build_pagination(page, per_page, total_items):
    """
    Build a consistent pagination metadata block for list responses.

    Returns a dict with page, per_page, total_items, total_pages and
    next/prev helpers so clients never have to compute paging math.
    """
    total_pages = (total_items + per_page - 1) // per_page if per_page else 0
    has_next = page < total_pages
    has_prev = page > 1
    return {
        "page": page,
        "per_page": per_page,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_next": has_next,
        "has_prev": has_prev,
        "next_page": page + 1 if has_next else None,
        "prev_page": page - 1 if has_prev else None,
    }
