"""SELECT-only guard for `halocli sql` (reads the LOCAL mirror, never Halo).

The mirror database contains tenant data we could already read through the
API; this guard exists so an ad-hoc query cannot mutate that local file
(SQLite itself would happily run it). Stance note: this is deliberately
unlike Halo's server-side SQL surface - nothing here reaches the tenant.

Rules (conservative by design; the same stance Servosity's tested guard
takes): the statement must begin with SELECT/WITH/EXPLAIN, must be a single
statement (one optional trailing semicolon), and must not contain mutation
keywords even inside string literals - rejecting a legal-but-awkward query
is fine; silently allowing a mutation is not.
"""

from __future__ import annotations

import re

_STARTS_WITH = re.compile(r"^\s*(SELECT\b|WITH\b|EXPLAIN\b)", re.IGNORECASE)

# Word-boundary matches. Literals like 'please update this row' are
# conservatively rejected (documented); no attempt is made to parse strings.
_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE|REINDEX|TRUNCATE|"
    r"ATTACH|DETACH|PRAGMA|VACUUM|ANALYZE|BEGIN|COMMIT|ROLLBACK|GRANT|REVOKE"
    r")\b",
    re.IGNORECASE,
)


class SQLGuardError(ValueError):
    """The query is not a read-only SELECT over the local mirror."""


def validate_select_only(query: str) -> str:
    """Return the normalized statement, or raise SQLGuardError.

    Normalization strips one optional trailing semicolon so the common
    copy-paste form (`SELECT ...;`) works, while a second statement
    (`SELECT 1; DELETE ...`) never does.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        raise SQLGuardError("Empty query. Provide a SELECT statement.")
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
        if not cleaned:
            raise SQLGuardError("Empty query. Provide a SELECT statement.")
    if ";" in cleaned:
        raise SQLGuardError("Refusing multiple statements: only a single SELECT is allowed.")
    if not _STARTS_WITH.match(cleaned):
        raise SQLGuardError(
            "Refusing non-SELECT statement: only SELECT/WITH/EXPLAIN queries "
            "run against the local mirror."
        )
    match = _FORBIDDEN.search(cleaned)
    if match:
        raise SQLGuardError(
            f"Refusing statement containing mutation keyword "
            f"'{match.group(0).upper()}'. This guard is conservative: remove "
            "the keyword (even inside a literal) or fix the query."
        )
    return cleaned
