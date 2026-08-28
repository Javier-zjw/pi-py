"""
**工具参数校验**
仅实现精简的 JSON Schema 子集（支持 type、required、properties、enum、items、default），足以支撑工具调用场景，并且不引入任何第三方依赖。
"""

from __future__ import annotations

from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),)
}

class ValidationError(ValueError):
    pass

def _check(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> Any:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(isinstance(value, _TYPES.get(t, ())) for t in expected):
            errors.append(f"{path}: expected one of {expected}")
            return value

    elif expected:
        allowed = _TYPES.get(expected)
        if expected == "number" and isinstance(value, bool):
            errors.append(f"{path}: expected number")
            return value

        if expected == "integer" and isinstance(value, bool):
            errors.append(f"{path}: expected integer")
            return value

        if allowed and not isinstance(value, allowed):

            if expected in ("number", "integer") and isinstance(value, str):
                try:
                    return int(value) if expected == "integer" else float(value)
                except ValueError:
                    pass

            if expected == "boolean" and isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"

            errors.append(f"{path}: expected {expected}, got {type(value).__name__}")

            return value

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")

    if expected == "object" and isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key}: required")

        for key, sub in props.items():
            if key in value:
                value[key] = _check(value[key], sub, f"{path}.{key}", errors)

    return value

def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """返回经过类型强制转换后的参数；校验失败则抛出 `ValidationError`"""
    args = dict(arguments or {})
    for key, sub in (schema.get("properties") or {}).items():
        if key not in args and isinstance(sub, dict) and "default" in sub:
            args[key] = sub["default"]

    errors: list[str] = []
    args = _check(args, {**schema, "type": schema.get("type", "object")}, "arguments", errors)
    if errors:
        raise ValidationError(";".join(errors))
    return args