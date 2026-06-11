# GitOps PR 리뷰봇

LangGraph + GitHub MCP + 로컬 LLM(OpenAI-compatible)으로 GitOps 레포 PR을 자동 리뷰한다.

## 구조

```
fetch_pr (GitHub MCP)
    ↓  RULE.md 변경 여부 판단
┌──────────────────┬──────────────────┐   ← 병렬
[changed_analyzer] [base_analyzer]
 diff 분석          base 원본 분석
 + RULE.md(변경시)   + RULE.md(미변경시)
└──────────────────┴──────────────────┘
    ↓
[compare_reviewer]  → JSON { summary, inline_comments[] }
    ↓
┌──────────────┬──────────────┐   ← 병렬
[PR 전체 코멘트] [인라인 코멘트]
 add_issue_     pending review
 comment        flow
```

- **RULE.md 분기**: PR에서 RULE.md가 변경됐으면 changed_analyzer가 head 버전을 읽고,
  변경 안 됐으면 base_analyzer가 변경 파일들의 상위 디렉토리를 거슬러 올라가며
  base 브랜치의 RULE.md를 찾아 읽는다 (`gitops/lcm-manila/RULE.md` 등).
- **참고 환경 교차 비교**: RULE.md에 `reference_environments` yaml 블록으로 환경 그룹을
  선언하면, 변경 파일 경로의 환경 세그먼트를 같은 그룹의 다른 환경으로 치환해
  **PR에서 변경되지 않은 대응 파일**도 읽어온다. base_analyzer가 환경 간 값을 비교해
  통일 여부 / 의도된 차이 여부를 분석하고, compare_reviewer가 리뷰에 반영한다.
  포맷은 [RULE.example.md](RULE.example.md) 참고.
- **인라인 코멘트 검증**: diff를 파싱해 라인번호 주석(R/L)을 붙여 LLM에 주고,
  LLM이 지정한 (path, line, side)가 실제 diff 안에 있는지 검증한다.
  검증 실패한 코멘트는 PR 전체 코멘트 하단에 "기타 지적"으로 합쳐진다.

## 파일

| 파일 | 역할 |
|---|---|
| `main.py` | 엔트리포인트 |
| `config.py` | 환경변수 로딩 |
| `graph.py` | LangGraph 노드 6개 + 와이어링 |
| `github_mcp.py` | GitHub MCP stdio 클라이언트 래퍼 |
| `llm.py` | OpenAI-compatible LLM 래퍼 |
| `diff_utils.py` | diff 파싱, 라인번호 주석, 인라인 코멘트 검증 |
| `rules.py` | RULE.md의 reference_environments 파싱, 대응 파일 경로 생성 |
| `prompts.py` | 에이전트 3개 프롬프트 |
| `RULE.example.md` | RULE.md 권장 포맷 예시 |

## 설정

모든 값은 CLI 파라미터 또는 환경변수로 줄 수 있다. **CLI 인자 > 환경변수** 우선순위.

| CLI 파라미터 | 환경변수 | 필수 | 설명 |
|---|---|---|---|
| `--github-token` | `GITHUB_TOKEN` | ✅ | GitHub PAT (repo, PR 읽기/코멘트 권한) |
| `--repo` | `GITHUB_REPOSITORY` | ✅ | `owner/repo` (env는 `REPO_OWNER`+`REPO_NAME` 분리형도 허용) |
| `--pr-number` | `PR_NUMBER` | ✅ | 리뷰할 PR 번호 |
| `--llm-base-url` | `LLM_BASE_URL` | ✅ | 로컬 LLM OpenAI-compatible 엔드포인트 (예: `http://llm:8000/v1`) |
| `--llm-model` | `LLM_MODEL` | ✅ | 모델 이름 |
| `--llm-api-key` | `LLM_API_KEY` | | 기본 `dummy` |
| `--mcp-cmd` | `GITHUB_MCP_CMD` | | MCP 서버 실행 커맨드. 기본: `docker run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server`. 바이너리가 있으면 `github-mcp-server stdio` |
| `--language` | `REVIEW_LANGUAGE` | | 리뷰 언어, 기본 `Korean` |
| `--dry-run` | `DRY_RUN=1` | | GitHub에 게시하지 않고 로그로만 출력 |

## 실행

```bash
pip install -r requirements.txt

# CLI 파라미터로
python main.py \
  --github-token ghp_xxx \
  --repo my-org/gitops \
  --pr-number 123 \
  --llm-base-url http://localhost:8000/v1 \
  --llm-model qwen2.5-32b-instruct \
  --dry-run                # 먼저 dry-run으로 테스트 권장

# 또는 환경변수로
export GITHUB_TOKEN=ghp_xxx
export GITHUB_REPOSITORY=my-org/gitops
export PR_NUMBER=123
export LLM_BASE_URL=http://localhost:8000/v1
export LLM_MODEL=qwen2.5-32b-instruct
python main.py
```

## Jenkins 연동 예시

GitHub webhook(PR opened/synchronize) → Generic Webhook Trigger로 PR 번호를 받는 형태.

```groovy
pipeline {
    agent any
    parameters {
        string(name: 'PR_NUMBER', description: 'PR 번호')
    }
    environment {
        GITHUB_TOKEN      = credentials('github-token')
        GITHUB_REPOSITORY = 'my-org/gitops'
        LLM_BASE_URL      = 'http://llm.internal:8000/v1'
        LLM_MODEL         = 'qwen2.5-32b-instruct'
    }
    stages {
        stage('Review') {
            steps {
                sh '''
                    python3 -m venv .venv && . .venv/bin/activate
                    pip install -r requirements.txt
                    python main.py
                '''
            }
        }
    }
}
```

## 참고

- GitHub MCP 서버 버전에 따라 툴 이름이 다를 수 있다.
  `github_mcp.py` 상단 `TOOLS` 딕셔너리만 맞춰주면 된다.
  (현재 기준: `get_pull_request`, `get_pull_request_files`, `get_pull_request_diff`,
  `get_file_contents`, `add_issue_comment`, `create_pending_pull_request_review`,
  `add_comment_to_pending_review`, `submit_pending_pull_request_review`)
- diff/파일 크기 상한(`config.py`의 `max_*`)을 로컬 LLM 컨텍스트 길이에 맞게 조정할 것.
