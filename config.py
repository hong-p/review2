"""환경변수 / Jenkins 파라미터 로딩."""
import os
import shlex
from dataclasses import dataclass, field


@dataclass
class Config:
    # GitHub
    github_token: str = ""
    owner: str = ""
    repo: str = ""
    pr_number: int = 0

    # 로컬 LLM (OpenAI-compatible)
    llm_base_url: str = ""
    llm_api_key: str = "dummy"
    llm_model: str = ""

    # GitHub MCP 서버 실행 커맨드 (stdio)
    mcp_command: list[str] = field(default_factory=list)

    # 동작 옵션
    review_language: str = "Korean"
    dry_run: bool = False
    max_diff_chars: int = 60_000
    max_file_chars: int = 20_000
    max_base_total_chars: int = 80_000


def load_config() -> Config:
    repo_full = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo_full:
        owner, repo = repo_full.split("/", 1)
    else:
        owner = os.environ.get("REPO_OWNER", "")
        repo = os.environ.get("REPO_NAME", "")

    mcp_cmd_raw = os.environ.get(
        "GITHUB_MCP_CMD",
        # 기본: 공식 GitHub MCP 서버를 docker stdio 모드로 실행
        "docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server",
    )

    cfg = Config(
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        owner=owner,
        repo=repo,
        pr_number=int(os.environ.get("PR_NUMBER", "0")),
        llm_base_url=os.environ.get("LLM_BASE_URL", ""),
        llm_api_key=os.environ.get("LLM_API_KEY", "dummy"),
        llm_model=os.environ.get("LLM_MODEL", ""),
        mcp_command=shlex.split(mcp_cmd_raw),
        review_language=os.environ.get("REVIEW_LANGUAGE", "Korean"),
        dry_run=os.environ.get("DRY_RUN", "") in ("1", "true", "yes"),
    )

    missing = [
        name
        for name, value in [
            ("GITHUB_TOKEN", cfg.github_token),
            ("GITHUB_REPOSITORY (또는 REPO_OWNER/REPO_NAME)", cfg.owner and cfg.repo),
            ("PR_NUMBER", cfg.pr_number),
            ("LLM_BASE_URL", cfg.llm_base_url),
            ("LLM_MODEL", cfg.llm_model),
        ]
        if not value
    ]
    if missing:
        raise SystemExit(f"필수 환경변수 누락: {', '.join(missing)}")
    return cfg
