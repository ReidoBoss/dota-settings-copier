#!/usr/bin/env python3
"""Dota 2 Settings Copier.

Copy Dota 2 settings and hotkeys from one Steam account to another on the
same computer. Works on Windows, Linux and macOS using only the Python
standard library (plus Tk for the window).
"""

import argparse
import datetime as _dt
import filecmp
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # Allows the core functions to be used/tested without Tk.
    tk = None

__version__ = "1.0.0"

APP_NAME = "Dota 2 Settings Copier"
DOTA_APP_ID = "570"
STEAMID64_BASE = 76561197960265728

# What gets copied, grouped by checkbox. Glob patterns relative to userdata/<id>/570.
CATEGORIES = {
    "Hotkeys (all bindings + hotkey slots)": [
        "remote/user_keys.vcfg",
        "remote/cfg/dotakeys_personal.lst",
        "local/cfg/user_keys_0_slot*.vcfg*",
    ],
    "Game options (quick cast, camera, chat wheel, sounds...)": [
        "remote/user_convars.vcfg",
        "local/cfg/user_convars_0_slot*.vcfg*",
    ],
    "Video / graphics": [
        "local/cfg/video.txt",
        "local/cfg/machine_convars.vcfg",
    ],
    "Other (control groups, item suggestions, chat, hero builds, facets, guides)": [
        "remote/cfg/chat.cfg",
        "remote/cfg/herobuilds.cfg",
        "remote/cfg/hero_facet_config.cfg",
        "remote/cfg/dota_armory_filters.txt",
        "remote/scripts/control_groups.txt",
        "remote/scripts/item_suggest_preference.txt",
        "remote/scripts/lobby_settings.txt",
        "remote/guides/*",
    ],
}

CLOUD_NOTE = (
    "Close Dota 2 before copying. If Steam later shows a \"Cloud Sync Conflict\" "
    "for Dota 2, choose the LOCAL files (this computer), or Steam will bring "
    "back the old settings."
)


@dataclass
class Account:
    id: str
    name: str
    path: Path  # .../userdata/<id>/570

    @property
    def label(self):
        return f"{self.name}   [{self.id}]"


@dataclass
class CopyResult:
    copied: int
    failed: int
    backup: Path


# ---------------------------------------------------------------- Steam lookup

def candidate_steam_paths():
    home = Path.home()
    paths = []
    if os.environ.get("STEAM_PATH"):
        paths.append(Path(os.environ["STEAM_PATH"]))

    if sys.platform == "win32":
        import winreg

        for hive, key, value in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    paths.append(Path(winreg.QueryValueEx(k, value)[0]))
            except OSError:
                pass
        for var in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(var):
                paths.append(Path(os.environ[var]) / "Steam")
    elif sys.platform == "darwin":
        paths.append(home / "Library" / "Application Support" / "Steam")
    else:
        paths += [
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".steam/root",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",  # Flatpak
            home / "snap/steam/common/.local/share/Steam",  # Snap
        ]
    return paths


def find_steam_path():
    for path in candidate_steam_paths():
        if (path / "userdata").is_dir():
            return path.resolve()
    return None


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _account_names(steam_path):
    """Map 32-bit account id -> display name using loginusers.vdf."""
    names = {}
    text = _read_text(steam_path / "config" / "loginusers.vdf")
    for m in re.finditer(r'"(\d{17})"\s*\{([^}]*)\}', text):
        account_id = str(int(m.group(1)) - STEAMID64_BASE)
        block = m.group(2)
        acct = re.search(r'"AccountName"\s*"([^"]*)"', block)
        persona = re.search(r'"PersonaName"\s*"([^"]*)"', block)
        if persona and acct:
            names[account_id] = f"{persona.group(1)} ({acct.group(1)})"
        elif persona or acct:
            names[account_id] = (persona or acct).group(1)
    return names


def get_accounts(steam_path):
    """Return every Steam account on this computer that has Dota 2 settings."""
    steam_path = Path(steam_path)
    names = _account_names(steam_path)
    accounts = []
    userdata = steam_path / "userdata"
    if not userdata.is_dir():
        return accounts
    for user_dir in userdata.iterdir():
        if not user_dir.name.isdigit() or user_dir.name == "0":
            continue
        dota_dir = user_dir / DOTA_APP_ID
        if not dota_dir.is_dir():
            continue
        name = names.get(user_dir.name)
        if not name:
            m = re.search(
                r'"PersonaName"\s*"([^"]*)"',
                _read_text(user_dir / "config" / "localconfig.vdf"),
            )
            name = m.group(1) if m else "Unknown account"
        accounts.append(Account(user_dir.name, name, dota_dir))
    accounts.sort(key=lambda a: a.name.lower())
    return accounts


