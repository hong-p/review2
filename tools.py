"""tool use loop에서 LLM이 호출하는 로컬 파일시스템 도구.

모든 경로는 repo_dir(로컬 체크아웃) 기준이며, repo_dir 밖 접근은 차단한다.
도구 결과는 max_tool_result_chars로 잘라 컨텍스트 폭주를 막는다.

native function calling용 스키마(TOOL_SCHEMAS)와 실행 디스패처(execute_tool)를 제공한다.
"""
import logging
import os
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    repo_dir: str
    diff_by_file: dict[str, str] = field(default_factory=dict)  # path → 해당 파일 diff
    changed_files: list[dict] = field(default_factory=list)     # [{path, status}]
    max_tool_result_chars: int = 8_000
    max_file_chars: int = 20_000
    max_diff_chars: int = 40_000


# ---- OpenAI function calling 스키마 ------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_changed_files",
            "description": "이 PR에서 변경된 파일 목록과 상태(added/modified/removed)를 반환한다. 리뷰 시작 시 먼저 호출하라.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diff",
            "description": "특정 파일의 변경 diff를 반환한다. path를 생략하면 변경 파일 목록만 반환한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "레포 루트 기준 파일 경로"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "파일 내용을 읽는다. 라인 범위(start_line, end_line)를 주면 그 부분만 읽는다. 변경되지 않은 파일이나 다른 환경의 파일을 확인할 때 쓴다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "레포 루트 기준 파일 경로"},
                    "start_line": {"type": "integer", "description": "시작 라인(1-base, 선택)"},
                    "end_line": {"type": "integer", "description": "끝 라인(선택)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "레포에서 패턴(정규식)을 검색해 매칭된 파일:라인:내용을 반환한다. 다른 환경의 같은 설정값을 비교하거나, 특정 키를 참조하는 곳을 찾을 때 쓴다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "검색할 정규식 패턴"},
                    "path_glob": {"type": "string", "description": "검색 범위 glob (예: 'overlay/prd-*/*.yaml'). 생략 시 전체."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "glob 패턴(예: 'gitops/**/deployment.yaml')으로 파일 경로 목록을 찾는다. 환경별 대응 파일이 존재하는지 확인할 때 쓴다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 패턴 (** 재귀 지원)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "디렉토리의 하위 파일/폴더 목록을 반환한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "레포 루트 기준 디렉토리 경로"},
                },
                "required": ["path"],
            },
        },
    },
]

TOOL_NAMES = {s["function"]["name"] for s in TOOL_SCHEMAS}


# ---- 실행 디스패처 -----------------------------------------------------------


def execute_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """도구 1개 실행. 결과 문자열을 반환하며, 오류도 LLM이 읽을 문자열로 돌려준다."""
    try:
        if name == "get_changed_files":
            return _get_changed_files(ctx)
        if name == "get_diff":
            return _get_diff(ctx, args.get("path"))
        if name == "read_file":
            return _read_file(ctx, args["path"], args.get("start_line"), args.get("end_line"))
        if name == "grep":
            return _grep(ctx, args["pattern"], args.get("path_glob"))
        if name == "glob":
            return _glob(ctx, args["pattern"])
        if name == "list_dir":
            return _list_dir(ctx, args["path"])
        return f"ERROR: 알 수 없는 도구 '{name}'. 사용 가능: {', '.join(sorted(TOOL_NAMES))}"
    except KeyError as e:
        return f"ERROR: 필수 인자 누락: {e}"
    except Exception as e:  # 도구 실패가 루프 전체를 죽이지 않게 문자열로
        log.warning("도구 %s 실행 실패: %s", name, e)
        return f"ERROR: {name} 실행 실패: {e}"


def _clip(ctx: ToolContext, text: str, limit: int | None = None) -> str:
    limit = limit or ctx.max_tool_result_chars
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[결과 잘림: {len(text) - limit}자 생략. 더 좁은 범위로 다시 시도하라]"


def _safe_path(ctx: ToolContext, rel: str) -> str:
    """repo_dir 밖 경로 접근 차단."""
    rel = (rel or "").lstrip("/")
    root = os.path.realpath(ctx.repo_dir)
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"레포 밖 경로 접근 불가: {rel}")
    return full


