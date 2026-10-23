# 자동 종료 타이머 (Shutdown Timer)

지정한 시간에 컴퓨터를 종료하는 트레이 상주 프로그램 + GitHub Pages 원격 제어 페이지.

- 창을 X로 닫아도 **트레이에서 계속 실행** (트레이 메뉴 → "프로그램 끝내기"로만 완전 종료)
- 시:분 지정 또는 +30분/+1시간… 빠른 예약, 예약 취소, 지금 종료
- 종료 1분 전 창을 띄워 알림 (그때 "예약 취소" 가능)
- **GitHub 페이지에서** PC 켜짐 여부·예약 시각 확인, 종료 시간 설정/취소, 지금 종료
- 윈도우 시작 시 트레이로 자동 실행 옵션

## 동작 방식

GitHub Pages는 정적 페이지라 PC와 직접 통신할 수 없어서, 내 계정의 **비밀 Gist**를 우편함으로 씁니다.

```
PC 프로그램 ──(60초마다 status.json 갱신, 10초마다 command.json 확인)──▶ 비밀 Gist ◀── GitHub 페이지
```

- PC가 60초마다 상태를 올리므로, 마지막 응답이 2분 30초 넘게 없으면 페이지에 "꺼져 있음"으로 표시됩니다.
- 원격 명령은 PC가 10초 안에 받아 실행합니다.
- 2분 넘게 지난 명령은 무시합니다 (PC가 꺼져 있을 때 누른 "지금 종료"가 나중에 켜자마자 실행되지 않도록).

## 설정

### 1. GitHub 토큰 만들기
[토큰 생성 페이지](https://github.com/settings/tokens/new?scopes=gist&description=shutdown-timer)에서 **gist 권한만** 체크해 토큰(classic)을 만듭니다.
(Fine-grained 토큰을 쓸 경우 Account permissions → Gists: Read and write)

### 2. PC 프로그램 실행 (Windows)
- **exe 사용**: Actions 탭 → "Build Windows exe" 워크플로 실행 결과의 `ShutdownTimer` 아티팩트를 받거나, `v1.0.0` 같은 태그를 푸시하면 Release에 exe가 올라갑니다.
- **파이썬으로 실행**:
  ```
  pip install -r app/requirements.txt
  pythonw app/shutdown_timer.py
  ```
- 프로그램의 "GitHub 토큰" 칸에 토큰을 넣고 **저장** → "원격 연결됨"이 뜨면 Gist가 자동 생성된 것입니다.
- "윈도우 시작 시 트레이로 자동 실행"을 켜두면 부팅 후 항상 대기합니다.

### 3. GitHub Pages 켜기
저장소 Settings → Pages → Source를 **GitHub Actions**로 설정하면 `main` 브랜치에 푸시할 때 `docs/`가 배포됩니다
(또는 Source를 "Deploy from a branch" → `main` / `/docs`로 지정).
`https://<아이디>.github.io/<저장소>/`에 접속해 같은 토큰을 넣으면 됩니다. 토큰은 그 브라우저의 localStorage에만 저장됩니다.

## 참고
- 기본은 일반 종료라 저장 안 된 문서가 있으면 Windows가 종료를 멈출 수 있습니다. 확실히 꺼야 하면 "강제 종료" 옵션을 켜세요.
- 설정 파일: `%APPDATA%\ShutdownTimer\config.json` (토큰 포함 — 공유하지 마세요)
- macOS/Linux에서도 실행은 되지만(종료 명령 `osascript` / `systemctl poweroff`) 트레이 동작은 Windows 기준으로 만들었습니다.
