# lcm-manila 리뷰 규칙

<!--
REVIEW_RULE.md 작성 가이드
- 이 파일은 자유 텍스트다. 아래 섹션 구성은 권장 포맷일 뿐, 순서/제목은 바꿔도 된다.
- 단, "환경 비교" 섹션의 environment_checks yaml 코드블록은 봇이 직접 파싱하므로 형식을 지킨다.
- 레포 루트 기준 gitops/lcm-manila/REVIEW_RULE.md 에 두면 하위 폴더 전체에 적용된다.
  더 깊은 폴더에 REVIEW_RULE.md를 추가로 두면 그 폴더 변경 시 함께 읽힌다.
-->

## 환경 비교 (environment_checks)

`changed`의 환경이 PR에서 변경되면, `compare_with`의 환경들과 비교한다.
상위 환경일수록 더 많은 하위 환경과 대조하도록 누적해서 적는다 (검증된 값이 올라와야 하므로).
봇이 변경된 환경을 감지해 비교 대상 파일 경로를 자동 계산하고, 에이전트가 값을 대조한다.

```yaml
environment_checks:
  - changed: [dev2-kr-west1, dev2-kr-west2]
    compare_with: [lcm3-kr-west1]
  - changed: [qa2-kr-west1, qa2-kr-west2]
    compare_with: [lcm3-kr-west1, dev2-kr-west1, dev2-kr-west2]
  - changed: [prd-e-kr-west1, prd-e-kr-east1]
    compare_with: [dev2-kr-west1, dev2-kr-west2, qa2-kr-west1, qa2-kr-west2]
```

- 환경명은 실제 디렉토리와 표기가 달라도 된다 — 봇이 정규화 매칭으로 잇는다
  (룰 `dev2-kr-west1` ↔ 실제 `dev2`). 못 맞추면 에이전트가 glob/list_dir로 찾아 보완한다.
- 비교는 같은 파일의 환경 위치만 치환해 대조한다 (`dev2/x.yaml` ↔ `lcm3/x.yaml`).
- 대칭 비교(A↔B 양쪽 모두 통일)가 필요하면 `changed`/`compare_with`에 서로를 넣어 양방향으로 적는다.

## 공통 규칙

<!-- 아래 규칙은 추상적으로만 적어도 된다. 리뷰어 LLM이 helm/k8s/kustomize의
     일반 베스트프랙티스를 알고 있으므로, "무엇을 따른다" 수준이면 알아서 적용한다.
     팀 고유의 제약만 구체적으로 적으면 충분하다. -->

- 일반적인 보안·운영 베스트프랙티스를 따른다 (이미지 태그 고정, 시크릿 평문 금지 등).

## helm/ 규칙

- Helm v3 차트 표준과 베스트프랙티스를 따른다.

## kustomize/ 규칙

- Kustomize 베스트프랙티스를 따른다 (overlay는 base를 올바르게 오버라이드).

## 운영(prd-*) 환경 규칙

- 운영 환경은 보수적으로 다룬다. 리소스 축소·이미지 다운그레이드 등 위험한 변경은 신중히 검토.
- (팀 고유 제약이 있으면 여기에 구체적으로 적는다.)

## 환경 간 의도된 차이 (불일치로 지적하지 말 것)

- 환경별로 다른 것이 정상인 값은 지적하지 않는다 (예: `namespace`, `ingress host`, 환경 규모 차이).

## 리뷰 포맷

- 리뷰는 정중하고 간결하게 쓴다. 트집을 위한 지적은 피하고, 실제로 문제가 되는 것에 집중한다.
- 각 지적은 `[심각도] 무엇이 문제인지 → 왜 → 어떻게 고치면 되는지` 순으로 한두 문장.
- 심각도: `error`(배포 실패/룰 위반/누락), `warn`(위험/환경 불일치), `info`(개선 제안).
- 전체 요약 맨 위에 잘된 점 한 줄을 먼저 적고, 그다음 우려사항을 나열한다.
- 단순 스타일/취향 문제는 info로 낮추고, 과하게 지적하지 않는다.
- (팀이 원하는 톤·언어·이모지 사용 여부 등을 여기에 자유롭게 적는다.)
