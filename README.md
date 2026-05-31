# 네이버 블로그 댓글/메일 캡처

네이버 블로그 글 링크를 기준으로 댓글을 수집하고, 캡처대상으로 체크된 댓글 또는 해당 댓글의 이메일에 대한 네이버 메일 본문을 PNG로 저장하는 Windows용 GUI 프로그램입니다.

## 실행 방식 요약

| 실행 방식 | 실행 파일 | Python 필요 | 주 용도 |
| --- | --- | --- | --- |
| Python 개발 실행 | `run_naver_comment_capture.bat` | 필요 | 코드를 수정하면서 실행하거나 개발 환경에서 테스트할 때 |
| 패키징 실행 | `release\NaverCommentCapture\NaverCommentCapture.exe` | 불필요 | 다른 컴퓨터에서 repository를 받은 뒤 바로 사용할 때 |

두 방식 모두 Chrome은 필요합니다. 프로그램은 설치된 Chrome을 자동으로 찾아 앱 전용 Chrome 프로필로 실행합니다.

## 1. `run_naver_comment_capture.bat`로 실행

이 방식은 Python이 설치된 컴퓨터에서 사용합니다.

1. repository 폴더를 엽니다.
2. `run_naver_comment_capture.bat`를 더블클릭합니다.
3. 처음 실행하면 `.venv` 가상환경을 만들고 `requirements.txt`의 Playwright 의존성을 설치합니다.
4. GUI가 열리면 `네이버 로그인 열기`로 로그인한 뒤, 열린 Chrome 창을 그대로 둔 채 `캡처대상 찾기`를 누릅니다.
5. 테이블에서 캡처할 댓글의 체크박스를 확인하거나 직접 조정한 뒤 `댓글 캡처` 또는 `이메일 본문 캡처`를 사용합니다.

이 방식은 `naver_comment_capture.py` 최신 코드를 바로 실행하므로, 코드를 수정한 뒤 빠르게 확인할 때 좋습니다.

## 2. `NaverCommentCapture.exe`로 실행

이 방식은 Python이 없어도 실행됩니다.

1. repository를 다운로드하거나 복사합니다.
2. `release\NaverCommentCapture\NaverCommentCapture.exe`를 더블클릭합니다.
3. 처음 실행하면 `release\NaverCommentCapture\chrome-profile` 폴더가 생성됩니다.
4. GUI에서 `네이버 로그인 열기`로 로그인한 뒤, 열린 Chrome 창을 그대로 둔 채 `캡처대상 찾기`를 누릅니다.
5. 테이블에서 캡처할 댓글의 체크박스를 확인하거나 직접 조정한 뒤 `댓글 캡처` 또는 `이메일 본문 캡처`를 사용합니다.

주의: `NaverCommentCapture.exe` 파일만 따로 빼서 실행하지 말고, `release\NaverCommentCapture` 폴더 전체를 유지해야 합니다. `_internal` 폴더 안에 실행에 필요한 파일들이 들어 있습니다.

## 두 실행 방식의 차이

- `run_naver_comment_capture.bat`는 현재 컴퓨터의 Python으로 `naver_comment_capture.py`를 실행합니다.
- `NaverCommentCapture.exe`는 Python 런타임과 Playwright 관련 파일이 패키징되어 있어 Python 설치 없이 실행됩니다.
- 두 방식은 서로 다른 위치의 `chrome-profile`을 사용할 수 있습니다.
  - bat 실행: repository 루트의 `chrome-profile`
  - exe 실행: `release\NaverCommentCapture\chrome-profile`
- 로그인 상태도 프로필 위치에 따라 따로 저장될 수 있으므로, 실행 방식을 바꾸면 다시 로그인해야 할 수 있습니다.

## 기본 사용 순서

1. `블로그 글 링크`에 네이버 블로그 글 주소를 입력합니다.
2. `저장 폴더`를 확인합니다.
3. `캡처대상 찾기`를 누릅니다.
4. 댓글이 작성 시각 오름차순으로 표시됩니다.
5. 프로그램은 마지막 `공유 완료` 댓글 이후에 달린 댓글들을 자동으로 체크합니다. `공유 완료` 댓글이 없으면 자동 체크하지 않습니다.
6. `캡처대상` column의 `☑`/`☐`를 클릭하거나 Space 키로 캡처 대상을 조정합니다.
7. `댓글 캡처`를 누르면 체크된 댓글 박스들이 PNG로 저장됩니다.
8. `이메일 본문 캡처`를 누르면 체크된 댓글 중 이메일이 있는 대상의 네이버 메일 본문이 PNG로 저장됩니다.

## exe 다시 만들기

코드를 수정한 뒤 exe에도 반영하려면 Python이 설치된 개발 컴퓨터에서 다음 파일을 실행합니다.

```bat
build_exe.bat
```

빌드가 끝나면 `release\NaverCommentCapture\NaverCommentCapture.exe`가 새로 생성됩니다.

## 문제 해결

- Chrome을 찾지 못한다는 오류가 나오면 Chrome을 설치한 뒤 다시 실행합니다.
- 로그인/캡처용 Chrome이 이미 열려 있다는 안내가 나오면, 기존 앱 전용 Chrome 창에서 계속 진행하거나 해당 Chrome 창을 닫고 다시 시도합니다.
- 다른 컴퓨터로 옮길 때는 `release\NaverCommentCapture` 폴더 전체가 포함되어야 합니다.
