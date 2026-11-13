"""Windows notification-area (tray) app: `python -m ghostpad --tray`, or the windowed exe.

The daemon runs on its own asyncio loop in a worker thread; the main thread runs a hidden
Win32 window and message loop that owns the tray icon. Win32 is reached through ctypes
(stdlib only, no pystray/pywin32).

Fail safe: Quit, Windows logoff/shutdown (WM_ENDSESSION) and Ctrl+C/Ctrl+Break in a console
all stop the server the normal way (pads neutral, then unplug). If the process is killed
instead, the ViGEm driver unplugs the pads when the process exits.

The pure helpers at the top (status text, icon state, autostart command line) run on any
OS and are unit-tested; the Win32 part needs Windows.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .backends.base import BackendUnavailable
from .server import GhostpadServer
from .trayicon import COLORS, render_rgba, rgba_to_bgra

log = logging.getLogger("ghostpad.tray")

APP = "ghostpad"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOW_CLASS = "ghostpad-tray"
TIP_MAX = 127  # NOTIFYICONDATAW.szTip is 128 WCHARs including the NUL


# --- pure helpers ------------------------------------------------------------


def default_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP / "ghostpad.log"


def _players(pads: list[int]) -> str:
    """Pad indices as the 1-based player numbers Windows shows."""
    if not pads:
        return "no pads yet"
    return ("player " if len(pads) == 1 else "players ") + ", ".join(str(p + 1) for p in pads)


def icon_state(status: dict | None, alive: bool) -> str:
    if not alive or status is None or not status.get("listening", True):
        return "error"
    if status.get("backend") == "mock":
        return "mock"
    return "connected" if status.get("client") else "idle"


def status_lines(status: dict | None, alive: bool) -> list[str]:
    """Two lines for the menu header (and, joined, the tooltip)."""
    if not alive or status is None:
        return ["ghostpad stopped", "see the log"]
    head = f"ghostpad on port {status['port']} ({status['backend']})"
    if status.get("backend") == "mock":
        head += ": no virtual pad"
    client = status.get("client")
    second = f"{client}: {_players(status.get('pads', []))}" if client else "waiting for a client"
    return [head, second]


def tooltip(status: dict | None, alive: bool) -> str:
    text = "\n".join(status_lines(status, alive))
    return text if len(text) <= TIP_MAX else text[:TIP_MAX - 1] + "…"


def autostart_command(exe: str, args: argparse.Namespace) -> str:
    """Command line for the HKCU Run key: this exe in tray mode with the current settings."""
    argv = ["--tray", "--port", str(args.port), "--bind", args.bind, "--backend", args.backend]
    if args.token:
        argv += ["--token", args.token]
    if args.log_file:
        argv += ["--log-file", str(args.log_file)]
    return subprocess.list2cmdline([exe] + argv)


def web_menu_label(status: dict | None) -> str:
    """Name the current owner: the page connects on load only when the pads are free."""
    client = status.get("client") if status else None
    if client:
        return f"Open web test client ({client[:40]} has the pads)"
    return "Open web test client"


def web_client_url(port: int, token: str | None) -> str:
    return f"http://localhost:{port}/" + (f"?token={quote(token, safe='')}" if token else "")


# --- daemon thread -------------------------------------------------------------


class DaemonThread(threading.Thread):
    """Runs the server on a private event loop; stop() shuts it down the normal way."""

    def __init__(self, make_server: Callable[[], GhostpadServer]) -> None:
        super().__init__(name="ghostpad-daemon", daemon=True)
        self._make_server = make_server
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.server: GhostpadServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            server = self._make_server()
            loop.run_until_complete(server.start())
        except BaseException as e:  # reported by the caller (message box)
            self.error = e
            self.ready.set()
            loop.close()
            return
        self.server = server
        self.ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(server.stop())  # neutral, then unplug
            except Exception:
                log.exception("server stop failed")
            loop.close()

    def status(self) -> dict | None:
        s = self.server
        return s.status() if s is not None and self.is_alive() else None

    def stop(self, timeout: float = 5.0) -> bool:
        loop = self.loop
        if loop is not None and self.is_alive():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass  # loop already closed
        if self.is_alive():
            self.join(timeout)
        return not self.is_alive()


# --- autostart (HKCU Run key) --------------------------------------------------


def autostart_get() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            value, _ = winreg.QueryValueEx(k, APP)
            return str(value)
    except OSError:
        return None


def autostart_set(command: str | None) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if command is None:
            try:
                winreg.DeleteValue(k, APP)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, command)


# --- Win32 --------------------------------------------------------------------

WM_NULL, WM_DESTROY, WM_CLOSE = 0x0000, 0x0002, 0x0010
WM_QUERYENDSESSION, WM_ENDSESSION, WM_TIMER = 0x0011, 0x0016, 0x0113
WM_LBUTTONUP, WM_RBUTTONUP, WM_APP = 0x0202, 0x0205, 0x8000
WM_TRAY = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_ERROR = 0x03
MF_STRING, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0000, 0x0001, 0x0008, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100
MB_ICONERROR, MB_ICONINFORMATION, MB_SETFOREGROUND, MB_TOPMOST = 0x10, 0x40, 0x10000, 0x40000
SM_CXSMICON = 49
ERROR_ALREADY_EXISTS = 183
TIMER_ID, TIMER_MS = 1, 1000
CMD_LOG, CMD_WEB, CMD_AUTOSTART, CMD_QUIT = 1, 2, 3, 4


class _Api:
    """ctypes prototypes for the Win32 calls used here (explicit types for 64-bit handles)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        self.ct, self.w = ctypes, w
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
                        ("hbrBackground", w.HANDLE), ("lpszMenuName", w.LPCWSTR),
                        ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                        ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                        ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                        ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", w.HICON)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
                        ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 1)]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                        ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]

        self.WNDCLASSEXW, self.NOTIFYICONDATAW = WNDCLASSEXW, NOTIFYICONDATAW
        self.BITMAPINFO, self.BITMAPINFOHEADER, self.ICONINFO = BITMAPINFO, BITMAPINFOHEADER, ICONINFO

        def fn(dll, name, restype, *argtypes):
            f = getattr(dll, name)
            f.restype, f.argtypes = restype, list(argtypes)
            setattr(self, name, f)

        P = ctypes.POINTER
        fn(user32, "DefWindowProcW", LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        fn(user32, "RegisterClassExW", w.ATOM, P(WNDCLASSEXW))
        fn(user32, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
        fn(user32, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
           ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
        fn(user32, "DestroyWindow", w.BOOL, w.HWND)
        fn(user32, "GetMessageW", w.BOOL, P(w.MSG), w.HWND, w.UINT, w.UINT)
        fn(user32, "TranslateMessage", w.BOOL, P(w.MSG))
        fn(user32, "DispatchMessageW", LRESULT, P(w.MSG))
        fn(user32, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        fn(user32, "PostQuitMessage", None, ctypes.c_int)
        fn(user32, "SetTimer", ctypes.c_size_t, w.HWND, ctypes.c_size_t, w.UINT, ctypes.c_void_p)
        fn(user32, "KillTimer", w.BOOL, w.HWND, ctypes.c_size_t)
        fn(user32, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
        fn(user32, "CreatePopupMenu", w.HMENU)
        fn(user32, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
        fn(user32, "TrackPopupMenu", w.BOOL, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
           w.HWND, ctypes.c_void_p)
        fn(user32, "DestroyMenu", w.BOOL, w.HMENU)
        fn(user32, "GetCursorPos", w.BOOL, P(w.POINT))
        fn(user32, "SetForegroundWindow", w.BOOL, w.HWND)
        fn(user32, "MessageBoxW", ctypes.c_int, w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT)
        fn(user32, "CreateIconIndirect", w.HICON, P(ICONINFO))
        fn(user32, "DestroyIcon", w.BOOL, w.HICON)
        fn(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
        fn(user32, "SetProcessDPIAware", w.BOOL)
        fn(shell32, "Shell_NotifyIconW", w.BOOL, w.DWORD, P(NOTIFYICONDATAW))
        fn(kernel32, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
        fn(kernel32, "CreateMutexW", w.HANDLE, ctypes.c_void_p, w.BOOL, w.LPCWSTR)
        fn(kernel32, "CloseHandle", w.BOOL, w.HANDLE)
        fn(gdi32, "CreateDIBSection", w.HBITMAP, w.HDC, P(BITMAPINFO), w.UINT, P(ctypes.c_void_p),
           w.HANDLE, w.DWORD)
        fn(gdi32, "CreateBitmap", w.HBITMAP, ctypes.c_int, ctypes.c_int, w.UINT, w.UINT, ctypes.c_void_p)
        fn(gdi32, "DeleteObject", w.BOOL, w.HANDLE)

    def message(self, text: str, error: bool) -> None:
        flags = (MB_ICONERROR if error else MB_ICONINFORMATION) | MB_SETFOREGROUND | MB_TOPMOST
        self.MessageBoxW(None, text, APP, flags)

    def make_icon(self, size: int, rgb: tuple[int, int, int]) -> int:
        ct = self.ct
        bgra = rgba_to_bgra(render_rgba(size, rgb))
        bmi = self.BITMAPINFO()
        h = bmi.bmiHeader
        h.biSize = ct.sizeof(self.BITMAPINFOHEADER)
        h.biWidth, h.biHeight = size, -size  # top-down
        h.biPlanes, h.biBitCount = 1, 32
        bits = ct.c_void_p()
        color = self.CreateDIBSection(None, ct.byref(bmi), 0, ct.byref(bits), None, 0)
        if not color or not bits.value:
            raise OSError("CreateDIBSection failed")
        ct.memmove(bits, bgra, len(bgra))
        stride = (size + 15) // 16 * 2  # 1-bpp rows are WORD aligned; all 0 = use colour + alpha
        mask_bits = (ct.c_ubyte * (stride * size))()
        mask = self.CreateBitmap(size, size, 1, 1, mask_bits)
        try:
            info = self.ICONINFO(True, 0, 0, mask, color)
            icon = self.CreateIconIndirect(ct.byref(info))
        finally:
            self.DeleteObject(mask)
            self.DeleteObject(color)
        if not icon:
            raise OSError("CreateIconIndirect failed")
        return icon


class TrayApp:
    def __init__(self, api: _Api, daemon: DaemonThread, args: argparse.Namespace, log_path: Path) -> None:
        self.api, self.daemon, self.args, self.log_path = api, daemon, args, log_path
        self.frozen = bool(getattr(sys, "frozen", False))
        size = api.GetSystemMetrics(SM_CXSMICON) or 16
        self.icons = {state: api.make_icon(size, rgb) for state, rgb in COLORS.items()}
        self.hwnd = None
        self.added = False
        self.shown: tuple[str, str] | None = None
        self.died_reported = False
        self._wndproc = api.WNDPROC(self._proc)  # keep a reference: called from Win32
        self._taskbar_created = api.RegisterWindowMessageW("TaskbarCreated")

    # window and icon

    def create_window(self) -> None:
        api, ct = self.api, self.api.ct
        hinst = api.GetModuleHandleW(None)
        wc = api.WNDCLASSEXW()
        wc.cbSize = ct.sizeof(api.WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = WINDOW_CLASS
        api.RegisterClassExW(ct.byref(wc))  # fails harmlessly if already registered
        self.hwnd = api.CreateWindowExW(0, WINDOW_CLASS, f"{WINDOW_CLASS} {os.getpid()}", 0,
                                        0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed ({ct.get_last_error()})")

    def _nid(self, flags: int) -> object:
        api, ct = self.api, self.api.ct
        nid = api.NOTIFYICONDATAW()
        nid.cbSize = ct.sizeof(api.NOTIFYICONDATAW)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        return nid

    def _notify(self, op: int, state: str, tip: str, balloon: tuple[str, str] | None = None) -> bool:
        flags = NIF_MESSAGE | NIF_ICON | NIF_TIP | (NIF_INFO if balloon else 0)
        nid = self._nid(flags)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.icons[state]
        nid.szTip = tip
        if balloon:
            nid.szInfoTitle, nid.szInfo = balloon[0][:63], balloon[1][:255]
            nid.dwInfoFlags = NIIF_ERROR
        return bool(self.api.Shell_NotifyIconW(op, self.api.ct.byref(nid)))

    def refresh(self, force: bool = False) -> None:
        alive = self.daemon.is_alive()
        status = self.daemon.status()
        want = (icon_state(status, alive), tooltip(status, alive))
        balloon = None
        if not alive and not self.died_reported:
            self.died_reported = True
            balloon = ("ghostpad stopped", f"The daemon stopped unexpectedly. Log: {self.log_path}")
            log.error("daemon thread ended unexpectedly")
        if not self.added:
            # Explorer may not be up yet at logon: keep trying on the timer.
            self.added = self._notify(NIM_ADD, *want)
            if self.added:
                self.shown = want
                log.info("tray icon added (%s)", want[0])
            return
        if force or want != self.shown or balloon:
            if self._notify(NIM_MODIFY, *want, balloon=balloon):
                self.shown = want

    def remove_icon(self) -> None:
        if self.added:
            self.api.Shell_NotifyIconW(NIM_DELETE, self.api.ct.byref(self._nid(0)))
            self.added = False

    def destroy_icons(self) -> None:
        for icon in self.icons.values():
            self.api.DestroyIcon(icon)
        self.icons = {}

    # menu

    def show_menu(self) -> None:
        api, ct = self.api, self.api.ct
        alive = self.daemon.is_alive()
        status = self.daemon.status()
        menu = api.CreatePopupMenu()
        try:
            for line in status_lines(status, alive):
                api.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, line)
            api.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            api.AppendMenuW(menu, MF_STRING, CMD_LOG, "Open log")
            api.AppendMenuW(menu, MF_STRING | (0 if alive else MF_GRAYED), CMD_WEB, web_menu_label(status))
            if self.frozen:
                checked = MF_CHECKED if autostart_get() else 0
                api.AppendMenuW(menu, MF_STRING | checked, CMD_AUTOSTART, "Start with Windows")
            else:
                api.AppendMenuW(menu, MF_STRING | MF_GRAYED, CMD_AUTOSTART, "Start with Windows (exe build only)")
            api.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            api.AppendMenuW(menu, MF_STRING, CMD_QUIT, "Quit")
            pt = api.w.POINT()
            api.GetCursorPos(ct.byref(pt))
            api.SetForegroundWindow(self.hwnd)  # else the menu does not close on an outside click
            cmd = api.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                     pt.x, pt.y, 0, self.hwnd, None)
            api.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            api.DestroyMenu(menu)
        self.command(cmd)

    def command(self, cmd: int) -> None:
        if cmd == CMD_LOG:
            target = self.log_path if self.log_path.exists() else self.log_path.parent
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif cmd == CMD_WEB:
            status = self.daemon.status()
            if status:
                webbrowser.open(web_client_url(status["port"], self.args.token))
        elif cmd == CMD_AUTOSTART:
            try:
                if autostart_get():
                    autostart_set(None)
                    log.info("autostart off")
                else:
                    autostart_set(autostart_command(sys.executable, self.args))
                    log.info("autostart on: %s", autostart_get())
            except OSError as e:
                self.api.message(f"Could not change the autostart setting:\n{e}", error=True)
        elif cmd == CMD_QUIT:
            log.info("quit from the tray menu")
            self.api.DestroyWindow(self.hwnd)

    # message loop

    def _proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                    self.show_menu()
                return 0
            if msg == WM_TIMER:
                self.refresh()
                return 0
            if msg == self._taskbar_created:  # Explorer restarted: the icon is gone
                self.added = False
                self.refresh()
                return 0
            if msg == WM_QUERYENDSESSION:
                return 1
            if msg == WM_ENDSESSION:
                if wparam:  # logoff/shutdown: the process may be killed after this returns
                    log.info("session ending: releasing pads")
                    self.daemon.stop(timeout=3.0)
                return 0
            if msg == WM_CLOSE:
                self.api.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.api.KillTimer(hwnd, TIMER_ID)
                self.remove_icon()
                self.api.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("tray: handling message %#x failed", msg)
        return self.api.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> None:
        api, ct = self.api, self.api.ct
        self.create_window()
        self.refresh()
        # The timer also wakes the loop each second, so Python runs pending signal handlers.
        api.SetTimer(self.hwnd, TIMER_ID, TIMER_MS, None)
        msg = api.w.MSG()
        while api.GetMessageW(ct.byref(msg), None, 0, 0) > 0:
            api.TranslateMessage(ct.byref(msg))
            api.DispatchMessageW(ct.byref(msg))


def run_tray(args: argparse.Namespace, log_path: Path,
             make_server: Callable[[], GhostpadServer]) -> int:
    """Run the daemon with a tray icon until Quit. Returns the process exit code."""
    if sys.platform != "win32":
        log.error("--tray needs Windows")
        return 2
    api = _Api()
    api.SetProcessDPIAware()
    mutex = None
    if args.port:
        mutex = api.CreateMutexW(None, False, f"Local\\ghostpad-tray-{args.port}")
        if api.ct.get_last_error() == ERROR_ALREADY_EXISTS:
            log.error("a ghostpad tray app already runs on port %d", args.port)
            api.message(f"ghostpad is already running in the tray (port {args.port}).", error=False)
            api.CloseHandle(mutex)
            return 1

    daemon = DaemonThread(make_server)
    daemon.start()
    daemon.ready.wait()
    if daemon.error is not None:
        e = daemon.error
        log.error("cannot start: %s", e)
        api.message(f"ghostpad could not start:\n\n{e}\n\nLog: {log_path}", error=True)
        if mutex:
            api.CloseHandle(mutex)
        return 2 if isinstance(e, BackendUnavailable) else 1

    app = TrayApp(api, daemon, args, log_path)
    old = {}
    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if sig is not None:  # console runs: Ctrl+C / Ctrl+Break quit cleanly
            old[sig] = signal.signal(sig, lambda *_: app.hwnd and api.PostMessageW(app.hwnd, WM_CLOSE, 0, 0))
    try:
        app.run()
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
        app.remove_icon()
        if not daemon.stop(timeout=5.0):
            log.error("daemon did not stop within 5 s")
        app.destroy_icons()
        if mutex:
            api.CloseHandle(mutex)
    log.info("tray exit")
    return 0
