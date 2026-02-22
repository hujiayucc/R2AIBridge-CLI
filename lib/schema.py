import json
from typing import Any, Dict, Optional

TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    "r2_open_file": {"required": ["file_path"],
                     "properties": {"file_path": {"type": "string"}, "auto_analyze": {"type": "boolean"}}},
}

ACTIVE_TOOL_SPECS: Dict[str, Dict[str, Any]] = TOOL_SPECS


def convert_tools_list_to_specs(tools_list: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(tools_list, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for item in tools_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        schema = item.get("inputSchema")
        if not name or not isinstance(schema, dict):
            continue
        props = schema.get("properties")
        req = schema.get("required")
        if not isinstance(props, dict):
            props = {}
        if not isinstance(req, list):
            req = []
        out[name] = {"required": [str(x) for x in req if isinstance(x, str)], "properties": props}
    return out


def validate_args(tool_name: str, args: Any, tool_specs: Dict[str, Dict[str, Any]]) -> Optional[str]:
    if tool_name not in tool_specs:
        return f"未知工具: {tool_name}"
    spec = tool_specs[tool_name]
    required = spec.get("required") or []
    props = spec.get("properties") or {}
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"

    extras = [k for k in args.keys() if k not in props]
    if extras:
        return f"包含未定义参数: {', '.join(sorted(map(str, extras)))}"

    for k in required:
        if k not in args:
            return f"缺少必填参数: {k}"
        if isinstance(props.get(k), dict) and props[k].get("type") == "string":
            if not str(args.get(k, "")).strip():
                return f"必填参数 {k} 不能为空"

    for k, v in args.items():
        ps = props.get(k)
        if not isinstance(ps, dict):
            continue
        t = ps.get("type")
        if t == "string":
            if not isinstance(v, str):
                return f"参数 {k} 类型应为 string"
        elif t == "integer":
            if (not isinstance(v, int)) or isinstance(v, bool):
                return f"参数 {k} 类型应为 integer"
        elif t == "boolean":
            if not isinstance(v, bool):
                return f"参数 {k} 类型应为 boolean"
        elif t == "object":
            if not isinstance(v, dict):
                return f"参数 {k} 类型应为 object"
        elif t == "array":
            if not isinstance(v, list):
                return f"参数 {k} 类型应为 array"
    return None


def extract_mcp_error_text(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    if isinstance(resp.get("error"), str) and resp["error"].strip():
        return resp["error"].strip()
    inner = resp.get("result")
    if isinstance(inner, str) and inner.startswith("ERROR:"):
        return inner
    if isinstance(inner, dict) and inner.get("isError") is True:
        content_list = inner.get("content")
        if isinstance(content_list, list) and content_list:
            texts = []
            for item in content_list[:3]:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    texts.append(item["text"])
            if texts:
                return "\n".join(texts).strip()
        return json.dumps(inner, ensure_ascii=False)
    return ""


def print_local_tools() -> None:
    print(f"\n工具总数: {len(ACTIVE_TOOL_SPECS)}")
    for i, (name, spec) in enumerate(ACTIVE_TOOL_SPECS.items(), 1):
        req = ", ".join(spec["required"]) if spec["required"] else "-"
        opt = [k for k in spec["properties"].keys() if k not in spec["required"]]
        print(f"{i:02d}. {name}")
        print(f"    required: {req}")
        print(f"    optional: {', '.join(opt) if opt else '-'}")
    print()
