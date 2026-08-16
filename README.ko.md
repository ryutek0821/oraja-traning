# oraja-training

beatoraja SP7 플레이를 개인 프로필별로 수집하고 연습표를 생성하는
검증 가능한 코어와 Cloudflare 서비스의 저장소입니다.

## 빠른 시작

합성 fixture와 로컬 도메인 코어만 사용합니다.

```sh
UV_CACHE_DIR=/private/tmp/oraja-uv-cache uv run pytest -q
```

서비스 경계와 데이터 보존 정책은 `docs/architecture.md`와
`docs/data-classification.md`를 먼저 읽으세요. 실제 계정, 토큰, DB를
저장소나 issue에 붙여 넣지 마세요.

공식 서비스의 가입/IR 전송/표 취득 절차는 초대 β와 공개 gate가 통과된
후에 별도 문서로 공지됩니다. 초기 범위에는 DP/PMS, replay key input,
원시 DB의 MCP 공개, 과금이 포함되지 않습니다.
