from collections import deque
import configparser
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox


# =========================================================
# CONFIGURATION
# =========================================================

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "config.ini"


def load_settings():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"ไม่พบไฟล์ตั้งค่า:\n{CONFIG_FILE}")

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(CONFIG_FILE, encoding="utf-8-sig")

    def gs(sec, key, default):
        return cfg.get(sec, key, fallback=default).strip()

    def gi(sec, key, default):
        return cfg.getint(sec, key, fallback=default)

    return {
        "ini_file": Path(gs("APP", "ini_file", r"C:\IniFiles\usbpio.ini")),
        "section": gs("APP", "section", "USB-PIO"),

        "poll_ms": max(50, gi("APP", "poll_ms", 100)),
        "fail_threshold": max(1, gi("APP", "offline_after_fails", 3)),

        "width": gi("APP", "window_width", 260),
        "height": gi("APP", "window_height", 130),

        "always_on_top": cfg.getboolean(
            "APP", "always_on_top", fallback=True
        ),

        "pin_testing": gi("PINS", "testing", 1),
        "pin_fail": gi("PINS", "fail", 2),
        "pin_pass": gi("PINS", "pass", 14),
        "pin_ready": gi("PINS", "ready", 5),

        "background": gs("COLORS", "background", "#F5F7FA"),
        "surface": gs("COLORS", "surface", "#FFFFFF"),
        "text": gs("COLORS", "text", "#20232A"),
        "muted": gs("COLORS", "muted", "#7A8492"),
        "off": gs("COLORS", "off", "#DDE2E8"),

        "testing": gs("COLORS", "testing", "#F5A623"),
        "pass": gs("COLORS", "pass", "#18A558"),
        "fail": gs("COLORS", "fail", "#E53935"),
        "ready": gs("COLORS", "ready", "#33FF05"),

        "blink_ms": max(150, gi("ANIMATION", "blink_ms", 450)),
        "recovery_ms": max(100, gi("ANIMATION", "recovery_blink_ms", 300)),

        "command_dir": Path(
            gs("APP", "command_dir",
               r"C:\ext_prog\USBPIO\bin\x86\Debug")
        ),
        "command_exe": gs("APP", "command_exe", "USBPIO_GUI.exe"),

        "command_delay_ms": max(
            100, gi("APP", "command_delay_ms", 800)
        ),

        "command_check_ms": max(
            50, gi("APP", "command_check_ms", 100)
        ),

        # ต้องอ่านเจอ False ติดกันกี่รอบ ถึงจะยอมดับ (กันอ่านไฟล์ตอน TSL กำลังเขียน)
        "off_confirm_polls": max(
            1, gi("APP", "off_confirm_polls", 3)
        ),

        # ค้าง PASS/FAIL ไว้ จนกว่า TSL จะเริ่มเทสใหม่ (Pin TESTING = True)
        "latch_result": cfg.getboolean(
            "APP", "latch_result", fallback=True
        ),
    }


# =========================================================
# COLOR / DRAWING UTILITIES
# =========================================================

def blend(fg, bg, a):
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]

    return "#%02x%02x%02x" % tuple(
        round(f[i] * a + b[i] * (1 - a))
        for i in range(3)
    )


def rrect(cv, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r,
        x1, y1 + r, x1, y1
    ]

    return cv.create_polygon(pts, smooth=True, **kw)


# =========================================================
# GPIO MINI MONITOR
# =========================================================

