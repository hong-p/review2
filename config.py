"""환경변수 / CLI 파라미터 로딩. 우선순위: CLI 인자 > 환경변수 > 기본값."""
import argparse
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
    llm_timeout: float = 600.0   # 로컬 LLM이 느릴 수 있어 호출당 대기 시간(초)을 길게 잡는다
    llm_max_retries: int = 2
    llm_concurrency: int = 2     # 배치 병렬 호출 시 로컬 LLM 과부하 방지

    # GitHub MCP 서버 실행 커맨드 (stdio). 게시(코멘트 등록)에만 사용한다.
    mcp_command: list[str] = field(default_factory=list)

    # 로컬 레포 경로 — tool use loop의 grep/glob/read가 여기서 파일을 읽는다.
    # Jenkins 워크스페이스에 PR 브랜치가 checkout된 디렉토리.
    repo_dir: str = "."

    # tool use loop
    max_turns: int = 15           # 에이전트 1개가 도는 최대 턴 수 (무한루프 방지)
    max_agents: int = 5           # planner가 만들 수 있는 최대 에이전트 수
    agent_concurrency: int = 2    # 에이전트 동시 실행 수 (단일 GPU면 1 권장)
    max_tool_result_chars: int = 8_000   # 도구 1회 결과 상한 (grep/read 등)
    max_file_chars: int = 20_000         # read_file 1회 상한
    max_diff_chars: int = 40_000         # get_diff 결과 상한
    max_comments_chars: int = 15_000     # 기존 리뷰 코멘트 전달 상한

    # 동작 옵션
    review_language: str = "Korean"
    dry_run: bool = False
    no_think: bool = True   # 탐색 턴에서 thinking 비활성 (Qwen3 /no_think). 타임아웃 방지


DEFAULT_MCP_CMD = (
    # 기본: 공식 GitHub MCP 서버를 docker stdio 모드로 실행
    "docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server"
)


def _build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    p = argparse.ArgumentParser(
        description="GitOps PR 리뷰봇. 인자를 생략하면 환경변수를 사용한다.",
    )
    p.add_argument("--github-token", default=env("GITHUB_TOKEN", ""),
                   help="GitHub PAT [env: GITHUB_TOKEN]")
    p.add_argument("--repo", default=env("GITHUB_REPOSITORY", ""),
                   help="owner/repo 형식 [env: GITHUB_REPOSITORY]")
    p.add_argument("--pr-number", type=int, default=int(env("PR_NUMBER", "0")),
                   help="리뷰할 PR 번호 [env: PR_NUMBER]")
    p.add_argument("--llm-base-url", default=env("LLM_BASE_URL", ""),
                   help="OpenAI-compatible 엔드포인트 [env: LLM_BASE_URL]")
    p.add_argument("--llm-api-key", default=env("LLM_API_KEY", "dummy"),
                   help="[env: LLM_API_KEY, 기본 dummy]")
    p.add_argument("--llm-model", default=env("LLM_MODEL", ""),
                   help="모델 이름 [env: LLM_MODEL]")
    p.add_argument("--llm-timeout", type=float, default=float(env("LLM_TIMEOUT", "600")),
                   help="LLM 호출당 대기 시간(초) [env: LLM_TIMEOUT, 기본 600]")
    p.add_argument("--llm-retries", type=int, default=int(env("LLM_MAX_RETRIES", "2")),
                   help="LLM 호출 실패 시 재시도 횟수 [env: LLM_MAX_RETRIES, 기본 2]")
    p.add_argument("--llm-concurrency", type=int, default=int(env("LLM_CONCURRENCY", "2")),
                   help="LLM 동시 호출 수 [env: LLM_CONCURRENCY, 기본 2]")
    p.add_argument("--mcp-cmd", default=env("GITHUB_MCP_CMD", DEFAULT_MCP_CMD),
                   help="GitHub MCP 서버 실행 커맨드(게시용) [env: GITHUB_MCP_CMD]")
    p.add_argument("--repo-dir", default=env("REPO_DIR", "."),
                   help="로컬 레포 경로(PR 브랜치 checkout됨) [env: REPO_DIR, 기본 .]")
    p.add_argument("--max-turns", type=int, default=int(env("MAX_TURNS", "15")),
                   help="에이전트당 tool use loop 최대 턴 [env: MAX_TURNS, 기본 15]")
    p.add_argument("--max-agents", type=int, default=int(env("MAX_AGENTS", "5")),
                   help="planner가 만들 최대 에이전트 수 [env: MAX_AGENTS, 기본 5]")
    p.add_argument("--agent-concurrency", type=int, default=int(env("AGENT_CONCURRENCY", "2")),
                   help="에이전트 동시 실행 수, 단일 GPU면 1 권장 [env: AGENT_CONCURRENCY, 기본 2]")
    p.add_argument("--language", default=env("REVIEW_LANGUAGE", "Korean"),
                   help="리뷰 언어 [env: REVIEW_LANGUAGE, 기본 Korean]")
    p.add_argument("--dry-run", action="store_true",
                   default=env("DRY_RUN", "") in ("1", "true", "yes"),
                   help="GitHub에 게시하지 않고 로그로만 출력 [env: DRY_RUN=1]")
    p.add_argument("--think", action="store_true",
                   default=env("THINK", "") in ("1", "true", "yes"),
                   help="탐색 턴에서도 thinking 활성 (기본은 비활성) [env: THINK=1]")
    return p


def load_config(argv: list[str] | None = None) -> Config:
    args = _build_parser().parse_args(argv)

    if "/" in args.repo:
        owner, repo = args.repo.split("/", 1)
    else:
        # --repo 미지정 시 REPO_OWNER/REPO_NAME 환경변수도 허용
        owner = os.environ.get("REPO_OWNER", "")
        repo = os.environ.get("REPO_NAME", "")

    cfg = Config(
        github_token=args.github_token,
        owner=owner,
        repo=repo,
        pr_number=args.pr_number,
        llm_base_url=args.llm_base_url,
        llm_api_key=args.llm_api_key,
        llm_model=args.llm_model,
        llm_timeout=args.llm_timeout,
        llm_max_retries=args.llm_retries,
        llm_concurrency=max(1, args.llm_concurrency),
        mcp_command=shlex.split(args.mcp_cmd),
        repo_dir=args.repo_dir,
        max_turns=args.max_turns,
        max_agents=max(1, args.max_agents),
        agent_concurrency=max(1, args.agent_concurrency),
        review_language=args.language,
        dry_run=args.dry_run,
        no_think=not args.think,
    )

    missing = [
        name
        for name, value in [
            ("--github-token (GITHUB_TOKEN)", cfg.github_token),
            ("--repo (GITHUB_REPOSITORY)", cfg.owner and cfg.repo),
            ("--pr-number (PR_NUMBER)", cfg.pr_number),
            ("--llm-base-url (LLM_BASE_URL)", cfg.llm_base_url),
            ("--llm-model (LLM_MODEL)", cfg.llm_model),
        ]
        if not value
    ]
    if missing:
        raise SystemExit(f"필수 값 누락: {', '.join(missing)}")
    return cfg
