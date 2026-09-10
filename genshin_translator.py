"""Screen-only Genshin English subtitle translator for Windows."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import mss
from PIL import Image, ImageDraw, ImageOps
import pytesseract
import pystray
import requests
import tkinter as tk


# A PyInstaller one-file executable extracts bundled data into a temporary
# directory. User configuration and logs belong beside the executable instead.
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
CONFIG_PATH = APP_DIR / "config.json"
EXAMPLE_CONFIG_PATH = BUNDLE_DIR / "config.example.json"
LOG_PATH = APP_DIR / "genshin_translator.log"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def show_error(message: str) -> None:
    """Keep startup failures visible when launched through an elevated script."""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "Genshin Subtitle Translator", 0x10)
    except OSError:
        pass


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        raise RuntimeError(
            f"Created {CONFIG_PATH.name}. Configure its API endpoint/model and set its API key environment variable."
        )
    try:
        # Windows PowerShell can save UTF-8 JSON with a BOM. utf-8-sig accepts
        # both that form and ordinary UTF-8 without leaking an invisible byte
        # into json.loads.
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid JSON in {CONFIG_PATH.name}: {error}") from error

    for section in ("api", "capture", "overlay"):
        if section not in config or not isinstance(config[section], dict):
            raise RuntimeError(f"Missing object '{section}' in {CONFIG_PATH.name}")
    return config


@dataclass(frozen=True)
class ScreenBox:
    left: int
    top: int
    width: int
    height: int


class SubtitleOcr:
    def __init__(self, capture_config: dict[str, Any]) -> None:
        configured_path = str(capture_config.get("tesseract_path", "")).strip()
        if configured_path:
            executable = Path(configured_path)
            if not executable.is_absolute():
                executable = APP_DIR / executable
            if not executable.is_file():
                raise RuntimeError(f"Configured tesseract_path does not exist: {executable}")
            pytesseract.pytesseract.tesseract_cmd = str(executable)
        try:
            pytesseract.get_tesseract_version()
        except (pytesseract.TesseractNotFoundError, OSError) as error:
            raise RuntimeError(
                "Tesseract OCR was not found. Install UB-Mannheim.TesseractOCR or set capture.tesseract_path."
            ) from error

    @staticmethod
    def _prepare(image: Image.Image) -> Image.Image:
        # Subtitle glyphs are small and light on a busy scene. Enlarging and
        # retaining only bright glyphs is more reliable than broad contrast
        # boosting, which also amplifies scene detail and subtitle outlines.
        image = ImageOps.grayscale(image)
        image = image.resize((image.width * 3, image.height * 3), Image.Resampling.LANCZOS)
        return image.point(lambda pixel: 255 if pixel >= 170 else 0)

    def read(self, image: Image.Image) -> str:
        prepared = self._prepare(image)
        # Dialogue can wrap onto multiple lines. A single block pass works for
        # one-line subtitles too, and avoids starting Tesseract a second time.
        raw = pytesseract.image_to_string(
            prepared,
            lang="eng",
            config="--oem 3 --psm 6",
        )
        return self._normalise(raw)

    @staticmethod
    def _normalise(raw: str) -> str:
        text = re.sub(r"\s+", " ", raw).strip()
        text = text.replace("“", "").replace("”", "")
        text = re.sub(r"[\s*_~]+$", "", text).strip()
        text = text.replace("|", "I")
        if len(text) < 3 or not re.search(r"[A-Za-z]", text):
            return ""
        # Decorative horizontal rules can be misread as punctuation-only text.
        if sum(character.isalpha() for character in text) < 2:
            return ""
        return text


class OpenAiCompatibleTranslator:
    def __init__(self, api_config: dict[str, Any], glossary: dict[str, str]) -> None:
        self.url = str(api_config.get("chat_completions_url", "")).strip()
        self.model = str(api_config.get("model", "")).strip()
        self.timeout = float(api_config.get("timeout_seconds", 20))
        try:
            self.max_tokens = int(api_config.get("max_tokens", 96))
        except (TypeError, ValueError) as error:
            raise RuntimeError("api.max_tokens must be an integer") from error
        env_name = str(api_config.get("api_key_env", "GENSHIN_TRANSLATOR_API_KEY")).strip()
        self.api_key = os.environ.get(env_name, "").strip()
        self.glossary = {str(key): str(value) for key, value in glossary.items()}

        if not self.url or not self.model:
            raise RuntimeError("Set api.chat_completions_url and api.model in config.json")
        if "your-provider.example" in self.url or self.model == "replace-with-your-cheap-model":
            raise RuntimeError("Replace the API URL and model placeholder values in config.json before starting the translator")
        parsed_url = urlsplit(self.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise RuntimeError("api.chat_completions_url must be an HTTP(S) URL")
        if parsed_url.path in {"", "/"}:
            self.url = self.url.rstrip("/") + "/v1/chat/completions"
        if not self.api_key:
            raise RuntimeError(f"Environment variable {env_name} is not set")
        if not 16 <= self.max_tokens <= 300:
            raise RuntimeError("api.max_tokens must be between 16 and 300")
        # Reusing the session also reuses a live HTTPS connection when the API
        # service permits it, avoiding a full TCP/TLS setup per subtitle.
        self.session = requests.Session()
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def translate(self, source: str) -> str:
        terms = "\n".join(f"- {english}: {chinese}" for english, chinese in self.glossary.items())
        system_prompt = (
            "Translate English dialogue from Genshin Impact into natural Simplified Chinese. "
            "Return only the Chinese subtitle, with no quote marks, notes, labels, or explanation. "
            "Use official Genshin Impact Chinese terminology whenever it is known. "
            "Translate the meaning of the whole line in context rather than word by word. "
            "Prioritize natural Chinese dialogue, the speaker's tone, and the intended dramatic effect; "
            "do not use stiff literal calques or invent story context that is absent from the source."
        )
        if terms:
            system_prompt += f"\nPreferred terminology:\n{terms}"
        payload = {
            "model": self.model,
            "temperature": 0.15,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": source},
            ],
        }
        response = self.session.post(
            self.url,
            headers=self.headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise RuntimeError("API response did not contain choices[0].message.content") from error
        result = str(content).strip().strip('"').strip()
        if not result:
            raise RuntimeError("API returned an empty translation")
        return result

    def close(self) -> None:
        self.session.close()


class SubtitleOverlay:
    """A transparent, click-through fullscreen Tk window for the Chinese line."""

    GWL_EXSTYLE = -20
    GWLP_WNDPROC = -4
    GA_ROOT = 2
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    WM_NCHITTEST = 0x0084
    WM_HOTKEY = 0x0312
    HTTRANSPARENT = -1
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    VK_T = 0x54
    VK_Q = 0x51
    HOTKEY_START = 1
    HOTKEY_PAUSE = 2
    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    LONG_PTR = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LONG_PTR, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    def __init__(self, monitor: dict[str, int], overlay_config: dict[str, Any]) -> None:
        self.left = int(monitor["left"])
        self.top = int(monitor["top"])
        self.width = int(monitor["width"])
        self.height = int(monitor["height"])
        self.text_y = int(self.height * float(overlay_config.get("top", 0.935)))
        self.max_chars = max(28, int(overlay_config.get("max_chars_per_line", 76)))
        self.base_font_size = max(14, int(overlay_config.get("font_size", 20)))
        self.min_font_size = max(12, min(self.base_font_size, int(overlay_config.get("min_font_size", 15))))
        self.max_text_width = int(self.width * min(0.92, max(0.4, float(overlay_config.get("max_width_ratio", 0.74)))))
        self.transparent_colour = "#ff00ff"

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.configure(bg=self.transparent_colour)
        self.root.wm_attributes("-topmost", True)
        self.root.wm_attributes("-transparentcolor", self.transparent_colour)
        self.root.geometry(f"{self.width}x{self.height}{self.left:+d}{self.top:+d}")
        self.canvas = tk.Canvas(
            self.root,
            bg=self.transparent_colour,
            highlightthickness=0,
            bd=0,
            width=self.width,
            height=self.height,
        )
        self.canvas.pack(fill="both", expand=True)
        self._text_ids: list[int] = []
        self._status_ids: list[int] = []
        self._clear_status_after: str | None = None
        self._hotkeys: dict[int, Callable[[], None]] = {}
        self._closed = False
        self._user32 = ctypes.windll.user32
        self._hwnd = 0
        self._original_wndproc = 0
        self._wndproc_callback = self.WNDPROC(self._window_proc)
        self._make_click_through()

    def _make_click_through(self) -> None:
        self.root.update_idletasks()
        self._user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self._user32.GetAncestor.restype = wintypes.HWND
        self._hwnd = self._user32.GetAncestor(self.root.winfo_id(), self.GA_ROOT)
        if not self._hwnd:
            raise RuntimeError("Could not get the overlay's top-level window handle")

        self._user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.GetWindowLongPtrW.restype = self.LONG_PTR
        self._user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, self.LONG_PTR]
        self._user32.SetWindowLongPtrW.restype = self.LONG_PTR
        self._user32.CallWindowProcW.argtypes = [self.LONG_PTR, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.CallWindowProcW.restype = self.LONG_PTR

        current = self._user32.GetWindowLongPtrW(self._hwnd, self.GWL_EXSTYLE)
        self._user32.SetWindowLongPtrW(
            self._hwnd,
            self.GWL_EXSTYLE,
            current | self.WS_EX_LAYERED | self.WS_EX_TRANSPARENT | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE,
        )
        self._original_wndproc = self._user32.SetWindowLongPtrW(
            self._hwnd,
            self.GWLP_WNDPROC,
            self.LONG_PTR(ctypes.cast(self._wndproc_callback, ctypes.c_void_p).value),
        )
        if not self._original_wndproc:
            raise RuntimeError("Could not install the overlay input pass-through handler")
        self._user32.SetWindowPos(
            self._hwnd,
            self.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE,
        )

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == self.WM_NCHITTEST:
            # This is the decisive mouse pass-through behavior. The desktop
            # window manager sends the click to the game below this overlay.
            return self.HTTRANSPARENT
        if message == self.WM_HOTKEY:
            callback = self._hotkeys.get(int(wparam))
            if callback is not None:
                try:
                    # Hotkey callbacks enqueue work only. Do not call Tk from
                    # this ctypes window procedure: doing so can re-enter Tcl
                    # while it is dispatching the native message.
                    callback()
                except Exception:
                    logging.exception("Global hotkey callback failed")
                return 0
        return self._user32.CallWindowProcW(self._original_wndproc, hwnd, message, wparam, lparam)

    def register_hotkeys(self, start: Callable[[], None], pause: Callable[[], None]) -> None:
        hotkeys = {
            self.HOTKEY_START: (self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT, self.VK_T, start),
            self.HOTKEY_PAUSE: (self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT, self.VK_Q, pause),
        }
        for hotkey_id, (modifiers, key, callback) in hotkeys.items():
            if not self._user32.RegisterHotKey(self._hwnd, hotkey_id, modifiers, key):
                self.unregister_hotkeys()
                raise RuntimeError(f"Could not register global hotkey id {hotkey_id}")
            self._hotkeys[hotkey_id] = callback

    def unregister_hotkeys(self) -> None:
        for hotkey_id in tuple(self._hotkeys):
            self._user32.UnregisterHotKey(self._hwnd, hotkey_id)
        self._hotkeys.clear()

    def _restore_window_proc(self) -> None:
        if self._hwnd and self._original_wndproc:
            self._user32.SetWindowLongPtrW(self._hwnd, self.GWLP_WNDPROC, self._original_wndproc)
            self._original_wndproc = 0

    def set_text(self, text: str) -> None:
        for text_id in self._text_ids:
            self.canvas.delete(text_id)
        self._text_ids.clear()
        if not text:
            return

        wrapped, font = self._layout_text(text)
        x = self.width // 2
        y = self.text_y
        # A five-point outline keeps the translation legible without putting a
        # visible panel over the game scene.
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1)):
            self._text_ids.append(
                self.canvas.create_text(x + dx, y + dy, text=wrapped, anchor="n", fill="#121212", font=font, justify="center")
            )
        self._text_ids.append(
            self.canvas.create_text(x, y, text=wrapped, anchor="n", fill="#f8f6ee", font=font, justify="center")
        )

    def _layout_text(self, text: str) -> tuple[str, tuple[str, int, str]]:
        text = text.replace("\n", "").strip()
        estimated_size = self.max_text_width // max(1, len(text))
        font_size = max(self.min_font_size, min(self.base_font_size, estimated_size))
        max_chars = min(self.max_chars, max(18, self.max_text_width // font_size))
        return self._wrap(text, max_chars), ("Microsoft YaHei UI", font_size, "bold")

    def show_status(self, text: str) -> None:
        for text_id in self._status_ids:
            self.canvas.delete(text_id)
        self._status_ids.clear()
        if self._clear_status_after is not None:
            self.root.after_cancel(self._clear_status_after)
        x = self.width // 2
        y = max(24, int(self.height * 0.07))
        status_font = ("Microsoft YaHei UI", 15, "bold")
        self._status_ids.append(self.canvas.create_text(x + 1, y + 1, text=text, anchor="n", fill="#101010", font=status_font))
        self._status_ids.append(self.canvas.create_text(x, y, text=text, anchor="n", fill="#f8f6ee", font=status_font))
        self._clear_status_after = self.root.after(1200, self._clear_status)

    def _clear_status(self) -> None:
        for text_id in self._status_ids:
            self.canvas.delete(text_id)
        self._status_ids.clear()
        self._clear_status_after = None

    def _wrap(self, text: str, max_chars: int) -> str:
        lines: list[str] = []
        current = ""
        for character in text:
            if len(current) >= max_chars and character not in "，。！？；：、）】』”":
                lines.append(current)
                current = ""
            current += character
        if current:
            lines.append(current)
        return "\n".join(lines)

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._clear_status_after is not None:
            self.root.after_cancel(self._clear_status_after)
        self.unregister_hotkeys()
        self._restore_window_proc()
        self.root.destroy()


class TrayController:
    """Windows tray presence and controls, kept separate from Tk's UI thread."""

    def __init__(self, request_toggle: Callable[[], None], request_stop: Callable[[], None]) -> None:
        self._request_toggle = request_toggle
        self._request_stop = request_stop
        self._enabled = False
        self._state_lock = threading.RLock()
        self._icon = pystray.Icon(
            "genshin_subtitle_translator",
            self._make_icon(False),
            "Genshin Translator: paused",
            pystray.Menu(
                pystray.MenuItem(self._state_label, None, enabled=False),
                pystray.MenuItem("Toggle translation", self._toggle),
                pystray.MenuItem("Exit translator", self._stop),
            ),
        )

    @staticmethod
    def _make_icon(enabled: bool) -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((5, 5, 59, 59), radius=12, fill=(26, 31, 40, 255))
        colour = (71, 190, 111, 255) if enabled else (136, 145, 160, 255)
        draw.ellipse((17, 17, 47, 47), fill=colour)
        return image

    def start(self) -> None:
        self._icon.run_detached()
        try:
            self._icon.notify("Ctrl+Shift+T starts translation; press T to capture one subtitle.", "Genshin Subtitle Translator")
        except Exception:
            logging.debug("Tray notification is unavailable", exc_info=True)

    def set_enabled(self, enabled: bool) -> None:
        with self._state_lock:
            self._enabled = enabled
            self._icon.icon = self._make_icon(enabled)
            self._icon.title = "Genshin Translator: ON" if enabled else "Genshin Translator: paused"
            self._icon.update_menu()

    def stop(self) -> None:
        with self._state_lock:
            self._icon.stop()

    def _state_label(self, _item: pystray.MenuItem) -> str:
        with self._state_lock:
            return "Translation: ON" if self._enabled else "Translation: OFF"

    def _toggle(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._request_toggle()

    def _stop(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self._request_stop()


class InputTriggerListener:
    """Observe the manual T trigger without ever consuming keyboard input."""

    WH_KEYBOARD_LL = 13
    HC_ACTION = 0
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_QUIT = 0x0012
    VK_T = 0x54
    VK_SHIFT = 0x10
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C
    LRESULT = ctypes.c_ssize_t
    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    def __init__(self, on_trigger: Callable[[], None]) -> None:
        self._on_trigger = on_trigger
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._thread_lock = threading.Lock()
        self._started = threading.Event()
        self._stop = threading.Event()
        self._startup_error: Exception | None = None
        self._keyboard_hook = 0
        self._t_is_down = False
        # ctypes callbacks must remain referenced for the full hook lifetime.
        self._keyboard_callback = self.HOOKPROC(self._keyboard_proc)
        self._configure_winapi()

    def _configure_winapi(self) -> None:
        self._user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            self.HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        self._user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self._user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.CallNextHookEx.restype = self.LRESULT
        self._user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="input-trigger-listener", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=3.0):
            self.stop()
            raise RuntimeError("Timed out while starting the input trigger listener")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            raise RuntimeError(f"Could not start the input trigger listener: {error}") from error

    def stop(self) -> None:
        self._stop.set()
        with self._thread_lock:
            thread_id = self._thread_id
        if thread_id:
            self._user32.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        message = wintypes.MSG()
        try:
            # Create this thread's message queue before stop() might need to
            # post WM_QUIT to it.
            self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            with self._thread_lock:
                self._thread_id = self._kernel32.GetCurrentThreadId()

            self._keyboard_hook = self._user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._keyboard_callback, None, 0)
            if not self._keyboard_hook:
                raise RuntimeError(f"SetWindowsHookExW(keyboard) failed with error {ctypes.get_last_error()}")

            self._started.set()
            while not self._stop.is_set():
                result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
        except Exception as error:
            self._startup_error = error
            logging.exception("Input trigger listener stopped unexpectedly")
        finally:
            if self._keyboard_hook:
                self._user32.UnhookWindowsHookEx(self._keyboard_hook)
                self._keyboard_hook = 0
            with self._thread_lock:
                self._thread_id = 0
            self._started.set()

    def _keyboard_proc(self, code: int, wparam: int, lparam: int) -> int:
        try:
            if code == self.HC_ACTION:
                key = ctypes.cast(lparam, ctypes.POINTER(self.KBDLLHOOKSTRUCT)).contents
                if key.vkCode == self.VK_T:
                    if wparam in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                        # Holding T emits repeated WM_KEYDOWN messages. Treat
                        # that as one manual translation request.
                        if not self._t_is_down and not self._modifier_is_down():
                            self._t_is_down = True
                            self._on_trigger()
                    elif wparam in (self.WM_KEYUP, self.WM_SYSKEYUP):
                        self._t_is_down = False
        except Exception:
            logging.exception("T-key trigger callback failed")
        # This hook is observational only. It must never consume a keystroke.
        return self._user32.CallNextHookEx(self._keyboard_hook, code, wparam, lparam)

    def _modifier_is_down(self) -> bool:
        return any(
            self._user32.GetAsyncKeyState(key) & 0x8000
            for key in (self.VK_SHIFT, self.VK_CONTROL, self.VK_MENU, self.VK_LWIN, self.VK_RWIN)
        )