def is_dota_running():
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq dota2.exe", "/NH"],
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            return "dota2.exe" in out.lower()
        if Path("/proc").is_dir():
            for comm in Path("/proc").glob("[0-9]*/comm"):
                try:
                    if comm.read_text().strip() == "dota2":
                        return True
                except OSError:
                    continue
            return False
        return subprocess.run(["pgrep", "-x", "dota2"], capture_output=True).returncode == 0
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------ copy / backup

def default_backup_root():
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "DotaSettingsBackups"


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)


def backup_account(account, backup_root):
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(backup_root) / f"{account.id}_{_safe_name(account.name)}_{stamp}"
    shutil.copytree(account.path, dest)
    return dest


def category_files(src_dir, category):
    files = []
    for pattern in CATEGORIES[category]:
        files += sorted(p for p in Path(src_dir).glob(pattern) if p.is_file())
    return files


def copy_settings(src, dst, categories, backup_root, log=print):
    """Back up dst, then copy the chosen categories from src to dst."""
    if src.id == dst.id:
        raise ValueError("FROM and TO are the same account.")
    log(f"Backing up {dst.name}...")
    backup = backup_account(dst, backup_root)
    log(f"Backup saved: {backup}")

    copied = failed = 0
    for category in categories:
        log(f"-- {category}")
        for file in category_files(src.path, category):
            rel = file.relative_to(src.path)
            target = dst.path / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, target)
                ok = filecmp.cmp(file, target, shallow=False)
            except OSError as e:
                log(f"   ERROR  {rel.as_posix()}: {e}")
                ok = False
            if ok:
                log(f"   OK  {rel.as_posix()}")
                copied += 1
            else:
                log(f"   FAILED  {rel.as_posix()}")
                failed += 1
    return CopyResult(copied, failed, backup)


def backup_account_id(backup_dir):
    m = re.match(r"^(\d+)_", Path(backup_dir).name)
    return m.group(1) if m else None


def restore_backup(backup_dir, account):
    shutil.copytree(backup_dir, account.path, dirs_exist_ok=True)


