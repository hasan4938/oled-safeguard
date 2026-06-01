#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OLED Safe-Guard: Hintergrunddienst und Kontrollzentrum für dynamische Alterungskompensation.
Entwickelt für Linux X11-Systeme mit minimaler Ressourcenbelastung.
"""

import os
import sys
import json
import time
import socket
import threading
import io
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageDraw

# X11 Libraries
from Xlib import display, X
from Xlib.ext import shape

# Konfigurationspfade
CONFIG_DIR = Path.home() / ".config" / "oled-safeguard"
CONFIG_FILE = CONFIG_DIR / "config.json"
WEAR_MAP_FILE = CONFIG_DIR / "wear_map.json"

# Standardwerte
DEFAULT_CONFIG = {
    "tracking_interval_seconds": 60,
    "aging_speed": 0.0001,      # Multiplikator für Abnutzung (Simulationssteuerung)
    "max_dimming_percent": 10,  # Maximale Dämpfung der gesunden Pixel
    "compensation_enabled": True,
    "grid_cols": 32,
    "grid_rows": 18,
    "idle_dimming_enabled": True,
    "idle_timeout_seconds": 60,
    "idle_dim_percent": 60
}

# Single-Instance Port
PORT = 49152

class OLEDModel:
    """Verwaltet das mathematische Modell der OLED-Pixelabnutzung und Helligkeitsberechnung."""
    def __init__(self):
        self.config = DEFAULT_CONFIG.copy()
        self.wear_map = []
        self.lock = threading.Lock()
        self.running = False
        
        self.load_config()
        self.load_wear_map()

    def load_config(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r") as f:
                    user_conf = json.load(f)
                    self.config.update(user_conf)
            else:
                self.save_config()
        except Exception as e:
            print(f"Fehler beim Laden der Konfiguration: {e}")

    def save_config(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f"Fehler beim Speichern der Konfiguration: {e}")

    def load_wear_map(self):
        rows = self.config["grid_rows"]
        cols = self.config["grid_cols"]
        try:
            if WEAR_MAP_FILE.exists():
                with open(WEAR_MAP_FILE, "r") as f:
                    self.wear_map = json.load(f)
                # Validierung der Dimensionen
                if len(self.wear_map) != rows or any(len(row) != cols for row in self.wear_map):
                    raise ValueError("Ungültige Dimensionen")
            else:
                self.reset_wear_map()
        except Exception as e:
            print(f"Fehler beim Laden der Abnutzungskarte, setze zurück: {e}")
            self.reset_wear_map()

    def save_wear_map(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(WEAR_MAP_FILE, "w") as f:
                json.dump(self.wear_map, f)
        except Exception as e:
            print(f"Fehler beim Speichern der Abnutzungskarte: {e}")

    def reset_wear_map(self):
        with self.lock:
            rows = self.config["grid_rows"]
            cols = self.config["grid_cols"]
            self.wear_map = [[0.0 for _ in range(cols)] for _ in range(rows)]
        self.save_wear_map()


class OLEDDaemon:
    """Hintergrund-Daemon für Bildschirm-Sampling und Kompensations-Berechnungen."""
    def __init__(self, model, overlay_manager):
        self.model = model
        self.overlay_manager = overlay_manager
        self.thread = None
        self.display = None
        self.root = None

    def start(self):
        if self.model.running:
            return
        self.model.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.model.running = False

    def _loop(self):
        # Verbindung zum X-Server herstellen
        try:
            self.display = display.Display()
            self.root = self.display.screen().root
        except Exception as e:
            print(f"Fehler bei X11-Verbindung im Daemon: {e}")
            self.model.running = False
            return

        print("OLED Safe-Guard Daemon erfolgreich gestartet.")
        while self.model.running:
            t_start = time.time()
            self._sample_screen()
            
            # Aktualisiere das Overlay, falls aktiv
            if self.model.config["compensation_enabled"]:
                self.overlay_manager.update_overlay()
            else:
                self.overlay_manager.hide_overlay()

            # Berechne verbleibende Schlafzeit
            interval = self.model.config["tracking_interval_seconds"]
            t_elapsed = time.time() - t_start
            sleep_time = max(0.1, interval - t_elapsed)
            
            # Segmentierter Schlaf für schnelleres Beenden
            for _ in range(int(sleep_time * 2)):
                if not self.model.running:
                    break
                time.sleep(0.5)

    def _sample_screen(self):
        try:
            width = self.display.screen().width_in_pixels
            height = self.display.screen().height_in_pixels
            
            # Nativer X11 Speicher-Grab (extrem schnell, in-process)
            raw_img = self.root.get_image(0, 0, width, height, X.ZPixmap, 0xffffffff)
            if not raw_img or not raw_img.data:
                return

            # Konvertierung und Herunterskalierung via PIL
            img = Image.frombytes("RGB", (width, height), raw_img.data, "raw", "BGRX")
            
            cols = self.model.config["grid_cols"]
            rows = self.model.config["grid_rows"]
            img_small = img.resize((cols, rows), Image.Resampling.BILINEAR)
            
            # Grayscale Luminanz berechnen und akkumulieren
            pixels = img_small.load()
            delta_hours = self.model.config["tracking_interval_seconds"] / 3600.0
            aging_speed = self.model.config["aging_speed"]
            
            with self.model.lock:
                for y in range(rows):
                    for x in range(cols):
                        r, g, b = pixels[x, y]
                        # Relative Helligkeit (Y) nach ITU-R BT.601
                        y_val = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
                        # Stress = Helligkeit * Zeit * Alterungsfaktor
                        stress = y_val * delta_hours * aging_speed
                        self.model.wear_map[y][x] += stress
                        # Obergrenze für Abnutzung (max 50% physikalischer Helligkeitsverlust)
                        if self.model.wear_map[y][x] > 0.5:
                            self.model.wear_map[y][x] = 0.5

            self.model.save_wear_map()
            
        except Exception as e:
            print(f"Fehler beim Bildschirm-Sampling: {e}")


class OverlayManager:
    """Steuert das rahmenlose, transparente Click-Through Overlay."""
    def __init__(self, root, model):
        self.root = root
        self.model = model
        self.overlay_win = None
        self.display = None
        self.is_idle = False
        
        # Starte die regelmäßige Inaktivitätsprüfung
        self.root.after(1000, self._poll_idle)

    def update_overlay(self):
        if not self.model.config["compensation_enabled"]:
            self.hide_overlay()
            return
        if self.is_idle:
            return

        self.root.after(0, self._sync_overlay)

    def _sync_overlay(self):
        try:
            if not self.display:
                self.display = display.Display()

            width = self.display.screen().width_in_pixels
            height = self.display.screen().height_in_pixels
            
            rows = self.model.config["grid_rows"]
            cols = self.model.config["grid_cols"]
            
            # Berechne Dämpfungswerte für jeden Block
            with self.model.lock:
                # Physikalisches L_max: 1.0 - Abnutzung
                l_max = [[1.0 - self.model.wear_map[y][x] for x in range(cols)] for y in range(rows)]
            
            # Finde die dunkelste Stelle des Bildschirms
            min_l_max = min(min(row) for row in l_max)
            
            # Sicherheitsbegrenzung (Dämpfungsschutz)
            max_dimming = self.model.config["max_dimming_percent"] / 100.0
            target_l = max(min_l_max, 1.0 - max_dimming)
            
            # Wenn fast keine Abnutzung vorhanden ist (< 0.5% Abweichung), blende Overlay aus
            if target_l > 0.995:
                self.hide_overlay()
                return

            # Dämpfungsmaske berechnen
            # 0.0 = gesund (maximal dämpfen = target_l)
            # > 0.0 = abgenutzt (weniger dämpfen)
            dim_map = [[0.0 for _ in range(cols)] for _ in range(rows)]
            for y in range(rows):
                for x in range(cols):
                    # Erforderliche Dämpfung um Helligkeit auf target_l anzugleichen
                    dim_needed = 1.0 - (target_l / l_max[y][x])
                    dim_map[y][x] = max(0.0, dim_needed)

            # Maximaler Dämpfungswert auf dem Bildschirm
            max_dim = max(max(row) for row in dim_map)
            if max_dim <= 0.005:
                self.hide_overlay()
                return

            # Erstelle das Overlay-Fenster falls erforderlich
            if not self.overlay_win:
                self.overlay_win = tk.Toplevel(self.root)
                self.overlay_win.overrideredirect(True)
                self.overlay_win.geometry(f"{width}x{height}+0+0")
                self.overlay_win.attributes("-topmost", True)
                self.overlay_win.config(bg="black")
                
                # Input-Shape leeren -> Fenster wird zu 100% Click-Through!
                self.overlay_win.update()
                window_id = int(self.overlay_win.wm_frame(), 16)
                xwin = self.display.create_resource_object('window', window_id)
                shape.rectangles(xwin, shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
                self.display.flush()

            # Setze die Gesamt-Fenster-Opacity auf das Maximum der benötigten Dämpfung
            self.overlay_win.attributes("-alpha", max_dim)

            # Maskierung der abgenutzten Zonen via X11 Bounding Shape
            # Wir dämpfen nur dort, wo der Helligkeitsverlust noch gering ist.
            # Stark abgenutzte Zonen werden aus dem Overlay "herausgeschnitten", damit dort
            # das Originallicht mit voller Helligkeit durchscheint!
            rects = []
            block_w = width // cols
            block_h = height // rows
            
            for y in range(rows):
                for x in range(cols):
                    # Wenn die benötigte Dämpfung nahe dem Maximum liegt, ist das Pixel gesund
                    # -> Dämpfungsfenster muss hier existieren
                    # Wenn die benötigte Dämpfung klein ist, ist das Pixel abgenutzt
                    # -> Dämpfungsfenster wird hier weggeschnitten (>50% Abnutzungsschwelle relativ)
                    if max_dim > 0 and dim_map[y][x] > 0.3 * max_dim:
                        rx = x * block_w
                        ry = y * block_h
                        rects.append((rx, ry, block_w, block_h))

            # Bounding Shape setzen
            self.overlay_win.update()
            window_id = int(self.overlay_win.wm_frame(), 16)
            xwin = self.display.create_resource_object('window', window_id)
            if rects:
                shape.rectangles(xwin, shape.SO.Set, shape.SK.Bounding, 0, 0, 0, rects)
            else:
                # Wenn keine Rechtecke vorhanden sind, verstecke das Fenster komplett
                shape.rectangles(xwin, shape.SO.Set, shape.SK.Bounding, 0, 0, 0, [])
            self.display.flush()

        except Exception as e:
            print(f"Fehler im OverlayManager beim Zeichnen: {e}")

    def hide_overlay(self):
        if self.overlay_win:
            try:
                self.overlay_win.destroy()
            except Exception:
                pass
            self.overlay_win = None

    def _poll_idle(self):
        try:
            if not self.display:
                self.display = display.Display()
                
            config_enabled = self.model.config.get("idle_dimming_enabled", True)
            if config_enabled and self.display.has_extension('MIT-SCREEN-SAVER'):
                info = self.display.screen().root.screensaver_query_info()
                idle_sec = info.idle / 1000.0
                timeout = self.model.config.get("idle_timeout_seconds", 60)
                
                if idle_sec >= timeout:
                    if not self.is_idle:
                        self.is_idle = True
                        print("System im Leerlauf. Aktiviere Inaktivitäts-Dimmer.")
                    self._apply_idle_dimming()
                else:
                    if self.is_idle:
                        self.is_idle = False
                        print("Aktivität erkannt. Deaktiviere Inaktivitäts-Dimmer.")
                        self.update_overlay()
            else:
                if self.is_idle:
                    self.is_idle = False
                    self.update_overlay()
        except Exception as e:
            print(f"Fehler bei Inaktivitätsprüfung: {e}")
            
        self.root.after(1000, self._poll_idle)

    def _apply_idle_dimming(self):
        try:
            width = self.display.screen().width_in_pixels
            height = self.display.screen().height_in_pixels
            
            if not self.overlay_win:
                self.overlay_win = tk.Toplevel(self.root)
                self.overlay_win.overrideredirect(True)
                self.overlay_win.geometry(f"{width}x{height}+0+0")
                self.overlay_win.attributes("-topmost", True)
                self.overlay_win.config(bg="black")
                
                self.overlay_win.update()
                window_id = int(self.overlay_win.wm_frame(), 16)
                xwin = self.display.create_resource_object('window', window_id)
                shape.rectangles(xwin, shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
                self.display.flush()

            # Setze Bounding Shape auf den vollen Bildschirm (gesamtes Fenster dimmen)
            self.overlay_win.update()
            window_id = int(self.overlay_win.wm_frame(), 16)
            xwin = self.display.create_resource_object('window', window_id)
            shape.rectangles(xwin, shape.SO.Set, shape.SK.Bounding, 0, 0, 0, [(0, 0, width, height)])
            self.display.flush()

            # Helligkeit dämpfen
            idle_dim = self.model.config.get("idle_dim_percent", 60) / 100.0
            self.overlay_win.attributes("-alpha", idle_dim)
            
        except Exception as e:
            print(f"Fehler beim Anwenden des Inaktivitäts-Dimmers: {e}")


class ControlGUI:
    """Edles Premium Dark Mode Kontrollzentrum."""
    def __init__(self, root, model, daemon, overlay_manager):
        self.root = root
        self.model = model
        self.daemon = daemon
        self.overlay_manager = overlay_manager
        
        self.setup_theme()
        self.create_layout()
        self.update_loop()

    def setup_theme(self):
        self.root.title("OLED Safe-Guard - Kontrollzentrum")
        self.root.geometry("850x550")
        self.root.minsize(800, 500)
        self.root.config(bg="#121212")

        # Premium Dark-Theme Styles
        self.style = ttk.Style()
        self.style.theme_use("clam")
        
        # Globale Farben
        self.bg_dark = "#121212"
        self.bg_card = "#1e1e1e"
        self.accent_cyan = "#00f2fe"
        self.text_light = "#e0e0e0"
        self.text_dim = "#888888"

        self.style.configure(".", background=self.bg_dark, foreground=self.text_light, font=("Inter", 10))
        self.style.configure("TFrame", background=self.bg_dark)
        self.style.configure("Card.TFrame", background=self.bg_card, relief="flat", borderwidth=0)
        
        # Tabs
        self.style.configure("TNotebook", background=self.bg_dark, borderwidth=0)
        self.style.configure("TNotebook.Tab", background=self.bg_card, foreground=self.text_dim, padding=[15, 8], font=("Outfit", 10, "bold"), borderwidth=0)
        self.style.map("TNotebook.Tab", background=[("selected", self.accent_cyan)], foreground=[("selected", "#000000")])
        
        # Buttons
        self.style.configure("TButton", background=self.bg_card, foreground=self.text_light, borderwidth=1, bordercolor="#333333", padding=[12, 6], font=("Outfit", 9, "bold"))
        self.style.map("TButton", background=[("active", "#2a2a2a")], foreground=[("active", self.accent_cyan)])
        self.style.configure("Accent.TButton", background=self.accent_cyan, foreground="#000000", borderwidth=0, padding=[12, 6], font=("Outfit", 9, "bold"))
        self.style.map("Accent.TButton", background=[("active", "#00d2de")])
        
        # Labels
        self.style.configure("Header.TLabel", background=self.bg_dark, foreground=self.text_light, font=("Outfit", 18, "bold"))
        self.style.configure("SubHeader.TLabel", background=self.bg_card, foreground=self.accent_cyan, font=("Outfit", 11, "bold"))
        self.style.configure("StatVal.TLabel", background=self.bg_card, foreground=self.text_light, font=("Outfit", 24, "bold"))
        self.style.configure("StatLbl.TLabel", background=self.bg_card, foreground=self.text_dim, font=("Inter", 9))
        self.style.configure("CardText.TLabel", background=self.bg_card, foreground=self.text_light)
        self.style.configure("TCheckbutton", background=self.bg_card, foreground=self.text_light, font=("Inter", 10))
        self.style.map("TCheckbutton", background=[("active", self.bg_card)], foreground=[("active", self.accent_cyan)])

    def create_layout(self):
        # Top Header
        header_frame = ttk.Frame(self.root)
        header_frame.pack(fill="x", padx=25, pady=20)
        
        header_lbl = ttk.Label(header_frame, text="OLED SAFE-GUARD", style="Header.TLabel")
        header_lbl.pack(side="left")
        
        self.status_indicator = tk.Canvas(header_frame, width=12, height=12, bg=self.bg_dark, highlightthickness=0)
        self.status_indicator.pack(side="left", padx=(15, 5))
        self.status_lbl = ttk.Label(header_frame, text="Aktiv", foreground="#00ff88", font=("Outfit", 10, "bold"))
        self.status_lbl.pack(side="left")

        # Main Navigation Tabs
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=25, pady=(0, 25))
        
        # Tab 1: Dashboard
        tab_dash = ttk.Frame(notebook)
        notebook.add(tab_dash, text=" DASHBOARD ")
        self.build_dashboard(tab_dash)
        
        # Tab 2: Wear Map
        tab_map = ttk.Frame(notebook)
        notebook.add(tab_map, text=" ABNUTZUNGS-KARTE ")
        self.build_wear_map(tab_map)
        
        # Tab 3: Settings
        tab_settings = ttk.Frame(notebook)
        notebook.add(tab_settings, text=" EINSTELLUNGEN ")
        self.build_settings(tab_settings)

        # Tab 4: Tools
        tab_tools = ttk.Frame(notebook)
        notebook.add(tab_tools, text=" WERKZEUGE ")
        self.build_tools(tab_tools)

    def build_dashboard(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(1, weight=1)
        
        # Stat Card 1: Homogenität
        card1 = ttk.Frame(parent, style="Card.TFrame")
        card1.grid(row=0, column=0, padx=(0, 10), pady=(10, 15), sticky="nsew")
        
        ttk.Label(card1, text="Homogenitäts-Index", style="StatLbl.TLabel").pack(anchor="w", padx=15, pady=(15, 2))
        self.lbl_homogeneity = ttk.Label(card1, text="100.0%", style="StatVal.TLabel")
        self.lbl_homogeneity.pack(anchor="w", padx=15, pady=(0, 15))

        # Stat Card 2: Maximale Abnutzung
        card2 = ttk.Frame(parent, style="Card.TFrame")
        card2.grid(row=0, column=1, padx=(10, 0), pady=(10, 15), sticky="nsew")
        
        ttk.Label(card2, text="Maximale Pixelabnutzung", style="StatLbl.TLabel").pack(anchor="w", padx=15, pady=(15, 2))
        self.lbl_max_wear = ttk.Label(card2, text="0.00%", style="StatVal.TLabel")
        self.lbl_max_wear.pack(anchor="w", padx=15, pady=(0, 15))

        # Card 3: Steuerkonsole & Info
        card3 = ttk.Frame(parent, style="Card.TFrame")
        card3.grid(row=1, column=0, columnspan=2, pady=(5, 10), sticky="nsew")
        
        ttk.Label(card3, text="Systemsteuerung", style="SubHeader.TLabel").pack(anchor="w", padx=20, pady=(20, 10))
        
        # Info Text
        desc_text = (
            "Dieser Hintergrundprozess gleicht physikalische Pixelabnutzungen (Einbrenneffekte) aus.\n"
            "Das System analysiert Ihren Bildschirm alle 60 Sekunden unauffällig im Speicher und dimmt gesunde\n"
            "Bereiche gezielt und unmerklich herunter, um eine absolut homogene Gesamthelligkeit zu garantieren."
        )
        ttk.Label(card3, text=desc_text, style="CardText.TLabel", justify="left").pack(anchor="w", padx=20, pady=(0, 20))
        
        # Buttons Frame
        btn_frame = ttk.Frame(card3, style="Card.TFrame")
        btn_frame.pack(fill="x", padx=20, pady=(0, 20))
        
        self.btn_toggle = ttk.Button(btn_frame, text="Kompensation Deaktivieren", command=self.toggle_compensation, style="Accent.TButton")
        self.btn_toggle.pack(side="left", padx=(0, 10))
        
        self.btn_hide = ttk.Button(btn_frame, text="In den Hintergrund minimieren", command=self.hide_to_background)
        self.btn_hide.pack(side="left")

    def build_wear_map(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        
        card = ttk.Frame(parent, style="Card.TFrame")
        card.grid(row=0, column=0, pady=10, sticky="nsew")
        
        ttk.Label(card, text="Visuelle Abnutzungskarte (Heat-Map)", style="SubHeader.TLabel").pack(anchor="w", padx=20, pady=(15, 5))
        ttk.Label(card, text="Visuelle Darstellung der thermischen Belastung. Dunkel = Gesund | Hell/Rot/Gelb = Statische Zonen", style="StatLbl.TLabel").pack(anchor="w", padx=20, pady=(0, 15))
        
        # Heatmap Canvas
        self.map_canvas = tk.Canvas(card, bg="#080808", highlightthickness=1, highlightbackground="#333333")
        self.map_canvas.pack(fill="both", expand=True, padx=20, pady=(0, 20))

    def build_settings(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        
        card = ttk.Frame(parent, style="Card.TFrame")
        card.grid(row=0, column=0, pady=10, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)
        
        # Header
        ttk.Label(card, text="Dienst-Einstellungen", style="SubHeader.TLabel").grid(row=0, column=0, columnspan=2, padx=20, pady=(20, 10), sticky="w")

        # Linke Spalte (Kern-Einstellungen)
        left_col = ttk.Frame(card, style="Card.TFrame")
        left_col.grid(row=1, column=0, padx=(20, 10), pady=10, sticky="nsew")
        
        ttk.Label(left_col, text="Kern-Einstellungen", font=("Outfit", 12, "bold"), background=self.bg_card, foreground=self.accent_cyan).pack(anchor="w", pady=(0, 10))

        # Slider 1: Sampling-Intervall
        ttk.Label(left_col, text="Abtastungs-Intervall (Sekunden):", style="CardText.TLabel").pack(anchor="w", pady=(5, 2))
        self.val_interval = tk.IntVar(value=self.model.config["tracking_interval_seconds"])
        self.lbl_interval_num = ttk.Label(left_col, text=f"{self.val_interval.get()}s", style="CardText.TLabel", foreground=self.accent_cyan)
        self.lbl_interval_num.pack(anchor="w")
        self.slider_interval = ttk.Scale(left_col, from_=5, to=300, variable=self.val_interval, orient="horizontal", command=lambda e: self.lbl_interval_num.config(text=f"{self.val_interval.get()}s"))
        self.slider_interval.pack(fill="x", pady=(0, 15))

        # Slider 2: Simulations-Geschwindigkeit
        ttk.Label(left_col, text="Alterungs-Geschwindigkeit (Simulationstest):", style="CardText.TLabel").pack(anchor="w", pady=(5, 2))
        self.val_speed = tk.DoubleVar(value=self.model.config["aging_speed"])
        self.lbl_speed_num = ttk.Label(left_col, text=f"{self.val_speed.get():.6f} (Echtzeit ≈ 0.000100)", style="CardText.TLabel", foreground=self.accent_cyan)
        self.lbl_speed_num.pack(anchor="w")
        self.slider_speed = ttk.Scale(left_col, from_=0.0001, to=10.0, variable=self.val_speed, orient="horizontal", command=lambda e: self.lbl_speed_num.config(text=f"{self.val_speed.get():.6f} (Simulation)" if self.val_speed.get() > 0.005 else f"{self.val_speed.get():.6f} (Echtzeit)"))
        self.slider_speed.pack(fill="x", pady=(0, 15))

        # Slider 3: Maximale Helligkeitsdämpfung
        ttk.Label(left_col, text="Maximale Helligkeitsdämpfung (%):", style="CardText.TLabel").pack(anchor="w", pady=(5, 2))
        self.val_max_dim = tk.IntVar(value=self.model.config["max_dimming_percent"])
        self.lbl_max_dim_num = ttk.Label(left_col, text=f"{self.val_max_dim.get()}%", style="CardText.TLabel", foreground=self.accent_cyan)
        self.lbl_max_dim_num.pack(anchor="w")
        self.slider_max_dim = ttk.Scale(left_col, from_=1, to=30, variable=self.val_max_dim, orient="horizontal", command=lambda e: self.lbl_max_dim_num.config(text=f"{self.val_max_dim.get()}%"))
        self.slider_max_dim.pack(fill="x", pady=(0, 15))

        # Rechte Spalte (Inaktivitäts-Dimmer)
        right_col = ttk.Frame(card, style="Card.TFrame")
        right_col.grid(row=1, column=1, padx=(10, 20), pady=10, sticky="nsew")

        ttk.Label(right_col, text="Inaktivitäts-Dimmer (Bildschirmschoner)", font=("Outfit", 12, "bold"), background=self.bg_card, foreground=self.accent_cyan).pack(anchor="w", pady=(0, 10))

        # Checkbox: Aktivieren
        self.val_idle_enabled = tk.BooleanVar(value=self.model.config.get("idle_dimming_enabled", True))
        self.chk_idle = ttk.Checkbutton(right_col, text="Dimmer aktivieren", variable=self.val_idle_enabled, style="TCheckbutton")
        self.chk_idle.pack(anchor="w", pady=(5, 15))

        # Slider 4: Inaktivitäts-Timeout
        ttk.Label(right_col, text="Verzögerung bis Dimmung (Sekunden):", style="CardText.TLabel").pack(anchor="w", pady=(5, 2))
        self.val_idle_timeout = tk.IntVar(value=self.model.config.get("idle_timeout_seconds", 60))
        self.lbl_idle_timeout_num = ttk.Label(right_col, text=f"{self.val_idle_timeout.get()}s", style="CardText.TLabel", foreground=self.accent_cyan)
        self.lbl_idle_timeout_num.pack(anchor="w")
        self.slider_idle_timeout = ttk.Scale(right_col, from_=10, to=600, variable=self.val_idle_timeout, orient="horizontal", command=lambda e: self.lbl_idle_timeout_num.config(text=f"{self.val_idle_timeout.get()}s"))
        self.slider_idle_timeout.pack(fill="x", pady=(0, 15))

        # Slider 5: Inaktivitäts-Dimmstärke
        ttk.Label(right_col, text="Dimm-Stärke (% Abdunkelung):", style="CardText.TLabel").pack(anchor="w", pady=(5, 2))
        self.val_idle_dim = tk.IntVar(value=self.model.config.get("idle_dim_percent", 60))
        self.lbl_idle_dim_num = ttk.Label(right_col, text=f"{self.val_idle_dim.get()}%", style="CardText.TLabel", foreground=self.accent_cyan)
        self.lbl_idle_dim_num.pack(anchor="w")
        self.slider_idle_dim = ttk.Scale(right_col, from_=10, to=90, variable=self.val_idle_dim, orient="horizontal", command=lambda e: self.lbl_idle_dim_num.config(text=f"{self.val_idle_dim.get()}%"))
        self.slider_idle_dim.pack(fill="x", pady=(0, 15))

        # Speichern Button (unten zentriert)
        self.btn_save = ttk.Button(card, text="Einstellungen Speichern", command=self.save_settings, style="Accent.TButton")
        self.btn_save.grid(row=2, column=0, columnspan=2, padx=20, pady=20, sticky="w")

    def build_tools(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        
        card = ttk.Frame(parent, style="Card.TFrame")
        card.grid(row=0, column=0, pady=10, sticky="nsew")
        
        ttk.Label(card, text="Kalibrierung und Diagnose-Werkzeuge", style="SubHeader.TLabel").pack(anchor="w", padx=20, pady=(20, 15))

        # Button row 1
        btn_f1 = ttk.Frame(card, style="Card.TFrame")
        btn_f1.pack(fill="x", padx=20, pady=10)
        
        ttk.Button(btn_f1, text="Diagnose: Vollbild Weiß", command=self.show_calibration_slide).pack(side="left", padx=(0, 10))
        ttk.Button(btn_f1, text="Pixel-Auffrischer starten (RGB)", command=self.start_pixel_refresher).pack(side="left", padx=(0, 10))

        # Button row 2
        btn_f2 = ttk.Frame(card, style="Card.TFrame")
        btn_f2.pack(fill="x", padx=20, pady=10)
        
        ttk.Button(btn_f2, text="Abnutzungskarte zurücksetzen", command=self.reset_confirm).pack(side="left", padx=(0, 10))
        ttk.Button(btn_f2, text="Autostart aktivieren/deaktivieren", command=self.toggle_autostart).pack(side="left")

    def toggle_compensation(self):
        enabled = not self.model.config["compensation_enabled"]
        self.model.config["compensation_enabled"] = enabled
        self.model.save_config()
        
        if enabled:
            self.btn_toggle.config(text="Kompensation Deaktivieren", style="Accent.TButton")
            self.overlay_manager.update_overlay()
        else:
            self.btn_toggle.config(text="Kompensation Aktivieren", style="TButton")
            self.overlay_manager.hide_overlay()

    def save_settings(self):
        self.model.config["tracking_interval_seconds"] = self.val_interval.get()
        self.model.config["aging_speed"] = self.val_speed.get()
        self.model.config["max_dimming_percent"] = self.val_max_dim.get()
        self.model.config["idle_dimming_enabled"] = self.val_idle_enabled.get()
        self.model.config["idle_timeout_seconds"] = self.val_idle_timeout.get()
        self.model.config["idle_dim_percent"] = self.val_idle_dim.get()
        self.model.save_config()
        messagebox.showinfo("Erfolg", "Einstellungen wurden erfolgreich gespeichert!")

    def hide_to_background(self):
        self.root.withdraw()
        # Systemmeldung anzeigen
        try:
            from subprocess import Popen
            Popen(["notify-send", "OLED Safe-Guard", "Der Dienst läuft im Hintergrund weiter. Starten Sie das Programm erneut, um das Kontrollzentrum zu öffnen."])
        except Exception:
            pass

    def reset_confirm(self):
        if messagebox.askyesno("Zurücksetzen", "Möchten Sie die gesamte Pixelabnutzungskarte wirklich zurücksetzen?"):
            self.model.reset_wear_map()
            self.overlay_manager.hide_overlay()
            messagebox.showinfo("Erfolg", "Die Abnutzungskarte wurde zurückgesetzt.")

    def show_calibration_slide(self):
        # Öffnet ein vollflächiges weißes Fenster zum Suchen von echtem Burn-in
        slide = tk.Toplevel(self.root)
        slide.attributes("-fullscreen", True)
        slide.attributes("-topmost", True)
        
        canvas = tk.Canvas(slide, bg="white", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        
        # Info-Text über Escape
        lbl = tk.Label(canvas, text="Drücken Sie ESCAPE, um den Kalibrierungs-Bildschirm zu verlassen.", bg="white", fg="#ff3333", font=("Outfit", 12, "bold"))
        lbl.pack(pady=20)
        
        slide.bind("<Escape>", lambda e: slide.destroy())
        slide.bind("<Button-1>", lambda e: slide.destroy()) # Click to exit

    def start_pixel_refresher(self):
        # Schnelle Farbabfolge zum Löschen von temporärem Image Retention
        refresher = tk.Toplevel(self.root)
        refresher.attributes("-fullscreen", True)
        refresher.attributes("-topmost", True)
        
        canvas = tk.Canvas(refresher, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        
        colors = ["red", "green", "blue", "white", "black"]
        idx = 0
        
        def cycle():
            nonlocal idx
            if not refresher.winfo_exists():
                return
            canvas.config(bg=colors[idx])
            idx = (idx + 1) % len(colors)
            refresher.after(300, cycle)
            
        cycle()
        refresher.bind("<Escape>", lambda e: refresher.destroy())
        refresher.bind("<Button-1>", lambda e: refresher.destroy())

    def toggle_autostart(self):
        autostart_dir = Path.home() / ".config" / "autostart"
        desktop_file = autostart_dir / "oled-safeguard.desktop"
        
        try:
            autostart_dir.mkdir(parents=True, exist_ok=True)
            if desktop_file.exists():
                desktop_file.unlink()
                messagebox.showinfo("Autostart", "Autostart wurde erfolgreich deaktiviert.")
            else:
                executable = sys.executable
                script_path = Path(__file__).resolve()
                
                content = f"""[Desktop Entry]