def _get_changed_files(ctx: ToolContext) -> str:
    if not ctx.changed_files:
        return "(변경된 파일 없음)"
    return "\n".join(f"{f['path']} ({f.get('status', '')})" for f in ctx.changed_files)


def _get_diff(ctx: ToolContext, path: str | None) -> str:
    if not path:
        return "변경 파일 목록:\n" + _get_changed_files(ctx)
    path = path.lstrip("/")
    if path in ctx.diff_by_file:
        return _clip(ctx, ctx.diff_by_file[path], ctx.max_diff_chars)
    # 경로 끝부분 매칭 (a/ b/ 접두어 차이 등 흡수)
    for p, d in ctx.diff_by_file.items():
        if p.endswith(path) or path.endswith(p):
            return _clip(ctx, d, ctx.max_diff_chars)
    return f"(이 PR에서 {path}의 변경 없음. 변경된 파일은 get_changed_files로 확인)"


def _read_file(ctx: ToolContext, path: str, start: int | None, end: int | None) -> str:
    full = _safe_path(ctx, path)
    if not os.path.isfile(full):
        return f"(파일 없음: {path}). glob이나 list_dir로 실제 경로를 먼저 찾아라."
    with open(full, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    if start is not None or end is not None:
        s = max(1, start or 1)
        e = min(len(lines), end or len(lines))
        body = "".join(f"{i + s}\t{ln}" for i, ln in enumerate(lines[s - 1 : e]))
        return _clip(ctx, f"# {path} (라인 {s}-{e})\n{body}", ctx.max_file_chars)
    return _clip(ctx, f"# {path}\n" + "".join(lines), ctx.max_file_chars)


def _split_glob(path_glob: str) -> tuple[str, str | None]:
    """path_glob을 (글로브 없는 상위 디렉토리, --include 파일패턴)으로 분리.

    예: 'overlay/**/deployment.yaml' → ('overlay', 'deployment.yaml')
        'overlay/prd-*/*.yaml'       → ('overlay', '*.yaml')
        'gitops/lcm/values.yaml'     → ('gitops/lcm', 'values.yaml')
    """
    parts = path_glob.strip("/").split("/")
    dir_parts = []
    for p in parts[:-1]:
        if any(ch in p for ch in "*?["):
            break
        dir_parts.append(p)
    return "/".join(dir_parts), parts[-1] or None


def _grep(ctx: ToolContext, pattern: str, path_glob: str | None) -> str:
    include = None
    rel_dir = ""
    if path_glob:
        rel_dir, include = _split_glob(path_glob)
    search_root = _safe_path(ctx, rel_dir)
    if not os.path.isdir(search_root):
        return f"(검색 경로 없음: {rel_dir or '.'}). glob/list_dir로 실제 경로를 먼저 찾아라."
    cmd = ["grep", "-rnI", "--exclude-dir=.git", "-E", pattern]
    if include:
        cmd.append(f"--include={include}")
    cmd.append(".")
    try:
        out = subprocess.run(
            cmd, cwd=search_root, capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: grep 타임아웃. 더 좁은 path_glob으로 다시 시도하라."
    if out.returncode not in (0, 1):  # 1 = 매칭 없음(정상)
        return f"ERROR: grep 실패: {out.stderr.strip()[:300]}"
    if not out.stdout.strip():
        return f"(매칭 없음: pattern='{pattern}', range='{path_glob or '전체'}')"
    return _clip(ctx, out.stdout.strip())


def _glob(ctx: ToolContext, pattern: str) -> str:
    import glob as _g
    root = _safe_path(ctx, "")
    matches = _g.glob(os.path.join(root, pattern), recursive=True)
    rels = sorted(os.path.relpath(m, root) for m in matches if os.path.isfile(m))
    if not rels:
        return f"(매칭 파일 없음: {pattern})"
    return _clip(ctx, "\n".join(rels))


def _list_dir(ctx: ToolContext, path: str) -> str:
    full = _safe_path(ctx, path)
    if not os.path.isdir(full):
        return f"(디렉토리 없음: {path})"
    entries = []
    for name in sorted(os.listdir(full)):
        if name == ".git":
            continue
        kind = "dir" if os.path.isdir(os.path.join(full, name)) else "file"
        entries.append(f"{name} ({kind})")
    return _clip(ctx, "\n".join(entries) or "(빈 디렉토리)")
