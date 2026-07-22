"""模板渲染：locale 回退 + jsonschema 校验 + {{var}} 替换。"""

from __future__ import annotations

import re

import jsonschema
from apihub_core.errors import ApiError, ErrorCode

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
DEFAULT_LOCALE = "zh-CN"


async def _fetch_template(conn, *, code: str, channel_type: str, locale: str):
    """按 locale → 默认 → 任一 回退取模板行。"""
    for loc in (locale, DEFAULT_LOCALE):
        row = await conn.fetchrow(
            "SELECT subject_tpl, body_tpl, variables_schema FROM notification_template"
            " WHERE code = $1 AND channel_type = $2 AND locale = $3",
            code,
            channel_type,
            loc,
        )
        if row:
            return row
    row = await conn.fetchrow(
        "SELECT subject_tpl, body_tpl, variables_schema FROM notification_template"
        " WHERE code = $1 AND channel_type = $2 ORDER BY locale LIMIT 1",
        code,
        channel_type,
    )
    return row


def _substitute(tpl: str, variables: dict) -> str:
    def repl(m: re.Match) -> str:
        return str(variables.get(m.group(1), ""))

    return _VAR_RE.sub(repl, tpl)


async def render(
    conn, *, code: str, channel_type: str, variables: dict, locale: str
) -> tuple[str, str]:
    row = await _fetch_template(conn, code=code, channel_type=channel_type, locale=locale)
    if not row:
        raise ApiError(ErrorCode.NOT_FOUND, f"template not found: {code}/{channel_type}")
    schema = row["variables_schema"] or {}
    if schema:
        try:
            jsonschema.validate(variables, schema)
        except jsonschema.ValidationError as e:
            raise ApiError(
                ErrorCode.INVALID_INPUT, f"invalid variables: {e.message}", http_status=400
            ) from e
    subject = _substitute(row["subject_tpl"] or "", variables)
    body = _substitute(row["body_tpl"] or "", variables)
    return subject, body
