# lcm-manila 리뷰 규칙

<!--
REVIEW_RULE.md 작성 가이드
- 이 파일은 자유 텍스트다. 아래 섹션 구성은 권장 포맷일 뿐, 순서/제목은 바꿔도 된다.
- 단, "참고 환경" 섹션의 yaml 코드블록(reference_environments)만은 봇이 직접 파싱하므로
  형식을 지켜야 한다.
- 레포 루트 기준 gitops/lcm-manila/REVIEW_RULE.md 에 두면 하위 폴더 전체에 적용된다.
  더 깊은 폴더에 REVIEW_RULE.md를 추가로 두면 그 폴더 변경 시 함께 읽힌다.
-->

## 참고 환경

같은 그룹에 속한 환경들은 설정이 통일되어야 한다.
한 환경의 파일이 변경되면 봇이 같은 그룹의 다른 환경에서 대응 파일을 읽어 비교한다.
(예: `kustomize/overlay/dev2/deployment.yaml` 변경 → `dev/`, `qa2/`의 같은 파일과 비교)

```yaml
reference_environments:
  - [dev, dev2, qa2]
  - [prd-a, prd-b, prd-c]
```

## 공통 규칙

- 이미지 태그에 `latest` 사용 금지. 반드시 버전 태그를 명시한다.
- 시크릿 값(비밀번호, 토큰, 인증서)을 평문으로 커밋하지 않는다.
- 들여쓰기는 스페이스 2칸. 탭 금지.

## helm/ 규칙

- `values.yaml`의 키 추가/삭제 시 차트 템플릿에서 실제로 사용하는지 확인할 것.
- `Chart.yaml`의 `version`은 차트 내용 변경 시 반드시 올린다.

## kustomize/overlay/dev*, qa* 규칙

- replicas는 1~2 범위 내에서 자유롭게 변경 가능.
- 리소스 limit은 prd보다 작거나 같아야 한다.

## kustomize/overlay/prd-* 규칙

- replicas 최소 2 이상.
- 이미지 태그 변경은 dev/qa에서 검증된 태그만 허용 — PR 본문에 검증 근거 링크 필수.
- 리소스 limit 축소는 금지. 축소가 필요하면 사유를 PR 본문에 명시.
- prd-* 그룹 내 환경 간에는 이미지 태그가 항상 동일해야 한다 (동시 배포 원칙).

## 환경 간 의도된 차이 (불일치로 지적하지 말 것)

- `namespace`, `ingress host`, `nodeSelector`는 환경별로 다른 것이 정상.
- dev/qa의 replicas와 리소스는 prd보다 작은 것이 정상.