class App(tk.Tk):

    def __init__(self, s):
        super().__init__()

        self.s = s

        self.title("GPIO Monitor")
        self.geometry(f'{s["width"]}x{s["height"]}')
        self.resizable(False, False)
        self.configure(bg=s["background"])

        if s["always_on_top"]:
            self.attributes("-topmost", True)

        # Connection state
        # หมายเหตุ: ห้ามใช้ชื่อ self.state เพราะไปทับเมธอด Tk.state()
        self.pin_state = None
        self.online = None
        self.fails = 0
        self.last_error = ""

        # เก็บสถานะล่าสุดที่ส่งคำสั่ง USB-PIO แล้ว
        self.last_command_states = {}

        # Sequential Command Queue
        self.command_queue = deque()
        self.command_process = None
        self.current_command = None
        self.command_job = None

        # Debounce / Latch
        self.off_counts = {}

        # Animation state
        self.blink_on = True
        self.recovering = False
        self.recovery_step = 0
        self.recovery_job = None

        self.build()
        self.render()
        self.poll()
        self.animate()

    # =====================================================
    # SEQUENTIAL USB-PIO COMMAND QUEUE
    # =====================================================

    def sync_usbpio(self, st):

        pin_map = {
            "testing": self.s["pin_testing"],
            "fail": self.s["pin_fail"],
            "pass": self.s["pin_pass"],
            "ready": self.s["pin_ready"]
        }

        for name, pin in pin_map.items():

            desired = st[name]

            # ตรวจสอบสถานะที่กำลังทำงาน
            if self.current_command and self.current_command[0] == pin:
                known = self.current_command[1]
            else:
                known = self.last_command_states.get(pin)

            # ตรวจสอบคำสั่งที่รออยู่ใน Queue
            for queued_pin, queued_state in self.command_queue:
                if queued_pin == pin:
                    known = queued_state

            if known == desired:
                continue

            # ลบคำสั่งเก่าของ Pin เดียวกัน
            self.command_queue = deque(
                (p, v) for p, v in self.command_queue
                if p != pin
            )

            # เพิ่มคำสั่งล่าสุด
            self.command_queue.append((pin, desired))

            print(
                f"[QUEUE] Pin{pin} -> "
                f"{'ON' if desired else 'OFF'}"
            )

        # เริ่ม Queue ถ้ายังไม่มีงานทำ
        self.schedule_next_command(0)

    # =====================================================
    # SCHEDULE NEXT COMMAND
    # =====================================================

    def schedule_next_command(self, delay):

        if self.command_job is not None:
            return

        if self.command_process is not None:
            return

        if not self.command_queue:
            return

        self.command_job = self.after(
            delay,
            self.run_next_command
        )

    # =====================================================
    # EXECUTE ONE COMMAND
    # =====================================================

    def run_next_command(self):

        self.command_job = None

        if self.command_process is not None:
            return

        if not self.command_queue:
            return

        pin, state = self.command_queue.popleft()

        work_dir = self.s["command_dir"]
        exe_path = work_dir / self.s["command_exe"]

        action = "ON" if state else "OFF"

        if not work_dir.is_dir() or not exe_path.is_file():

            print(f"[COMMAND ERROR] EXE not found: {exe_path}")

            self.schedule_next_command(
                self.s["command_delay_ms"]
            )
            return

        try:

            # เรียก exe ตรงๆ แบบ list ไม่ผ่าน cmd.exe
            # เพื่อเลี่ยงปัญหา quote / path ที่มีช่องว่าง
            self.command_process = subprocess.Popen(
                [str(exe_path), str(pin), action],
                cwd=str(work_dir),
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            self.current_command = (pin, state)

            # บันทึกว่าคำสั่งนี้ถูกส่งแล้ว
            self.last_command_states[pin] = state

            print(f"[RUNNING] Pin{pin} -> {action}")

            # ตรวจสอบ Process
            self.check_command_process()

        except OSError as e:

            print(f"[COMMAND ERROR] {e}")

            self.command_process = None
            self.current_command = None

            self.schedule_next_command(
                self.s["command_delay_ms"]
            )

    # =====================================================
    # WAIT UNTIL PROCESS FINISH
    # =====================================================

    def check_command_process(self):

        if self.command_process is None:
            return

        return_code = self.command_process.poll()

        # EXE ยังทำงานอยู่
        if return_code is None:

            self.after(
                self.s["command_check_ms"],
                self.check_command_process
            )
            return

        # EXE ทำงานเสร็จแล้ว
        pin, state = self.current_command

        if return_code == 0:

            print(
                f"[COMPLETED] Pin{pin} -> "
                f"{'ON' if state else 'OFF'}"
            )

        else:

            print(
                f"[COMMAND FAILED] Pin{pin}, "
                f"Exit Code: {return_code}"
            )

        self.command_process = None
        self.current_command = None

        # พักก่อนส่งคำสั่งถัดไป
        self.schedule_next_command(
            self.s["command_delay_ms"]
        )

    # =====================================================
    # BUILD MINI UI
    # =====================================================

    def build(self):

        s = self.s
        W, H = s["width"], s["height"]
        bg = s["background"]

        cv = tk.Canvas(
            self,
            width=W,
            height=H,
            bg=bg,
            highlightthickness=0
        )

        cv.pack()
        self.cv = cv

        mx = 12

        # Header
        cv.create_text(
            mx, 18,
            text="GPIO MONITOR",
            anchor="w",
            font=("Segoe UI", 10, "bold"),
            fill=s["text"]
        )

        # Status badge
        self.badge_bg = rrect(
            cv,
            W - mx - 83, 8,
            W - mx, 29,
            9,
            fill=bg,
            outline=""
        )

        self.badge_txt = cv.create_text(
            W - mx - 41,
            18,
            text="",
            font=("Segoe UI", 8, "bold"),
            fill=s["muted"]
        )

        # Main status
        self.state_txt = cv.create_text(
            W / 2,
            60,
            text="WAIT...",
            fill=s["muted"],
            font=("Segoe UI", 25, "bold")
        )

        # Status cards
        gap = 7
        top = 88
        card_h = 34

        cw = (W - 2 * mx - 2 * gap) / 3

        border = blend(s["off"], s["surface"], 0.5)

        self.lamps = {}

        for i, (key, title) in enumerate((
            ("testing", "TEST"),
            ("pass", "PASS"),
            ("fail", "FAIL")
        )):

            x1 = mx + i * (cw + gap)
            x2 = x1 + cw
            cx = (x1 + x2) / 2

            # Card shadow
            rrect(
                cv,
                x1 + 1, top + 2,
                x2 + 1, top + card_h + 2,
                7,
                fill=blend(s["text"], bg, 0.06),
                outline=""
            )

            # Card
            rrect(
                cv,
                x1, top,
                x2, top + card_h,
                7,
                fill=s["surface"],
                outline=border
            )

            # LED
            dot = cv.create_oval(
                cx - 25, top + 12,
                cx - 15, top + 22,
                fill=s["off"],
                outline=""
            )

            # Label
            name = cv.create_text(
                cx - 9,
                top + 17,
                text=title,
                anchor="w",
                fill=s["muted"],
                font=("Segoe UI", 9, "bold")
            )

            self.lamps[key] = (None, dot, name, s[key])

    # =====================================================
    # LED CONTROL
    # =====================================================

    def set_lamp(self, key, on):

        glow, dot, name, color = self.lamps[key]
        s = self.s

        self.cv.itemconfig(
            dot,
            fill=color if on else s["off"]
        )

        if glow:
            self.cv.itemconfig(
                glow,
                fill=blend(color, s["surface"], 0.28)
                if on else s["surface"]
            )

        self.cv.itemconfig(
            name,
            fill=s["text"] if on else s["muted"]
        )

    # =====================================================
    # RENDER STATUS
    # =====================================================

    def render(self):

        s = self.s
        cv = self.cv
        bg = s["background"]

        # ---------------- CONNECTING ----------------
        if self.online is None:

            cv.itemconfig(
                self.badge_bg,
                fill=blend(s["muted"], bg, 0.15)
            )
            cv.itemconfig(
                self.badge_txt,
                text="● ...",
                fill=s["muted"]
            )
            cv.itemconfig(
                self.state_txt,
                text="WAIT...",
                fill=s["muted"],
                font=("Segoe UI", 23, "bold")
            )

            for k in self.lamps:
                self.set_lamp(k, False)

            return

        # ---------------- OFFLINE ----------------
        if self.online is False:

            cv.itemconfig(
                self.badge_bg,
                fill=blend(s["fail"], bg, 0.14)
            )
            cv.itemconfig(
                self.badge_txt,
                text="● OFFLINE",
                fill=s["fail"]
            )
            cv.itemconfig(
                self.state_txt,
                text="OFFLINE",
                fill=s["fail"],
                font=("Segoe UI", 25, "bold")
            )

            for k in self.lamps:
                self.set_lamp(k, False)

            return

        # ---------------- ONLINE ----------------
        st = self.pin_state

        if st["fail"]:
            text = "FAIL"
            color = s["fail"]

        elif st["pass"]:
            text = "PASS"
            color = s["pass"]

        elif st["testing"]:
            text = "TESTING"
            color = s["testing"]

        else:
            text = "IDLE"
            color = s["muted"]

        cv.itemconfig(
            self.state_txt,
            text=text,
            fill=color,
            font=("Segoe UI", 25, "bold")
        )

        # TESTING blinking
        self.set_lamp("testing", st["testing"] and self.blink_on)

        # FAIL LED
        self.set_lamp("fail", st["fail"])

        # ---------------- RECOVERY ANIMATION ----------------
        if self.recovering:

            flash = self.recovery_step % 2 == 0

            self.set_lamp("pass", flash)

            cv.itemconfig(
                self.badge_bg,
                fill=s["pass"] if flash
                else blend(s["pass"], bg, 0.14)
            )
            cv.itemconfig(
                self.badge_txt,
                text="● LIVE",
                fill="#FFFFFF" if flash else s["pass"]
            )

        else:

            self.set_lamp("pass", st["pass"])

            # READY / STANDBY
            if st["ready"]:

                cv.itemconfig(
                    self.badge_bg,
                    fill=blend(s["ready"], bg, 0.15)
                )
                cv.itemconfig(
                    self.badge_txt,
                    text="● READY",
                    fill=s["ready"]
                )

            else:

                cv.itemconfig(
                    self.badge_bg,
                    fill=blend(s["muted"], bg, 0.15)
                )
                cv.itemconfig(
                    self.badge_txt,
                    text="● STANDBY",
                    fill=s["muted"]
                )

    # =====================================================
    # READ USB-PIO INI
    # =====================================================

    def read_state(self):

        try:
            raw = self.s["ini_file"].read_bytes()

        except OSError as e:
            self.last_error = f"Cannot read file: {e}"
            return None

        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16", errors="replace")

        else:
            try:
                text = raw.decode("utf-8-sig")

            except UnicodeDecodeError:
                text = raw.decode("cp874", errors="replace")

        cfg = configparser.ConfigParser(
            interpolation=None,
            strict=False,
            inline_comment_prefixes=(";", "#")
        )

        try:
            cfg.read_string(text)

        except configparser.Error as e:
            self.last_error = f"Bad INI format: {e}"
            return None

        want = self.s["section"].lower()

        sec = next(
            (x for x in cfg.sections() if x.lower() == want),
            None
        )

        if sec is None:

            found = ", ".join(cfg.sections()) or "none"

            self.last_error = (
                f'Section [{self.s["section"]}] not found '
                f'(found: {found})'
            )

            return None

        def pin(n):

            v = cfg.get(
                sec,
                f"Pin{n}",
                fallback="False"
            ).strip().lower()

            return v in ("true", "1", "yes", "on")

        return {
            "testing": pin(self.s["pin_testing"]),
            "pass": pin(self.s["pin_pass"]),
            "fail": pin(self.s["pin_fail"]),
            "ready": pin(self.s["pin_ready"])
        }

    # =====================================================
    # DEBOUNCE + LATCH
    # =====================================================

    def stabilize(self, raw):

        prev = self.pin_state

        # ครั้งแรก / เพิ่งกลับมา Online: ใช้ค่าที่อ่านได้เลย
        if prev is None:
            self.off_counts = {k: 0 for k in raw}
            return dict(raw)

        need = self.s["off_confirm_polls"]
        out = {}

        for k, v in raw.items():

            if v:
                self.off_counts[k] = 0
                out[k] = True

            else:
                # ดับเมื่ออ่านได้ False ติดกันครบ need รอบเท่านั้น
                self.off_counts[k] = self.off_counts.get(k, 0) + 1
                out[k] = prev[k] and self.off_counts[k] < need

        # ค้าง PASS / FAIL ไว้ จนกว่าจะเริ่มเทสใหม่ (TESTING = True)
        if self.s["latch_result"] and not raw["testing"]:

            for k, other in (("fail", "pass"), ("pass", "fail")):

                if prev[k] and not raw[other]:
                    out[k] = True

        # TESTING กับ PASS/FAIL ต้องไม่ติดพร้อมกัน
        if not raw["testing"] and (out["fail"] or out["pass"]):
            # เทสจบแล้ว: ดับ TESTING ทันที แล้วโชว์ผล PASS/FAIL
            out["testing"] = False

        elif out["testing"]:
            # กำลังเทสอยู่: ซ่อนผลเก่าที่ยังค้างในไฟล์ไว้ก่อน
            out["fail"] = False
            out["pass"] = False

        return out

    # =====================================================
    # AUTO RECONNECT + AUTO COMMAND
    # =====================================================

    def poll(self):

        st = self.read_state()

        if st is not None:

            self.fails = 0

            # OFFLINE -> ONLINE
            if self.online is False:
                self.start_recovery()

            st = self.stabilize(st)

            self.online = True
            self.pin_state = st

            # อ่านค่า INI แล้วสั่ง USB-PIO
            self.sync_usbpio(st)

        else:

            self.fails += 1

            if (
                self.fails >= self.s["fail_threshold"]
                and self.online is not False
            ):

                self.online = False
                self.pin_state = None

                self.stop_recovery()

                # ล้างคำสั่งที่ยังไม่ได้เริ่มทำงาน
                self.command_queue.clear()

                # Sync ใหม่เมื่อกลับมา ONLINE
                self.last_command_states.clear()

        self.render()

        self.after(self.s["poll_ms"], self.poll)

    # =====================================================
    # RECOVERY: GREEN BLINK x2
    # =====================================================

    def start_recovery(self):

        self.stop_recovery()

        self.recovering = True
        self.recovery_step = 0

        self.recovery_job = self.after(
            self.s["recovery_ms"],
            self.recovery_tick
        )

    def recovery_tick(self):

        self.recovery_step += 1

        if self.recovery_step >= 4:

            self.recovering = False
            self.recovery_job = None

        else:

            self.recovery_job = self.after(
                self.s["recovery_ms"],
                self.recovery_tick
            )

        self.render()

    def stop_recovery(self):

        if self.recovery_job is not None:
            self.after_cancel(self.recovery_job)

        self.recovery_job = None
        self.recovering = False

    # =====================================================
    # TESTING BLINK ANIMATION
    # =====================================================

    def animate(self):

        self.blink_on = not self.blink_on

        self.render()

        self.after(self.s["blink_ms"], self.animate)


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    try:
        App(load_settings()).mainloop()

    except Exception as error:

        root = tk.Tk()
        root.withdraw()

        messagebox.showerror("GPIO Monitor", str(error))

        root.destroy()
