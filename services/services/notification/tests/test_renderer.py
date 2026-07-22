"""renderer 单测（_FakeConn admin 模式）。"""

import pytest
from apihub_core.errors import ApiError
from notification import renderer as renderer_mod


class _FakeRow:
    def __init__(self, d):
        self._d = d

    def __getitem__(self, k):
        return self._d[k]


class _FakeConn:
    def __init__(self, templates):
        self._templates = templates

    async def fetchrow(self, sql, *args):
        code, ctype = args[0], args[1]
        loc = args[2] if len(args) > 2 else None
        matches = [t for t in self._templates if t["code"] == code and t["channel_type"] == ctype]
        if not matches:
            return None
        if loc:
            for t in matches:
                if t["locale"] == loc:
                    return _FakeRow(t)
            return None
        return _FakeRow(matches[0])


class TestRender:
    async def test_substitutes_vars(self):
        conn = _FakeConn(
            [
                {
                    "code": "t",
                    "channel_type": "email",
                    "locale": "zh-CN",
                    "subject_tpl": "Hi {{name}}",
                    "body_tpl": "ID={{id}}",
                    "variables_schema": {
                        "type": "object",
                        "required": ["name", "id"],
                        "properties": {"name": {"type": "string"}, "id": {"type": "string"}},
                    },
                }
            ]
        )
        subject, body = await renderer_mod.render(
            conn,
            code="t",
            channel_type="email",
            variables={"name": "Bob", "id": "x1"},
            locale="zh-CN",
        )
        assert subject == "Hi Bob" and body == "ID=x1"

    async def test_locale_fallback_to_default(self):
        conn = _FakeConn(
            [
                {
                    "code": "t",
                    "channel_type": "email",
                    "locale": "zh-CN",
                    "subject_tpl": "S",
                    "body_tpl": "B",
                    "variables_schema": {},
                }
            ]
        )
        subject, body = await renderer_mod.render(
            conn, code="t", channel_type="email", variables={}, locale="en"
        )
        assert subject == "S" and body == "B"

    async def test_missing_var_renders_empty(self):
        conn = _FakeConn(
            [
                {
                    "code": "t",
                    "channel_type": "email",
                    "locale": "zh-CN",
                    "subject_tpl": "[{{a}}]",
                    "body_tpl": "B",
                    "variables_schema": {},
                }
            ]
        )
        s, _ = await renderer_mod.render(
            conn, code="t", channel_type="email", variables={}, locale="zh-CN"
        )
        assert s == "[]"

    async def test_schema_validation_failure(self):
        conn = _FakeConn(
            [
                {
                    "code": "t",
                    "channel_type": "email",
                    "locale": "zh-CN",
                    "subject_tpl": "S",
                    "body_tpl": "B",
                    "variables_schema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                    },
                }
            ]
        )
        with pytest.raises(ApiError):
            await renderer_mod.render(
                conn, code="t", channel_type="email", variables={}, locale="zh-CN"
            )

    async def test_template_not_found(self):
        conn = _FakeConn([])
        with pytest.raises(ApiError):
            await renderer_mod.render(
                conn, code="nope", channel_type="email", variables={}, locale="zh-CN"
            )
