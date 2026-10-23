"""GUI와 무관한 핵심 로직: 설정 저장, GitHub Gist 중계, 컴퓨터 종료."""

import json
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

APP_NAME = "ShutdownTimer"
APP_VERSION = "1.0.0"
GIST_DESCRIPTION = "shutdown-timer-relay"
API = "https://api.github.com"

POLL_INTERVAL = 10          # 원격 명령 확인 주기(초)
HEARTBEAT_INTERVAL = 60     # 온라인 상태 갱신 주기(초)
COMMAND_MAX_AGE = 120       # 이보다 오래된 원격 명령은 무시(초)


# ---------------------------------------------------------------- 설정

def config_dir():
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    return os.path.join(os.path.expanduser("~"), ".config", APP_NAME)


def config_path():
    return os.path.join(config_dir(), "config.json")


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(config_dir(), exist_ok=True)
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path())


# ---------------------------------------------------------------- 종료

def shutdown_command(force=False):
    system = platform.system()
    if system == "Windows":
        cmd = ["shutdown", "/s", "/t", "0"]
        if force:
            cmd.append("/f")
        return cmd
    if system == "Darwin":
        return ["osascript", "-e", 'tell app "System Events" to shut down']
    return ["systemctl", "poweroff"]


def shutdown_now(force=False):
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen(shutdown_command(force), **kwargs)


# ---------------------------------------------------------------- GitHub Gist 중계

class RelayError(Exception):
    pass


class GistRelay:
    """비밀 Gist 하나를 PC와 웹페이지 사이의 우편함으로 사용한다.

    status.json  : PC -> 웹  (온라인 여부, 예약 시각)
    command.json : 웹 -> PC  (예약/취소/즉시 종료 명령)
    """

    def __init__(self, token):
        self.token = token.strip()
        self.gist_id = None

    def _request(self, method, path, body=None, timeout=15):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", APP_NAME)
        req.add_header("Cache-Control", "no-cache")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RelayError("토큰이 올바르지 않습니다 (401)") from e
            if e.code == 403:
                raise RelayError("권한 부족 또는 요청 한도 초과 (403)") from e
            raise RelayError("GitHub 오류 %d" % e.code) from e
        except (urllib.error.URLError, OSError) as e:
            raise RelayError("네트워크 오류: %s" % e) from e
        return json.loads(raw) if raw else None

    def ensure_gist(self):
        if self.gist_id:
            return self.gist_id
        page = 1
        while True:
            gists = self._request("GET", "/gists?per_page=100&page=%d" % page)
            for g in gists:
                if g.get("description") == GIST_DESCRIPTION:
                    self.gist_id = g["id"]
                    return self.gist_id
            if len(gists) < 100:
                break
            page += 1
        g = self._request("POST", "/gists", {
            "description": GIST_DESCRIPTION,
            "public": False,
            "files": {
                "status.json": {"content": "{}"},
                "command.json": {"content": "{}"},
            },
        })
        self.gist_id = g["id"]
        return self.gist_id

    def read_command(self):
        g = self._request("GET", "/gists/%s?t=%d" % (self.ensure_gist(), time.time()))
        f = (g.get("files") or {}).get("command.json")
        if not f or f.get("truncated"):
            return {}
        try:
            return json.loads(f.get("content") or "{}")
        except ValueError:
            return {}

    def write_status(self, status, timeout=15):
        self._request("PATCH", "/gists/%s" % self.ensure_gist(), {
            "files": {"status.json": {"content": json.dumps(status, ensure_ascii=False, indent=2)}},
        }, timeout=timeout)


def parse_command(cmd, now=None):
    """원격 명령을 검사해 (action, 종료시각 epoch초 또는 None)을 돌려준다. 무효하면 None."""
    now = time.time() if now is None else now
    if not isinstance(cmd, dict):
        return None
    action = cmd.get("action")
    issued = cmd.get("issued_at")
    if action not in ("schedule", "cancel", "shutdown_now") or not isinstance(issued, (int, float)):
        return None
    # PC가 꺼져 있는 동안 보낸 명령이 나중에 켜졌을 때 실행되지 않도록 한다.
    if now - issued / 1000 > COMMAND_MAX_AGE:
        return None
    if action == "schedule":
        at = cmd.get("at")
        if not isinstance(at, (int, float)) or at / 1000 <= now:
            return None
        return action, at / 1000
    return action, None


class SyncWorker(threading.Thread):
    """백그라운드에서 원격 명령을 받아오고 상태를 올린다.

    get_state()   -> {"scheduled_at": epoch초 또는 None}
    on_command(action, at, cmd_id) : 새 명령이 도착하면 호출 (워커 스레드에서)
    on_status(text, ok)     : 연결 상태 문구가 바뀌면 호출 (워커 스레드에서)
    """

    def __init__(self, get_state, on_command, on_status, last_command_id=None):
        super().__init__(daemon=True)
        self.get_state = get_state
        self.on_command = on_command
        self.on_status = on_status
        self._lock = threading.Lock()
        self._relay = None
        self._last_command_id = last_command_id
        self._wake = threading.Event()
        self._dirty = True
        self._stopped = False

    def set_token(self, token):
        with self._lock:
            self._relay = GistRelay(token) if token and token.strip() else None
            self._dirty = True
        self._wake.set()

    def mark_dirty(self):
        self._dirty = True
        self._wake.set()

    def gist_url(self):
        relay = self._relay
        if relay and relay.gist_id:
            return "https://gist.github.com/" + relay.gist_id
        return None

    def build_status(self, online=True):
        scheduled = self.get_state().get("scheduled_at")
        return {
            "online": online,
            "updated_at": int(time.time() * 1000),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "scheduled_at": int(scheduled * 1000) if scheduled else None,
            "last_command_id": self._last_command_id,
            "app_version": APP_VERSION,
        }

    def go_offline(self, timeout=5):
        """앱 종료/PC 종료 직전에 오프라인으로 표시 (최선 노력)."""
        self._stopped = True
        self._wake.set()
        relay = self._relay
        if not relay or not relay.gist_id:
            return
        try:
            relay.write_status(self.build_status(online=False), timeout=timeout)
        except RelayError:
            pass

    def run(self):
        last_heartbeat = 0
        while not self._stopped:
            relay = self._relay
            if relay is None:
                self.on_status("원격 제어 꺼짐 (토큰 없음)", False)
            else:
                try:
                    cmd = relay.read_command()
                    cmd_id = cmd.get("id") if isinstance(cmd, dict) else None
                    if cmd_id and cmd_id != self._last_command_id:
                        self._last_command_id = cmd_id
                        parsed = parse_command(cmd)
                        if parsed:
                            self.on_command(parsed[0], parsed[1], cmd_id)
                        self._dirty = True
                    now = time.time()
                    if self._stopped:
                        break
                    if self._dirty or now - last_heartbeat >= HEARTBEAT_INTERVAL:
                        self._dirty = False
                        relay.write_status(self.build_status())
                        last_heartbeat = now
                    self.on_status("원격 연결됨", True)
                except RelayError as e:
                    self.on_status(str(e), False)
            self._wake.wait(POLL_INTERVAL)
            self._wake.clear()