Type=Application
Version=1.0
Name=OLED Safe-Guard
Comment=Dynamic OLED Aging Compensation Service
Exec={executable} {script_path} --daemon
Icon=display
Terminal=false
Categories=Utility;
"""
                with open(desktop_file, "w") as f:
                    f.write(content)
                desktop_file.chmod(0o755)
                messagebox.showinfo("Autostart", "Autostart wurde erfolgreich aktiviert.")
        except Exception as e:
            messagebox.showerror("Fehler", f"Autostart-Einstellung fehlgeschlagen: {e}")

    def draw_heatmap(self):
        # Hole Canvas Dimensionen
        cw = self.map_canvas.winfo_width()
        ch = self.map_canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return

        self.map_canvas.delete("all")
        
        cols = self.model.config["grid_cols"]
        rows = self.model.config["grid_rows"]
        
        block_w = cw / cols
        block_h = ch / rows
        
        with self.model.lock:
            wear = [row[:] for row in self.model.wear_map]

        max_w = max(max(row) for row in wear) if wear else 0
        
        # Thermische Farbpalette (Schwarz -> Blau -> Magenta -> Rot -> Orange -> Gelb -> Weiß)
        def get_heat_color(val):
            if max_w == 0:
                return "#000000"
            # Skaliere auf 0.0 - 1.0
            nv = val / max_w if max_w > 0 else 0
            # RGB-Mischung
            if nv < 0.2: # Dunkelblau
                r = 0
                g = 0
                b = int((nv / 0.2) * 255)
            elif nv < 0.4: # Violett
                r = int(((nv - 0.2) / 0.2) * 150)
                g = 0
                b = 255
            elif nv < 0.6: # Magenta/Rot
                r = 255
                g = 0
                b = int((1.0 - (nv - 0.4) / 0.2) * 255)
            elif nv < 0.8: # Orange
                r = 255
                g = int(((nv - 0.6) / 0.2) * 160)
                b = 0
            else: # Gelb/Weiß
                r = 255
                g = 255
                b = int(((nv - 0.8) / 0.2) * 255)
                
            return f"#{r:02x}{g:02x}{b:02x}"

        for y in range(rows):
            for x in range(cols):
                w_val = wear[y][x]
                color = get_heat_color(w_val)
                x1 = x * block_w
                y1 = y * block_h
                x2 = x1 + block_w
                y2 = y1 + block_h
                self.map_canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="#181818", width=1)

    def update_loop(self):
        # Pulsierender Aktivitäts-Indikator
        if self.model.running:
            self.status_indicator.delete("all")
            # Blinkendes Grün
            color = "#00ff88" if int(time.time() * 2) % 2 == 0 else "#00aa55"
            self.status_indicator.create_oval(2, 2, 10, 10, fill=color, outline="")
            self.status_lbl.config(text="Aktiv", foreground="#00ff88")
        else:
            self.status_indicator.delete("all")
            self.status_indicator.create_oval(2, 2, 10, 10, fill="#ff3333", outline="")
            self.status_lbl.config(text="Gestoppt", foreground="#ff3333")

        # Homogenität & Maximalabnutzung berechnen
        with self.model.lock:
            wear = [row[:] for row in self.model.wear_map]
            
        max_wear = max(max(row) for row in wear) if wear else 0
        homogeneity = 100.0 - (max_wear * 100.0)
        
        self.lbl_max_wear.config(text=f"{max_wear * 100.0:.3f}%")
        self.lbl_homogeneity.config(text=f"{homogeneity:.3f}%")

        # Aktualisiere Hitze-Karte
        self.draw_heatmap()

        # Nächstes Update planen (alle 500ms für responsive GUI)
        self.root.after(500, self.update_loop)


def start_instance_server(root):
    """Startet einen lokalen Socket-Server, um Mehrfachinstanzen zu steuern."""
    def server_thread():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', PORT))
            s.listen(1)
        except Exception:
            return  # Port besetzt (bereits aktiv)
            
        while True:
            try:
                conn, addr = s.accept()
                data = conn.recv(1024).decode('utf-8')
                if data == "show":
                    root.after(0, root.deiconify)
                    root.after(0, root.lift)
                conn.close()
            except Exception:
                break
                
    t = threading.Thread(target=server_thread, daemon=True)
    t.start()


def try_show_existing_instance():
    """Prüft, ob bereits eine Instanz läuft, und holt diese in den Vordergrund."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(('127.0.0.1', PORT))
        s.sendall(b"show")
        s.close()
        return True
    except Exception:
        return False


