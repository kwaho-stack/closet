"""자동 종료 타이머 - 지정한 시간에 컴퓨터를 종료하고, 트레이에 상주하며 웹에서 원격 제어된다."""

import datetime
import os
import platform
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import pystray
from PIL import Image, ImageDraw

import core

WARNING_SECONDS = 60        # 예약 종료 전에 창을 띄워 알리는 시간


def make_icon_image(active=False):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (230, 70, 60, 255) if active else (60, 140, 230, 255)
    d.ellipse((2, 2, size - 2, size - 2), fill=color)
    d.arc((16, 16, size - 16, size - 16), start=-60, end=240, fill="white", width=6)
    d.line((size // 2, 10, size // 2, size // 2), fill="white", width=6)
    return img


def fmt_remaining(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d" % (h, m, s)


# ---------------------------------------------------------------- 윈도우 시작 프로그램

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_supported():
    return platform.system() == "Windows"


def autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s" --minimized' % sys.executable
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    return '"%s" "%s" --minimized' % (exe, os.path.abspath(__file__))


def autostart_enabled():
    if not autostart_supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, core.APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, core.APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, core.APP_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- 앱

class App:
    def __init__(self, start_minimized=False):
        self.cfg = core.load_config()
        self.scheduled_at = None        # epoch 초
        self.warned = False
        self.shutting_down = False
        self.closed = False
        self.events = queue.Queue()     # 다른 스레드 -> Tk 스레드

        self.root = tk.Tk()
        self.root.title("자동 종료 타이머")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._build_ui()

        self.tray = pystray.Icon(
            core.APP_NAME, make_icon_image(), "자동 종료 타이머",
            menu=pystray.Menu(
                pystray.MenuItem("열기", lambda: self.events.put(("show",)), default=True),
                pystray.MenuItem("예약 취소", lambda: self.events.put(("cancel",))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("프로그램 끝내기", lambda: self.events.put(("quit",))),
            ),
        )
        threading.Thread(target=self.tray.run, daemon=True).start()

        self.worker = core.SyncWorker(
            get_state=lambda: {"scheduled_at": self.scheduled_at},
            on_command=lambda action, at, cmd_id: self.events.put(("remote", action, at, cmd_id)),
            on_status=lambda text, ok: self.events.put(("remote_status", text, ok)),
            last_command_id=self.cfg.get("last_command_id"),
        )
        self.worker.set_token(self.cfg.get("token", ""))
        self.worker.start()

        if start_minimized:
            self.root.withdraw()
        self.root.after(200, self.tick)

    # ------------------------------------------------------------ UI

    def _build_ui(self):
        pad = {"padx": 12, "pady": 4}
        frm = ttk.Frame(self.root, padding=12)
        frm.grid()

        self.clock_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.clock_var, font=("Segoe UI", 22, "bold")).grid(
            row=0, column=0, columnspan=4, **pad)

        self.state_var = tk.StringVar(value="예약 없음")
        self.state_label = ttk.Label(frm, textvariable=self.state_var, font=("Segoe UI", 11))
        self.state_label.grid(row=1, column=0, columnspan=4, **pad)

        now = datetime.datetime.now() + datetime.timedelta(hours=1)
        self.hour_var = tk.StringVar(value="%02d" % now.hour)
        self.min_var = tk.StringVar(value="%02d" % now.minute)
        row = ttk.Frame(frm)
        row.grid(row=2, column=0, columnspan=4, **pad)
        ttk.Label(row, text="종료 시각").pack(side="left", padx=(0, 6))
        ttk.Spinbox(row, from_=0, to=23, width=3, format="%02.0f", wrap=True,
                    textvariable=self.hour_var).pack(side="left")
        ttk.Label(row, text=":").pack(side="left")
        ttk.Spinbox(row, from_=0, to=59, width=3, format="%02.0f", wrap=True,
                    textvariable=self.min_var).pack(side="left")
        ttk.Button(row, text="예약", command=self.schedule_from_inputs).pack(side="left", padx=(8, 0))

        quick = ttk.Frame(frm)
        quick.grid(row=3, column=0, columnspan=4, **pad)
        for label, minutes in (("+30분", 30), ("+1시간", 60), ("+2시간", 120), ("+3시간", 180)):
            ttk.Button(quick, text=label, width=7,
                       command=lambda m=minutes: self.schedule(time.time() + m * 60)).pack(side="left", padx=2)

        actions = ttk.Frame(frm)
        actions.grid(row=4, column=0, columnspan=4, **pad)
        ttk.Button(actions, text="예약 취소", command=self.cancel).pack(side="left", padx=4)
        ttk.Button(actions, text="지금 종료", command=self.confirm_shutdown_now).pack(side="left", padx=4)

        ttk.Separator(frm).grid(row=5, column=0, columnspan=4, sticky="ew", pady=8)

        remote = ttk.LabelFrame(frm, text="원격 제어 (GitHub 페이지)", padding=8)
        remote.grid(row=6, column=0, columnspan=4, sticky="ew")
        ttk.Label(remote, text="GitHub 토큰 (gist 권한)").grid(row=0, column=0, sticky="w")
        self.token_var = tk.StringVar(value=self.cfg.get("token", ""))
        ttk.Entry(remote, textvariable=self.token_var, show="•", width=34).grid(row=1, column=0, sticky="w")
        ttk.Button(remote, text="저장", command=self.save_token).grid(row=1, column=1, padx=(6, 0))
        self.remote_var = tk.StringVar(value="연결 확인 중…")
        self.remote_label = ttk.Label(remote, textvariable=self.remote_var, foreground="gray")
        self.remote_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        link = ttk.Label(remote, text="Gist 보기", foreground="#3a7bd5", cursor="hand2")
        link.grid(row=3, column=0, sticky="w")
        link.bind("<Button-1>", lambda e: self.open_gist())

        opts = ttk.Frame(frm)
        opts.grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.force_var = tk.BooleanVar(value=self.cfg.get("force", False))
        ttk.Checkbutton(opts, text="강제 종료 (저장 안 된 프로그램도 닫음)", variable=self.force_var,
                        command=self.save_options).pack(anchor="w")
        if autostart_supported():
            self.autostart_var = tk.BooleanVar(value=autostart_enabled())
            ttk.Checkbutton(opts, text="윈도우 시작 시 트레이로 자동 실행", variable=self.autostart_var,
                            command=self.toggle_autostart).pack(anchor="w")
        ttk.Label(opts, text="창을 닫아도(X) 트레이에서 계속 실행됩니다.",
                  foreground="gray").pack(anchor="w", pady=(4, 0))

    # ------------------------------------------------------------ 동작

    def schedule_from_inputs(self):
        try:
            h, m = int(self.hour_var.get()), int(self.min_var.get())
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except ValueError:
            messagebox.showerror("입력 오류", "시(0~23)와 분(0~59)을 올바르게 입력하세요.")
            return
        now = datetime.datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        self.schedule(target.timestamp())

    def schedule(self, at):
        self.scheduled_at = at
        self.warned = False
        self.worker.mark_dirty()
        self.refresh_tray()

    def cancel(self):
        self.scheduled_at = None
        self.warned = False
        self.state_label.configure(foreground="")
        self.worker.mark_dirty()
        self.refresh_tray()

    def confirm_shutdown_now(self):
        if messagebox.askyesno("지금 종료", "컴퓨터를 지금 종료할까요?"):
            self.do_shutdown()

    def do_shutdown(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        self.scheduled_at = None
        self.state_var.set("컴퓨터를 종료합니다…")
        self.root.update_idletasks()
        t = threading.Thread(target=self.worker.go_offline, daemon=True)
        t.start()
        t.join(6)
        core.shutdown_now(force=self.force_var.get())

    def save_token(self):
        self.cfg["token"] = self.token_var.get().strip()
        core.save_config(self.cfg)
        self.remote_var.set("연결 확인 중…")
        self.worker.set_token(self.cfg["token"])

    def save_options(self):
        self.cfg["force"] = bool(self.force_var.get())
        core.save_config(self.cfg)

    def toggle_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
        except OSError as e:
            messagebox.showerror("오류", "시작 프로그램 설정 실패: %s" % e)
            self.autostart_var.set(autostart_enabled())

    def open_gist(self):
        url = self.worker.gist_url()
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("Gist", "아직 연결되지 않았습니다. 토큰을 저장하세요.")

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.cfg.get("tray_hint_shown"):
            self.cfg["tray_hint_shown"] = True
            core.save_config(self.cfg)
            try:
                self.tray.notify("트레이에서 계속 실행 중입니다.", "자동 종료 타이머")
            except Exception:
                pass

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))

    def quit(self):
        if self.scheduled_at:
            self.show()
        if self.scheduled_at and not messagebox.askyesno(
                "끝내기", "종료 예약이 있습니다. 프로그램을 끝내면 예약이 취소됩니다. 계속할까요?"):
            return
        self.scheduled_at = None
        self.root.withdraw()
        t = threading.Thread(target=self.worker.go_offline, daemon=True)
        t.start()
        t.join(6)
        self.tray.stop()
        self.closed = True
        self.root.destroy()

    def refresh_tray(self):
        active = self.scheduled_at is not None
        try:
            self.tray.icon = make_icon_image(active)
            if active:
                when = datetime.datetime.fromtimestamp(self.scheduled_at).strftime("%H:%M")
                self.tray.title = "자동 종료 타이머 - %s 종료 예정" % when
            else:
                self.tray.title = "자동 종료 타이머 - 예약 없음"
        except Exception:
            pass

    # ------------------------------------------------------------ 루프

    def handle_events(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "show":
                self.show()
            elif kind == "cancel":
                self.cancel()
            elif kind == "quit":
                self.quit()
                return
            elif kind == "remote_status":
                _, text, ok = ev
                self.remote_var.set(text)
                self.remote_label.configure(foreground="#2e9d4f" if ok else "#c0392b")
            elif kind == "remote":
                _, action, at, cmd_id = ev
                self.cfg["last_command_id"] = cmd_id
                core.save_config(self.cfg)
                if action == "schedule":
                    self.schedule(at)
                elif action == "cancel":
                    self.cancel()
                elif action == "shutdown_now":
                    self.do_shutdown()

    def tick(self):
        self.handle_events()
        if self.closed:
            return
        now = time.time()
        self.clock_var.set(datetime.datetime.now().strftime("%H:%M:%S"))
        if self.scheduled_at and not self.shutting_down:
            remaining = self.scheduled_at - now
            when = datetime.datetime.fromtimestamp(self.scheduled_at)
            day = "" if when.date() == datetime.date.today() else "내일 "
            self.state_var.set("%s%s 종료 예정 (남은 시간 %s)" % (day, when.strftime("%H:%M"), fmt_remaining(remaining)))
            if remaining <= WARNING_SECONDS and not self.warned:
                self.warned = True
                self.state_label.configure(foreground="#c0392b")
                self.show()
            if remaining <= 0:
                self.do_shutdown()
        elif not self.shutting_down:
            self.state_var.set("예약 없음")
        self.root.after(500, self.tick)

    def run(self):
        self.root.mainloop()


def main():
    App(start_minimized="--minimized" in sys.argv).run()


if __name__ == "__main__":
    main()