def open_folder(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening a folder in Explorer
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# --------------------------------------------------------------------- GUI

class App:
    def __init__(self, root, steam_path=None, backup_root=None):
        self.root = root
        self.steam_path_override = steam_path
        self.backup_root = Path(backup_root) if backup_root else default_backup_root()
        self.accounts = []

        root.title(APP_NAME)
        root.minsize(640, 600)
        style = ttk.Style(root)
        if sys.platform.startswith("linux") and "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("TkDefaultFont", 14, "bold"))
        style.configure("Note.TLabel", foreground="#b00020")
        style.configure("Primary.TButton", font=("TkDefaultFont", 10, "bold"))

        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Make one Steam account's Dota 2 settings and hotkeys match another's.").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        ttk.Label(frame, text="Copy settings FROM:").grid(row=2, column=0, sticky="w")
        self.from_var = tk.StringVar()
        self.from_box = ttk.Combobox(frame, textvariable=self.from_var, state="readonly")
        self.from_box.grid(row=3, column=0, sticky="ew", pady=(2, 8))

        ttk.Button(frame, text="⇅ Swap", command=self.swap).grid(row=3, column=1, rowspan=3, padx=(8, 0))

        ttk.Label(frame, text="Copy settings TO (gets overwritten, a backup is made first):").grid(
            row=4, column=0, sticky="w"
        )
        self.to_var = tk.StringVar()
        self.to_box = ttk.Combobox(frame, textvariable=self.to_var, state="readonly")
        self.to_box.grid(row=5, column=0, sticky="ew", pady=(2, 8))

        group = ttk.LabelFrame(frame, text="What to copy", padding=(10, 6))
        group.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        self.category_vars = {}
        for i, category in enumerate(CATEGORIES):
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(group, text=category, variable=var).grid(row=i, column=0, sticky="w", pady=2)
            self.category_vars[category] = var

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.copy_button = ttk.Button(buttons, text="Copy Settings", style="Primary.TButton", command=self.do_copy)
        self.copy_button.pack(side="left")
        ttk.Button(buttons, text="Restore a Backup…", command=self.do_restore).pack(side="left", padx=6)
        ttk.Button(buttons, text="Open Backups Folder", command=lambda: open_folder(self.backup_root)).pack(
            side="left"
        )
        ttk.Button(buttons, text="Refresh", command=self.load_accounts).pack(side="right")

        log_frame = ttk.Frame(frame)
        log_frame.grid(row=8, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(8, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="none", font=("TkFixedFont", 9), state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        ttk.Label(frame, text=CLOUD_NOTE, style="Note.TLabel", wraplength=600, justify="left").grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

        self.load_accounts()

    def log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.root.update_idletasks()

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def load_accounts(self):
        self.clear_log()
        steam_path = self.steam_path_override or find_steam_path()
        if not steam_path or not (Path(steam_path) / "userdata").is_dir():
            self.accounts = []
            self.from_box["values"] = self.to_box["values"] = ()
            self.log("Could not find Steam. Is it installed? (You can pass --steam-path.)")
            return
        self.log(f"Steam folder: {steam_path}")
        self.accounts = get_accounts(steam_path)
        labels = [a.label for a in self.accounts]
        self.from_box["values"] = labels
        self.to_box["values"] = labels
        if not self.accounts:
            self.log("No Steam accounts with Dota 2 settings found. Launch Dota once on each account first.")
            return
        self.from_box.current(0)
        self.to_box.current(min(1, len(labels) - 1))
        self.log(f"Found {len(self.accounts)} account(s) with Dota 2 settings.")

    def swap(self):
        a, b = self.from_box.current(), self.to_box.current()
        if a >= 0 and b >= 0:
            self.from_box.current(b)
            self.to_box.current(a)

    def do_copy(self):
        a, b = self.from_box.current(), self.to_box.current()
        if a < 0 or b < 0:
            messagebox.showerror(APP_NAME, "Pick both accounts first.")
            return
        src, dst = self.accounts[a], self.accounts[b]
        if src.id == dst.id:
            messagebox.showerror(APP_NAME, "FROM and TO are the same account.")
            return
        if is_dota_running():
            messagebox.showerror(APP_NAME, "Dota 2 is running. Close it completely, then try again.")
            return
        categories = [c for c, var in self.category_vars.items() if var.get()]
        if not categories:
            messagebox.showerror(APP_NAME, "Tick at least one thing to copy.")
            return
        if not messagebox.askokcancel(
            "Confirm",
            f"Copy Dota 2 settings\n\nFROM: {src.label}\nTO: {dst.label}\n\n"
            "The TO account's current settings will be backed up first.",
        ):
            return

        self.copy_button.state(["disabled"])
        try:
            self.log("")
            result = copy_settings(src, dst, categories, self.backup_root, log=self.log)
            self.log("")
            if result.failed == 0:
                self.log(f"Done. {result.copied} file(s) copied and verified.")
                self.log(f"Launch Dota on {dst.name}. If Steam asks about a Cloud conflict, pick LOCAL files.")
                messagebox.showinfo(
                    APP_NAME,
                    f"Done! {result.copied} file(s) copied.\n\n"
                    "If Steam shows a Cloud Sync Conflict, choose the LOCAL files.",
                )
            else:
                self.log(f"Finished with {result.failed} failure(s). Backup is at: {result.backup}")
                messagebox.showerror(
                    APP_NAME, f"{result.failed} file(s) failed to copy. See the log.\nBackup is at:\n{result.backup}"
                )
        except Exception as e:  # Surface any failure in the UI instead of crashing.
            self.log(f"ERROR: {e}")
            messagebox.showerror(APP_NAME, str(e))
        finally:
            self.copy_button.state(["!disabled"])

    def do_restore(self):
        if is_dota_running():
            messagebox.showerror(APP_NAME, "Dota 2 is running. Close it completely, then try again.")
            return
        if not self.backup_root.is_dir():
            messagebox.showerror(APP_NAME, f"No backups yet ({self.backup_root}).")
            return
        folder = filedialog.askdirectory(
            title="Pick a backup folder to restore (named <accountid>_<name>_<date>)",
            initialdir=str(self.backup_root),
        )
        if not folder:
            return
        account_id = backup_account_id(folder)
        if not account_id:
            messagebox.showerror(APP_NAME, "That does not look like a backup folder made by this app.")
            return
        account = next((a for a in self.accounts if a.id == account_id), None)
        if not account:
            messagebox.showerror(APP_NAME, f"Account {account_id} was not found on this computer.")
            return
        if not messagebox.askokcancel("Confirm restore", f"Restore this backup onto {account.label}?\n\n{folder}"):
            return
        try:
            self.log("")
            self.log(f"Restoring {Path(folder).name} -> {account.name}...")
            restore_backup(folder, account)
            self.log("Restore complete.")
            messagebox.showinfo(APP_NAME, "Backup restored.")
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror(APP_NAME, str(e))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--steam-path", type=Path, help="Steam install folder (auto-detected if omitted)")
    parser.add_argument("--backup-dir", type=Path, help="Where backups are saved (default: Documents/DotaSettingsBackups)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # parse_known_args: macOS may pass extra arguments (e.g. -psn_...) to bundled apps.
    args, _ = parser.parse_known_args(argv)

    if tk is None:
        sys.exit(
            "Tkinter is not installed.\n"
            "  Ubuntu/Debian: sudo apt install python3-tk\n"
            "  macOS (Homebrew): brew install python-tk"
        )
    root = tk.Tk()
    App(root, steam_path=args.steam_path, backup_root=args.backup_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