def main():
    # Prüfen, ob bereits ein Instanz-Server läuft
    if try_show_existing_instance():
        print("Eine Instanz von OLED Safe-Guard läuft bereits. Bringe Kontrollzentrum in den Vordergrund.")
        sys.exit(0)

    # CLI-Argumente analysieren
    run_headless = False
    if len(sys.argv) > 1 and sys.argv[1] in ["--daemon", "-d"]:
        run_headless = True

    # Root Tkinter Initialisierung (immer erforderlich, auch headless für den Main-Loop)
    root = tk.Tk()
    
    # Model und Manager initialisieren
    model = OLEDModel()
    overlay_manager = OverlayManager(root, model)
    daemon = OLEDDaemon(model, overlay_manager)
    
    # Daemon starten
    daemon.start()
    
    # Local Socket für Einmaligkeitssteuerung starten
    start_instance_server(root)

    if run_headless:
        # Im Headless-Modus verstecken wir das Hauptfenster komplett
        root.withdraw()
    else:
        # Starte die edle Kontroll-GUI
        ControlGUI(root, model, daemon, overlay_manager)

    # Hauptschleife starten
    try:
        root.mainloop()
    finally:
        # Sauberes Beenden des Daemons
        daemon.stop()


if __name__ == "__main__":
    main()
