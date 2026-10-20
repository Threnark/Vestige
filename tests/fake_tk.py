"""
fake_tk.py
----------
A tiny stand-in for tkinter and customtkinter, used ONLY by the tests, so the Help window
can be built and clicked through on a machine with no screen (and without either library).

What it is: every widget is a FakeWidget that remembers how it was created, its options,
its children and the functions bound to it. Nothing is drawn.

What it can and can't prove: it proves Vestige's own logic (which topic is shown, that every
block type renders, that wrap widths follow the window, that links open the right address,
that there is only ever one Help window). It can NOT prove how things look on a real screen.

Usage:
    with fake_tk.installed() as env:
        env.help_ui.open_help(env.root, "gmail")
"""

from __future__ import annotations

import contextlib
import sys
import types
from unittest import mock


class FakeWidget:
    """Any widget. Keeps its options, its children and its event bindings."""
    _is_toplevel = False

    def __init__(self, master=None, *args, **options):
        self.master = master
        self.kw = dict(options)
        self.children: list = []
        self.bindings: dict = {}
        self.destroyed = False
        self.packed = {}
        if hasattr(master, "children"):
            master.children.append(self)

    # --- options ---
    def configure(self, **options):
        self.kw.update(options)

    config = configure

    def cget(self, name):
        return self.kw.get(name)

    # --- geometry managers (recorded, never laid out) ---
    def pack(self, **options): self.packed = options
    def grid(self, **options): self.packed = options
    def place(self, **options): pass
    def grid_remove(self): pass
    def pack_propagate(self, flag=True): pass
    def grid_propagate(self, flag=True): pass
    def grid_columnconfigure(self, *a, **k): pass
    def grid_rowconfigure(self, *a, **k): pass
    def columnconfigure(self, *a, **k): pass
    def rowconfigure(self, *a, **k): pass

    # --- events and timers ---
    def bind(self, sequence, func=None, add=None):
        self.bindings[sequence] = func

    def after(self, ms, func=None, *args):
        return "after#1"

    def after_cancel(self, job): pass
    def update_idletasks(self): pass
    def focus_force(self): pass
    def focus(self): pass

    # --- window-ish methods ---
    def winfo_exists(self): return 0 if self.destroyed else 1
    def winfo_width(self): return self.__dict__.get("fake_width", 1000)      # (not getattr: permissive mode answers anything)
    def winfo_height(self): return 700
    def winfo_screenwidth(self): return 1920
    def winfo_screenheight(self): return 1080

    def winfo_toplevel(self):
        node = self
        while node is not None and not node._is_toplevel:
            node = node.master
        return node

    def destroy(self):
        self.destroyed = True
        for child in list(self.children):
            child.destroy()
        if hasattr(self.master, "children") and self in self.master.children:
            self.master.children.remove(self)

    def get_children(self, *args):
        return ()

    # --- helpers for the tests ---
    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def texts(self):
        return [w.kw["text"] for w in self.walk() if isinstance(w.kw.get("text"), str)]


class FakeToplevel(FakeWidget):
    _is_toplevel = True
    clipboard: str = ""

    def title(self, *a): pass
    def geometry(self, spec=None): self.kw["geometry"] = spec
    def minsize(self, *a): pass
    def resizable(self, *a): pass
    def protocol(self, *a): pass
    def transient(self, *a): pass
    def lift(self): pass
    def deiconify(self): pass
    def clipboard_clear(self): FakeToplevel.clipboard = ""
    def clipboard_append(self, text): FakeToplevel.clipboard += text


class FakeTclError(Exception):
    pass


class FakeVar:
    """Stands in for StringVar / BooleanVar / IntVar: holds a value and runs trace callbacks on set(),
    so code that reacts to a changed field (e.g. the Add account window) can be tested."""

    def __init__(self, master=None, value=None, **options):
        self._value = "" if value is None else value
        self._traces: list = []

    def get(self):
        return self._value

    def set(self, value):
        self._value = value
        for callback in list(self._traces):
            callback("fake", "", "write")

    def trace_add(self, mode, callback):
        self._traces.append(callback)


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    # any CTk... / Tk widget name not given above becomes a plain FakeWidget
    module.__getattr__ = lambda attr: FakeWidget
    return module


class FakeFont:
    def __init__(self, root=None, font=None, **options):
        self.options = options

    def metrics(self, what):
        return 17

    def actual(self):
        return {}


class FakeScaling:
    @staticmethod
    def get_widget_scaling(widget):
        return 1.0


class Environment:
    """What installed() hands back."""
    root = None
    theme = None
    help_ui = None


def _anything_goes(self, name):
    """Permissive mode: a method the fake doesn't know does nothing and returns another fake widget."""
    if name.startswith("_"):
        raise AttributeError(name)
    return lambda *args, **options: FakeWidget(self)


@contextlib.contextmanager
def installed(permissive: bool = False):
    """Put fake tkinter/customtkinter in place, import ui_theme and ui_help against them, and
    undo everything afterwards (so a real tkinter in the same process is not disturbed).

    permissive=False (the default, used for the Help window tests) means calling a Tk method the
    fake doesn't implement is an error, so mistakes are caught. permissive=True makes every unknown
    method a harmless no-op; it is used to build the whole interface in the smoke tests."""
    tk = _module("tkinter", TclError=FakeTclError, Toplevel=FakeToplevel, Label=FakeWidget, StringVar=FakeVar,
                 BooleanVar=FakeVar, IntVar=FakeVar, DoubleVar=FakeVar, END="end")
    tkfont = _module("tkinter.font", Font=FakeFont, families=lambda root=None: ["Segoe UI"])
    ttk = _module("tkinter.ttk", Style=type("Style", (), {
        "__init__": lambda self, root=None: None, "theme_use": lambda self, n: None,
        "layout": lambda self, *a: None, "configure": lambda self, *a, **k: None,
        "map": lambda self, *a, **k: None}))
    filedialog = _module("tkinter.filedialog")
    tk.font, tk.ttk, tk.filedialog = tkfont, ttk, filedialog
    ctk = _module("customtkinter", CTkToplevel=FakeToplevel, ScalingTracker=FakeScaling,
                  CTkFont=lambda **options: FakeFont(**options))
    fakes = {"tkinter": tk, "tkinter.font": tkfont, "tkinter.ttk": ttk, "tkinter.filedialog": filedialog,
             "customtkinter": ctk}
    patches = [mock.patch.dict(sys.modules, fakes)]
    if permissive:
        patches.append(mock.patch.object(FakeWidget, "__getattr__", _anything_goes, create=True))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        sys.modules.pop("ui_theme", None)
        sys.modules.pop("ui_help", None)
        import ui_help
        import ui_theme

        env = Environment()
        env.root = FakeToplevel(None)
        ui_theme.init(env.root)
        env.theme, env.help_ui = ui_theme, ui_help
        ui_help._window = None
        FakeToplevel.clipboard = ""
        try:
            yield env
        finally:
            ui_help._window = None
