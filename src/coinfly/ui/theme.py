"""Native desktop palette and shared widget construction."""

from typing import Any

BACKGROUND = "#10151F"
PANEL = "#192230"
PANEL_ALT = "#202D3D"
TEXT = "#EDF2F8"
MUTED = "#9CAFC3"
ACCENT = "#4CD8C4"
WARNING = "#F2BE69"
DANGER = "#F08086"


def label(toolkit: Any, parent: Any, text: str, *, size: int = 13, color: str = TEXT,
          bold: bool = False, **kwargs: Any) -> Any:
    return toolkit.CTkLabel(parent, text=text, font=("", size, "bold" if bold else "normal"),
                            text_color=color, anchor="w", **kwargs)


def configure_table_style() -> None:
    from tkinter import ttk
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Coinfly.Treeview", background=PANEL, foreground=TEXT, fieldbackground=PANEL,
                    borderwidth=0, rowheight=30, font=("", 11))
    style.configure("Coinfly.Treeview.Heading", background=PANEL_ALT, foreground=MUTED,
                    font=("", 11, "bold"), relief="flat")
    style.map("Coinfly.Treeview", background=[("selected", "#295951")], foreground=[("selected", TEXT)])


def table(parent: Any, columns: tuple[tuple[str, str, int], ...], *, height: int = 10) -> Any:
    from tkinter import ttk
    widget = ttk.Treeview(parent, columns=tuple(item[0] for item in columns), show="headings",
                          height=height, style="Coinfly.Treeview", selectmode="browse")
    for key, title, width in columns:
        widget.heading(key, text=title)
        widget.column(key, width=width, minwidth=60, anchor="w", stretch=True)
    return widget
