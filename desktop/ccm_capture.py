#!/usr/bin/env python3
"""CCM Quick Capture — macOS menubar app for screenshot-to-task creation."""

import json
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser

import requests
import rumps
from PIL import Image, ImageTk
from pynput import keyboard

CONFIG_PATH = os.path.expanduser("~/.ccm_capture.json")
DEFAULT_SERVER = "https://xiaoyu.claude-code-manager.com"


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {"server_url": DEFAULT_SERVER, "auth_token": ""}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def api_headers(token):
    return {"Authorization": f"Bearer {token}"}


def fetch_projects(cfg):
    resp = requests.get(
        f"{cfg['server_url']}/api/projects",
        headers=api_headers(cfg["auth_token"]),
        timeout=10,
    )
    resp.raise_for_status()
    return [p for p in resp.json() if p.get("show_in_selector", True)]


def fetch_system_config(cfg):
    try:
        resp = requests.get(
            f"{cfg['server_url']}/api/system/config",
            headers=api_headers(cfg["auth_token"]),
            timeout=10,
        )
        if resp.ok:
            return resp.json()
    except Exception:
        pass
    return {}


def upload_image(cfg, path):
    with open(path, "rb") as f:
        resp = requests.post(
            f"{cfg['server_url']}/api/uploads",
            headers=api_headers(cfg["auth_token"]),
            files={"files": ("capture.png", f, "image/png")},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()


def create_task(cfg, data):
    resp = requests.post(
        f"{cfg['server_url']}/api/tasks",
        headers={**api_headers(cfg["auth_token"]), "Content-Type": "application/json"},
        json=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------

def show_settings():
    cfg = load_config()

    win = tk.Tk()
    win.title("CCM Quick Capture — Settings")
    win.geometry("420x220")
    win.resizable(False, False)
    win.attributes("-topmost", True)

    frame = ttk.Frame(win, padding=20)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="Server URL").grid(row=0, column=0, sticky="w", pady=(0, 4))
    url_var = tk.StringVar(value=cfg.get("server_url", DEFAULT_SERVER))
    ttk.Entry(frame, textvariable=url_var, width=48).grid(row=1, column=0, sticky="ew", pady=(0, 12))

    ttk.Label(frame, text="Auth Token").grid(row=2, column=0, sticky="w", pady=(0, 4))
    token_var = tk.StringVar(value=cfg.get("auth_token", ""))
    ttk.Entry(frame, textvariable=token_var, width=48, show="•").grid(row=3, column=0, sticky="ew", pady=(0, 16))

    status_var = tk.StringVar()
    status_label = ttk.Label(frame, textvariable=status_var, foreground="gray")
    status_label.grid(row=5, column=0, sticky="w", pady=(8, 0))

    def do_save():
        new_cfg = {
            "server_url": url_var.get().strip().rstrip("/"),
            "auth_token": token_var.get().strip(),
        }
        save_config(new_cfg)
        status_var.set("Saved!")

    def do_test():
        server = url_var.get().strip().rstrip("/")
        token = token_var.get().strip()
        if not server or not token:
            status_var.set("Please fill in both fields.")
            return
        try:
            resp = requests.get(
                f"{server}/api/system/stats",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.ok:
                status_var.set("Connected successfully!")
                status_label.config(foreground="green")
            elif resp.status_code == 401:
                status_var.set("Auth failed. Check token.")
                status_label.config(foreground="red")
            else:
                status_var.set(f"Server returned {resp.status_code}")
                status_label.config(foreground="red")
        except Exception as e:
            status_var.set(f"Cannot reach server: {e}")
            status_label.config(foreground="red")

    btn_frame = ttk.Frame(frame)
    btn_frame.grid(row=4, column=0, sticky="ew")
    ttk.Button(btn_frame, text="Save", command=do_save).pack(side="left", padx=(0, 8))
    ttk.Button(btn_frame, text="Test Connection", command=do_test).pack(side="left")

    frame.columnconfigure(0, weight=1)
    win.mainloop()


# ---------------------------------------------------------------------------
# Capture form window
# ---------------------------------------------------------------------------

def show_capture_form(image_path):
    cfg = load_config()
    if not cfg.get("auth_token"):
        rumps.notification("CCM Quick Capture", "", "Please configure settings first.")
        show_settings()
        return

    try:
        projects = fetch_projects(cfg)
    except Exception as e:
        rumps.notification("CCM Quick Capture", "Error", f"Cannot fetch projects: {e}")
        return

    if not projects:
        rumps.notification("CCM Quick Capture", "", "No projects available.")
        return

    sys_config = fetch_system_config(cfg)

    win = tk.Tk()
    win.title("Quick Capture — Create Task")
    win.geometry("460x480")
    win.resizable(False, False)
    win.attributes("-topmost", True)

    frame = ttk.Frame(win, padding=16)
    frame.pack(fill="both", expand=True)

    # Screenshot preview
    try:
        img = Image.open(image_path)
        ratio = min(420 / img.width, 160 / img.height, 1.0)
        thumb = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        photo = ImageTk.PhotoImage(thumb)
        img_label = ttk.Label(frame, image=photo)
        img_label.image = photo
        img_label.grid(row=0, column=0, sticky="w", pady=(0, 12))
    except Exception:
        ttk.Label(frame, text="[Screenshot captured]").grid(row=0, column=0, sticky="w", pady=(0, 12))

    # Description
    ttk.Label(frame, text="Prompt").grid(row=1, column=0, sticky="w", pady=(0, 4))
    desc_text = tk.Text(frame, height=5, width=52, wrap="word")
    desc_text.grid(row=2, column=0, sticky="ew", pady=(0, 12))
    desc_text.focus_set()

    # Project selector
    ttk.Label(frame, text="Project").grid(row=3, column=0, sticky="w", pady=(0, 4))
    project_names = [p["name"] for p in projects]
    project_var = tk.StringVar()
    project_combo = ttk.Combobox(
        frame, textvariable=project_var, values=project_names,
        state="readonly", width=50,
    )
    project_combo.grid(row=4, column=0, sticky="ew", pady=(0, 16))

    # Status
    status_var = tk.StringVar()
    status_label = ttk.Label(frame, textvariable=status_var, foreground="gray")
    status_label.grid(row=6, column=0, sticky="w", pady=(8, 0))

    def do_create():
        desc = desc_text.get("1.0", "end").strip()
        proj_name = project_var.get()
        if not desc:
            status_var.set("Please enter a prompt.")
            status_label.config(foreground="red")
            return
        if not proj_name:
            status_var.set("Please select a project.")
            status_label.config(foreground="red")
            return

        project = next((p for p in projects if p["name"] == proj_name), None)
        if not project:
            return

        create_btn.config(state="disabled")
        status_var.set("Creating...")
        status_label.config(foreground="gray")
        win.update()

        try:
            results = upload_image(cfg, image_path)
            file_paths = [r["path"] for r in results]
            attachments = [
                {"url": r["url"], "name": r.get("filename") or "capture.png", "is_image": r.get("is_image", True)}
                for r in results
            ]

            default_model = sys_config.get("default_model", "claude-opus-4-6")

            task = create_task(cfg, {
                "description": desc,
                "project_id": project["id"],
                "priority": 0,
                "mode": "auto",
                "provider": "claude",
                "model": default_model,
                "file_paths": file_paths,
                "attachments": attachments,
            })

            win.destroy()
            webbrowser.open(f"{cfg['server_url']}#/tasks/chat/{task['id']}")
            rumps.notification("CCM Quick Capture", "", f"Task #{task['id']} created!")

        except Exception as e:
            status_var.set(f"Error: {e}")
            status_label.config(foreground="red")
            create_btn.config(state="normal")

    create_btn = ttk.Button(frame, text="Create Task", command=do_create)
    create_btn.grid(row=5, column=0, sticky="ew")

    frame.columnconfigure(0, weight=1)

    # Clean up temp file on close
    def on_close():
        try:
            os.unlink(image_path)
        except OSError:
            pass
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.mainloop()


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class CCMCaptureApp(rumps.App):
    def __init__(self):
        super().__init__("CCM", title="\U0001f4f7")  # 📷

    @rumps.clicked("Capture & Create Task")
    def on_capture(self, _):
        self._do_capture()

    @rumps.clicked("Settings...")
    def on_settings(self, _):
        threading.Thread(target=show_settings, daemon=True).start()

    def _do_capture(self):
        tmp = f"/tmp/ccm_capture_{int(time.time() * 1000)}.png"
        result = subprocess.run(["screencapture", "-i", tmp])
        if result.returncode != 0 or not os.path.exists(tmp):
            return
        threading.Thread(target=show_capture_form, args=(tmp,), daemon=True).start()


def main():
    app = CCMCaptureApp()

    # Global hotkey: Cmd+Shift+S
    def on_hotkey():
        app._do_capture()

    hotkey = keyboard.GlobalHotKeys({"<cmd>+<shift>+s": on_hotkey})
    hotkey.start()

    app.run()


if __name__ == "__main__":
    main()
