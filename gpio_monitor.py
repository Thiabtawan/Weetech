from collections import deque
import configparser
import subprocess
import sys
import time
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

    mode = gi("APP", "mode", 2)
    if mode not in (1, 2):
        mode = 2

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

        # โหมด 1 = ใช้ Pin5 เป็น Interlock / โหมด 2 = ไม่สน Pin5 (ส่งผลตามเวลา)
        "mode": mode,

        # โหมด 2: ส่ง PASS/FAIL ค้างไว้กี่วินาที แล้วหยุดส่ง
        "result_hold_s": max(
            0.1, cfg.getfloat("APP", "result_hold_sec", fallback=5.0)
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

        self.title(f'GPIO Monitor - Mode {s["mode"]}')
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
        self.dismissed = {"pass": False, "fail": False}
        self.result_since = {"pass": None, "fail": None}

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

    def make_lamp(self, cx, cy, r, color, base):
        """สร้างหลอดไฟ 1 ดวง: ฮาโล 2 ชั้น + ขอบ + แก้วหลอด + ประกายสะท้อน"""

        cv = self.cv
        s = self.s

        halo2 = cv.create_oval(
            cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10,
            fill=base, outline=""
        )

        halo1 = cv.create_oval(
            cx - r - 6, cy - r - 6, cx + r + 6, cy + r + 6,
            fill=base, outline=""
        )

        bezel = cv.create_oval(
            cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3,
            fill=blend(s["off"], base, 0.6),
            outline=blend(s["muted"], base, 0.35)
        )

        dot = cv.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=s["off"], outline=""
        )

        hl = cv.create_oval(
            cx - 0.58 * r, cy - 0.72 * r,
            cx + 0.08 * r, cy - 0.30 * r,
            fill=s["off"], outline=""
        )

        lamp = {
            "color": color,
            "base": base,
            "halo2": halo2,
            "halo1": halo1,
            "bezel": bezel,
            "dot": dot,
            "hl": hl,
        }

        self.paint_lamp(lamp, False)

        return lamp

    def paint_lamp(self, lamp, on):

        cv = self.cv
        s = self.s

        color = lamp["color"]
        base = lamp["base"]

        unlit = blend(color, s["off"], 0.20)

        cv.itemconfig(
            lamp["halo2"],
            fill=blend(color, base, 0.12) if on else base
        )

        cv.itemconfig(
            lamp["halo1"],
            fill=blend(color, base, 0.28) if on else base
        )

        cv.itemconfig(
            lamp["bezel"],
            outline=blend(color, base, 0.55) if on
            else blend(s["muted"], base, 0.35)
        )

        cv.itemconfig(
            lamp["dot"],
            fill=color if on else unlit
        )

        cv.itemconfig(
            lamp["hl"],
            fill=blend("#FFFFFF", color if on else unlit, 0.55)
        )

    def build(self):

        s = self.s
        W, H = s["width"], s["height"]
        bg = s["background"]
        surface = s["surface"]
        border = blend(s["off"], surface, 0.5)

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

        # ---------------------------------------------
        # มุมบนขวา: หลอด READY (Pin5) ไม่มีข้อความ
        # ---------------------------------------------

        rcx, rcy, rr = W - mx - 12, 19, 6

        self.ready_lamp = self.make_lamp(
            rcx, rcy, rr, s["ready"], bg
        )

        # ---------------------------------------------
        # ตรงกลางด้านบน: ชื่อโปรแกรม
        # (ตอนหลุดการเชื่อมต่อ จะถูกแทนที่ด้วยป้าย OFFLINE สีแดง)
        # ---------------------------------------------

        self.title_txt = cv.create_text(
            W / 2, 19,
            text="GPIO MONITOR",
            font=("Segoe UI", 10, "bold"),
            fill=s["text"]
        )

        self.badge_bg = rrect(
            cv,
            W / 2 - 42, 8,
            W / 2 + 42, 29,
            9,
            fill=bg,
            outline=""
        )

        self.badge_txt = cv.create_text(
            W / 2, 18,
            text="",
            font=("Segoe UI", 8, "bold"),
            fill=s["muted"]
        )

        # ---------------------------------------------
        # แผงหลอด 3 ดวง: TEST / PASS / FAIL
        # ---------------------------------------------

        pt, pb = 40, H - 8
        ph = pb - pt

        rrect(
            cv,
            mx + 1, pt + 2,
            W - mx + 1, pb + 2,
            10,
            fill=blend(s["text"], bg, 0.06),
            outline=""
        )

        rrect(
            cv,
            mx, pt,
            W - mx, pb,
            10,
            fill=surface,
            outline=border
        )

        cw = (W - 2 * mx) / 3

        r = max(10, min(
            int(cw / 2 - 12),
            int((ph - 24) / 2 - 10)
        ))

        cy = pt + (ph - 24) / 2 + 3
        label_y = pb - 13

        self.lamps = {}
        self.labels = {}

        for i, (key, title) in enumerate((
            ("testing", "TEST"),
            ("pass", "PASS"),
            ("fail", "FAIL")
        )):

            cx = mx + cw * (i + 0.5)

            self.lamps[key] = self.make_lamp(
                cx, cy, r, s[key], surface
            )

            self.labels[key] = cv.create_text(
                cx, label_y,
                text=title,
                fill=s["muted"],
                font=("Segoe UI", 9, "bold")
            )

    # =====================================================
    # LAMP CONTROL
    # =====================================================

    def set_lamp(self, key, on):

        self.paint_lamp(self.lamps[key], on)

        self.cv.itemconfig(
            self.labels[key],
            fill=self.s["text"] if on else self.s["muted"]
        )

    def set_ready(self, on):

        self.paint_lamp(self.ready_lamp, on)

    def set_badge(self, text, fg, fill):

        self.cv.itemconfig(self.title_txt, state="hidden")
        self.cv.itemconfig(self.badge_bg, fill=fill, state="normal")
        self.cv.itemconfig(
            self.badge_txt, text=text, fill=fg, state="normal"
        )

    def hide_badge(self):

        self.cv.itemconfig(self.badge_bg, state="hidden")
        self.cv.itemconfig(self.badge_txt, state="hidden")
        self.cv.itemconfig(self.title_txt, state="normal")

    # =====================================================
    # RENDER STATUS
    # =====================================================

    def render(self):

        s = self.s
        bg = s["background"]

        # ---------------- CONNECTING ----------------
        if self.online is None:

            self.hide_badge()

            for k in self.lamps:
                self.set_lamp(k, False)

            self.set_ready(False)
            return

        # ---------------- OFFLINE ----------------
        if self.online is False:

            self.set_badge(
                "● OFFLINE", s["fail"],
                blend(s["fail"], bg, 0.14)
            )

            for k in self.lamps:
                self.set_lamp(k, False)

            self.set_ready(False)
            return

        # ---------------- ONLINE ----------------
        st = self.pin_state

        self.hide_badge()

        self.set_lamp("testing", st["testing"] and self.blink_on)
        self.set_lamp("fail", st["fail"])
        self.set_ready(st["ready"])

        # ---------------- RECOVERY ANIMATION ----------------
        if self.recovering:

            flash = self.recovery_step % 2 == 0

            self.set_lamp("pass", flash)

        else:

            self.set_lamp("pass", st["pass"])

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
    # DEBOUNCE + LATCH + MODE
    # =====================================================

    def stabilize(self, raw_in):

        s = self.s
        mode1 = s["mode"] == 1
        need = s["off_confirm_polls"]

        raw = dict(raw_in)
        prev = self.pin_state

        # ครั้งแรก / เพิ่งกลับมา Online
        if prev is None:
            prev = {k: False for k in raw}
            self.off_counts = {k: 0 for k in raw}

        # MODE 1: ผลเก่าที่ถูกยกเลิกไปแล้ว (Pin5 หลุด)
        # ห้ามกลับมาโชว์ซ้ำ จนกว่าไฟล์จะเคลียร์ หรือเริ่มเทสใหม่
        if mode1:

            if raw["testing"]:
                self.dismissed = {"pass": False, "fail": False}

            for k in ("pass", "fail"):
                if self.dismissed[k]:
                    if raw[k]:
                        raw[k] = False
                    else:
                        self.dismissed[k] = False

        # Debounce: ดับเมื่ออ่านได้ False ติดกันครบ need รอบเท่านั้น
        out = {}

        for k, v in raw.items():

            if v:
                self.off_counts[k] = 0
                out[k] = True

            else:
                self.off_counts[k] = self.off_counts.get(k, 0) + 1
                out[k] = prev[k] and self.off_counts[k] < need

        # Latch: ค้าง PASS / FAIL ไว้ จนกว่าจะเริ่มเทสใหม่ (TESTING = True)
        if s["latch_result"] and not raw["testing"]:

            for k, other in (("fail", "pass"), ("pass", "fail")):

                if prev[k] and not raw[other]:
                    out[k] = True

        # TESTING กับ PASS/FAIL ต้องไม่ติดพร้อมกัน
        if not raw["testing"] and (out["fail"] or out["pass"]):
            out["testing"] = False

        elif out["testing"]:
            out["fail"] = False
            out["pass"] = False

        # MODE 1: Interlock ด้วย Pin5 (READY)
        # Pin5 = False -> ยกเลิก / รีเซ็ตทุกอย่าง
        if mode1 and not out["ready"]:

            out["testing"] = False
            out["fail"] = False
            out["pass"] = False

            for k in ("pass", "fail"):
                if raw_in[k]:
                    self.dismissed[k] = True

        return out

    # =====================================================
    # OUTPUT STATE (สิ่งที่ส่งไป USB-PIO)
    # =====================================================

    def output_state(self, disp):

        out = dict(disp)

        # MODE 1: ส่งตามที่แสดงทุกอย่าง (ค้างไว้)
        if self.s["mode"] != 2:
            return out

        # MODE 2: ส่ง PASS / FAIL ตามเวลาที่ตั้งไว้ แล้วหยุดส่ง
        # (หน้าจอยังแสดงผลค้างอยู่)
        now = time.monotonic()
        hold = self.s["result_hold_s"]

        for k in ("pass", "fail"):

            if disp[k]:

                if self.result_since[k] is None:
                    self.result_since[k] = now

                out[k] = (now - self.result_since[k]) < hold

            else:
                self.result_since[k] = None

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
            self.sync_usbpio(self.output_state(st))

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