class TranslatorApp:
    def __init__(self, config: dict[str, Any], preview: bool = False) -> None:
        capture_config = config["capture"]
        monitor_index = int(capture_config.get("monitor_index", 1))
        with mss.MSS() as screenshotter:
            if monitor_index < 1 or monitor_index >= len(screenshotter.monitors):
                raise RuntimeError(f"capture.monitor_index must be between 1 and {len(screenshotter.monitors) - 1}")
            self.monitor = dict(screenshotter.monitors[monitor_index])

        self.capture_config = capture_config
        self.preview = preview
        self.overlay = SubtitleOverlay(self.monitor, config["overlay"])
        self.ui_events: queue.Queue[tuple[str, ...]] = queue.Queue()
        self.running = threading.Event()
        self.enabled = threading.Event()
        self.stop_requested = threading.Event()
        self.active_source = ""
        self._active_source_lock = threading.Lock()
        self._input_trigger = threading.Event()
        self._trigger_lock = threading.Lock()
        self._trigger_serial = 0
        self.translation_cache: dict[str, str] = {}
        self.pending: queue.Queue[str] = queue.Queue(maxsize=1)
        self.tray = TrayController(self.request_toggle, self.request_stop)
        self.input_listener = InputTriggerListener(self._record_input_trigger)

        if not preview:
            self.ocr = SubtitleOcr(capture_config)
            self.translator = OpenAiCompatibleTranslator(config["api"], config.get("glossary", {}))

    def start(self) -> None:
        if self.preview:
            self.overlay.set_text("这是翻译字幕的位置预览。")
            self.overlay.root.after(8000, self.stop)
            self.overlay.run()
            return

        self.running.set()
        self.overlay.register_hotkeys(
            self.request_start,
            self.request_pause,
        )
        try:
            self.input_listener.start()
            self.tray.start()
            threading.Thread(target=self._capture_loop, name="subtitle-capture", daemon=True).start()
            threading.Thread(target=self._translation_loop, name="translation-api", daemon=True).start()
        except Exception:
            self.running.clear()
            self.input_listener.stop()
            self.overlay.unregister_hotkeys()
            raise
        self.overlay.root.after(80, self._drain_ui_events)
        logging.info("Ready. Ctrl+Shift+T starts translation, T captures one subtitle, Ctrl+Shift+Q pauses translation.")
        self.overlay.run()

    def start_translation(self, source: str = "unknown") -> None:
        if self.enabled.is_set():
            self.overlay.show_status("翻译已开启，按 T 识别")
            logging.info("Translation start ignored; already enabled (source=%s)", source)
            return
        self.enabled.set()
        self.overlay.show_status("翻译已开启，按 T 识别")
        self.tray.set_enabled(True)
        logging.info("Translation enabled; waiting for T trigger (source=%s)", source)

    def pause_translation(self, source: str = "unknown") -> None:
        if not self.enabled.is_set():
            self.overlay.show_status("翻译已关闭")
            logging.info("Translation pause ignored; already paused (source=%s)", source)
            return
        self.enabled.clear()
        self._cancel_pending_trigger()
        self._set_active_source("")
        self.ui_events.put(("text", ""))
        self.overlay.show_status("翻译已关闭")
        self.tray.set_enabled(False)
        logging.info("Translation paused (source=%s)", source)

    def toggle(self, source: str = "tray") -> None:
        if self.enabled.is_set():
            self.pause_translation(source)
        else:
            self.start_translation(source)

    def request_toggle(self) -> None:
        self.ui_events.put(("toggle", "tray"))

    def request_start(self) -> None:
        self.ui_events.put(("start", "hotkey"))

    def request_pause(self) -> None:
        self.ui_events.put(("pause", "hotkey"))

    def request_stop(self) -> None:
        self.ui_events.put(("stop", "tray"))

    def stop(self, source: str = "unknown") -> None:
        if self.stop_requested.is_set():
            return
        self.stop_requested.set()
        logging.info("Translator process exit requested (source=%s)", source)
        if self.preview:
            self.overlay.close()
            return
        self.running.clear()
        self.enabled.clear()
        self._input_trigger.set()
        self.input_listener.stop()
        self.tray.stop()
        self.translator.close()

    def _screen_box(self) -> ScreenBox:
        left = float(self.capture_config.get("left", 0.18))
        top = float(self.capture_config.get("top", 0.85))
        right = float(self.capture_config.get("right", 0.84))
        bottom = float(self.capture_config.get("bottom", 0.90))
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise RuntimeError("capture bounds must satisfy 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
        return ScreenBox(
            left=int(self.monitor["left"] + self.monitor["width"] * left),
            top=int(self.monitor["top"] + self.monitor["height"] * top),
            width=max(1, int(self.monitor["width"] * (right - left))),
            height=max(1, int(self.monitor["height"] * (bottom - top))),
        )

    def _record_input_trigger(self) -> None:
        """Hook-thread callback: signal capture only, never touch Tk or OCR."""
        if not self.enabled.is_set():
            return
        with self._trigger_lock:
            self._trigger_serial += 1
        self._input_trigger.set()

    def _cancel_pending_trigger(self) -> None:
        with self._trigger_lock:
            self._trigger_serial += 1
        self._input_trigger.clear()

    def _set_active_source(self, source: str) -> None:
        with self._active_source_lock:
            self.active_source = source

    def _is_active_source(self, source: str) -> bool:
        with self._active_source_lock:
            return self.active_source == source

    def _trigger_is_current(self, serial: int) -> bool:
        with self._trigger_lock:
            return serial == self._trigger_serial

    def _capture_loop(self) -> None:
        box = self._screen_box()
        logging.info("Subtitle OCR region: left=%d top=%d width=%d height=%d", box.left, box.top, box.width, box.height)

        with mss.MSS() as screenshotter:
            while self.running.is_set():
                if not self._input_trigger.wait(timeout=0.25):
                    continue
                self._input_trigger.clear()
                if not self.running.is_set() or not self.enabled.is_set():
                    continue

                # A new dialogue action must remove the prior translation
                # before a manually requested replacement is displayed.
                self._set_active_source("")
                self.ui_events.put(("text", ""))
                with self._trigger_lock:
                    serial = self._trigger_serial

                try:
                    capture_started = time.perf_counter()
                    frame = screenshotter.grab(box.__dict__)
                    capture_ms = (time.perf_counter() - capture_started) * 1000
                    image = Image.frombytes("RGB", frame.size, frame.rgb)
                    ocr_started = time.perf_counter()
                    source = self.ocr.read(image)
                    ocr_ms = (time.perf_counter() - ocr_started) * 1000
                    logging.info(
                        "Subtitle sampling completed: capture=%.0f ms OCR=%.0f ms chars=%d",
                        capture_ms,
                        ocr_ms,
                        len(source),
                    )
                except Exception as error:
                    logging.exception("OCR capture failed: %s", error)
                    continue

                # Ignore an OCR result if a new dialogue action occurred
                # while the screenshot or OCR operation was in progress.
                if not self.enabled.is_set() or not self._trigger_is_current(serial):
                    continue
                if not source:
                    logging.info("No subtitle detected after input trigger")
                    continue
                self._set_active_source(source)
                cached = self.translation_cache.get(source)
                if cached:
                    self.ui_events.put(("translation", source, cached))
                else:
                    self._queue_translation(source)

    def _queue_translation(self, source: str) -> None:
        try:
            self.pending.put_nowait(source)
        except queue.Full:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self.pending.put_nowait(source)
            except queue.Full:
                pass

    def _translation_loop(self) -> None:
        while self.running.is_set():
            try:
                source = self.pending.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                # A newer T press may have replaced this line while the
                # source was waiting in the single-item queue.
                if not self._is_active_source(source):
                    continue
                request_started = time.perf_counter()
                translated = self.translator.translate(source)
                request_ms = (time.perf_counter() - request_started) * 1000
                self.translation_cache[source] = translated
                self.ui_events.put(("translation", source, translated))
                logging.info("Translation API completed in %.0f ms (%d source characters)", request_ms, len(source))
            except requests.RequestException as error:
                logging.error("Translation API request failed: %s", error)
            except Exception as error:
                logging.exception("Translation failed: %s", error)

    def _drain_ui_events(self) -> None:
        if self.stop_requested.is_set():
            self.overlay.close()
            return
        while True:
            try:
                event = self.ui_events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "text":
                self.overlay.set_text(event[1])
            elif kind == "toggle":
                self.toggle(event[1])
            elif kind == "start":
                self.start_translation(event[1])
            elif kind == "pause":
                self.pause_translation(event[1])
            elif kind == "stop":
                self.stop(event[1])
            elif kind == "translation":
                _, source, translated = event
                # Do not show a slow response for dialogue that has already
                # disappeared or changed to a different line.
                if self.enabled.is_set() and self._is_active_source(source):
                    self.overlay.set_text(translated)
        if self.running.is_set():
            self.overlay.root.after(80, self._drain_ui_events)


def main() -> int:
    parser = argparse.ArgumentParser(description="Genshin screen subtitle translator")
    parser.add_argument("--overlay-preview", action="store_true", help="show the overlay position for 8 seconds without OCR or API")
    args = parser.parse_args()
    configure_logging()
    try:
        if not is_admin():
            logging.warning("Not elevated. The overlay may appear behind an elevated game window. Use run_as_admin.ps1.")
        config = load_config()
        app = TranslatorApp(config, preview=args.overlay_preview)
        app.start()
        return 0
    except RuntimeError as error:
        logging.error("%s", error)
        show_error(str(error))
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        logging.exception("Unexpected startup failure")
        show_error(f"Unexpected startup failure:\n{error}")
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
