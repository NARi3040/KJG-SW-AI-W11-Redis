# 줄 주석 명령 모음

PR 본문에 지적을 몰아넣지 않고, 정확한 줄에 주석을 다는 명령들이다.
PowerShell 기준이며 `OWNER`/`REPO`/`PR번호`는 실제 값으로 바꾼다.

---

## 0. 준비

```powershell
$OWNER = "NARi3040"
$REPO  = "KJG-SW-AI-W11-Redis"
$PR    = 3
```

---

## 1. 초안 PR 해제

초안 상태면 자동 리뷰(CodeRabbit 등)가 돌지 않고, 일반 리뷰어도 "검토 대상 아님"으로 본다.

```powershell
gh pr ready $PR
```

---

## 2. 어떤 줄이 바뀌었는지 확인

```powershell
# 전체 패치
gh pr diff $PR --patch

# 파일 목록만
gh pr diff $PR --name-only
```

새로 추가하는 문서는 모든 줄이 `+`라서 **원하는 줄 번호를 그대로 쓰면 된다.**

---

## 3. 한 줄에 주석 달기

```powershell
$HEAD = gh pr view $PR --json headRefOid -q .headRefOid

gh api "repos/$OWNER/$REPO/pulls/$PR/comments" `
  -f body="여기에 지적과 수정안을 함께 적는다" `
  -f commit_id=$HEAD `
  -f path="docs/planning.md" `
  -F line=96 `
  -f side="RIGHT"
```

- `side=RIGHT` → 변경 후(오른쪽) 줄
- `side=LEFT` → 변경 전(왼쪽) 줄

### 한글 본문이 깨질 때 (중요)

PowerShell에서 `-f body="한글..."`이나 `-F body=@file`로 넘기면 **한글이 깨진다.** 실제로 시험해 보니 둘 다 깨졌다.

가장 확실한 방법은 **Python에서 JSON으로 만들어 `--input -`으로 넘기는 것**이다. 아래는 실제로 확인한 코드다.

```python
import json, subprocess

HEAD = subprocess.check_output(
    ["gh", "pr", "view", "3", "--json", "headRefOid", "-q", ".headRefOid"],
    text=True,
).strip()

payload = json.dumps({
    "body": "한글 본문",
    "commit_id": HEAD,
    "path": "docs/planning.md",
    "line": 96,
    "side": "RIGHT",
}, ensure_ascii=False).encode("utf-8")

subprocess.run(
    ["gh", "api", "repos/OWNER/REPO/pulls/3/comments", "--input", "-"],
    input=payload, check=True,
)
```

> `gh pr review --body-file`는 한글이 깨지지 않는다. 본문 요약은 그 방법을 쓰면 된다.
> 깨진 주석은 `gh api -X DELETE "repos/.../pulls/comments/<id>"` 로 지운다.

## 4. 여러 줄에 묶어 주석 달기

```powershell
gh api "repos/$OWNER/$REPO/pulls/$PR/comments" `
  -f body="이 블록 전체에 대한 지적" `
  -f commit_id=$HEAD `
  -f path="docs/issues.md" `
  -F start_line=219 -F line=223 `
  -f side="RIGHT" -f start_side="RIGHT"
```

---

## 5. 바로 고쳐지게 제안하기 (`suggestion`)

````powershell
$body = @'
```suggestion
4 * (n + 1) > 3 * m
```
'@

gh api "repos/$OWNER/$REPO/pulls/$PR/comments" `
  -f body=$body `
  -f commit_id=$HEAD `
  -f path="docs/c-hashtable-and-bindings.md" `
  -F line=47 -f side="RIGHT"
````

GitHub 화면에서 **Commit suggestion** 버튼으로 바로 반영된다.

---

## 6. 리뷰 요약 제출

줄 주석을 모두 단 뒤, 본문에는 요약만 남긴다.

```powershell
gh pr review $PR --comment --body-file review-summary.md
```

`review-summary.md`에는 **무엇을 먼저 봐야 하는지**와 **체크리스트 링크**만 둔다.

---

## 7. 기존 지적 상태 확인

```powershell
# 열린 리뷰 스레드 확인 (해결 여부 포함)
gh api "repos/$OWNER/$REPO/pulls/$PR/comments" --paginate

# 리뷰 목록
gh pr view $PR --json reviews --jq ".reviews[] | [.author.login, .state, .submittedAt] | @tsv"
```

> 참고: 이 환경의 `gh`는 `--jq` 식을 공백에서 잘라 오류를 낼 때가 있다. 이때는 `gh pr view --json`으로 받아 **다른 도구로 필터링**하는 편이 안전하다.

---

## 8. 자주 쓰는 한 줄

| 하고 싶은 것 | 명령 |
| --- | --- |
| 초안 해제 | `gh pr ready 3` |
| 줄 단위 주석 | 위 3번 |
| 바로 반영 제안 | 위 5번 |
| 리뷰 요약만 제출 | `gh pr review 3 --comment --body-file summary.md` |
| 문서 브랜치 받아오기 | `git fetch origin docs` |
| 문서 내용 읽기 | `git show origin/docs:docs/planning.md` |
