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
import datetime
import random
import math
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
    "idle_dimming_enabled": False,
    "idle_timeout_seconds": 60,
    "idle_dim_percent": 60,
    "operating_mode": "Schutz",
    "night_dim_percent": 30,
    "night_schedule_enabled": False,
    "dithering_percent": 1,     # Dithering-Rauschen zur Verringerung von Color Banding (0-5%)
    "aging_speed_red": 0.0001,
    "aging_speed_green": 0.0001,
    "aging_speed_blue": 0.00015, # Höhere Alterungsrate für blaue OLEDs
    "idle_mode": "Dimmen",      # "Dimmen" oder "Heilung"
    "bypass_apps": "vlc,steam,mpv",
    "bypass_fullscreen": True
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
                
                # Upgrade von 2D (Grayscale) auf 3D (Sub-Pixel RGB)
                if self.wear_map and not isinstance(self.wear_map[0][0], list):
                    print("Konvertiere alte 2D-Abnutzungskarte in 3D (Sub-Pixel RGB)...")
                    self.wear_map = [[[val, val, val] for val in row] for row in self.wear_map]
                    self.save_wear_map()
                elif self.wear_map and isinstance(self.wear_map[0][0], list) and len(self.wear_map[0][0]) != 3:
                    # Sicherstellen, dass jeder Block exakt 3 Werte hat
                    self.wear_map = [[[val[0] if len(val) > 0 else 0.0, val[1] if len(val) > 1 else 0.0, val[2] if len(val) > 2 else 0.0] if isinstance(val, list) else [val, val, val] for val in row] for row in self.wear_map]
                    self.save_wear_map()
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
            self.wear_map = [[[0.0, 0.0, 0.0] for _ in range(cols)] for _ in range(rows)]
        self.save_wear_map()

    def check_night_schedule(self, current_hour):
        if self.config.get("night_schedule_enabled", False):
            is_night_time = (current_hour >= 20 or current_hour < 6)
            current_mode = self.config.get("operating_mode", "Schutz")
            if is_night_time and current_mode != "Nacht":
                self.config["operating_mode"] = "Nacht"
                self.save_config()
                return True
            elif not is_night_time and current_mode == "Nacht":
                self.config["operating_mode"] = "Schutz"
                self.save_config()
                return True
        return False


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
            
            # Automatische Zeitsteuerung prüfen
            self.model.check_night_schedule(datetime.datetime.now().hour)
            self._sample_screen()
            
            # Aktualisiere das Overlay, falls aktiv
            bypass_active = self._check_bypass_active()
            if self.model.config["compensation_enabled"] and not bypass_active:
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
            
            # Sub-Pixel RGB Luminanz berechnen und akkumulieren
            pixels = img_small.load()
            delta_hours = self.model.config["tracking_interval_seconds"] / 3600.0
            
            # Unabhängige Alterungsgeschwindigkeiten laden
            k_red = self.model.config.get("aging_speed_red", 0.0001)
            k_green = self.model.config.get("aging_speed_green", 0.0001)
            k_blue = self.model.config.get("aging_speed_blue", 0.00015)
            
            with self.model.lock:
                for y in range(rows):
                    for x in range(cols):
                        r, g, b = pixels[x, y]
                        
                        # Stress für jeden einzelnen Subpixel berechnen
                        stress_r = (r / 255.0) * delta_hours * k_red
                        stress_g = (g / 255.0) * delta_hours * k_green
                        stress_b = (b / 255.0) * delta_hours * k_blue
                        
                        # Addieren
                        self.model.wear_map[y][x][0] += stress_r
                        self.model.wear_map[y][x][1] += stress_g
                        self.model.wear_map[y][x][2] += stress_b
                        
                        # Obergrenze bei 50% physikalischem Verlust
                        for i in range(3):
                            if self.model.wear_map[y][x][i] > 0.5:
                                self.model.wear_map[y][x][i] = 0.5

            self.model.save_wear_map()
            
        except Exception as e:
            print(f"Fehler beim Bildschirm-Sampling: {e}")

    def _check_bypass_active(self):
        try:
            if not self.display:
                return False
            
            bypass_fs = self.model.config.get("bypass_fullscreen", True)
            bypass_apps_str = self.model.config.get("bypass_apps", "vlc,steam,mpv")
            bypass_apps = [a.strip().lower() for a in bypass_apps_str.split(",") if a.strip()]
            
            if not bypass_fs and not bypass_apps:
                return False
                
            active_window_atom = self.display.intern_atom('_NET_ACTIVE_WINDOW')
            response = self.root.get_full_property(active_window_atom, X.AnyPropertyType)
            if response and response.value:
                win_id = response.value[0]
                if win_id != 0:
                    win = self.display.create_resource_object('window', win_id)
                    
                    if bypass_fs:
                        state_atom = self.display.intern_atom('_NET_WM_STATE')
                        fullscreen_atom = self.display.intern_atom('_NET_WM_STATE_FULLSCREEN')
                        state_prop = win.get_full_property(state_atom, X.AnyPropertyType)
                        if state_prop and state_prop.value:
                            if fullscreen_atom in state_prop.value:
                                return True
                                
                    if bypass_apps:
                        wm_class = win.get_wm_class()
                        if wm_class:
                            for name in wm_class:
                                if name.lower() in bypass_apps:
                                    return True
        except Exception:
            pass
        return False


class OverlayManager:
    """Steuert das rahmenlose, transparente Click-Through Overlay."""
    def __init__(self, root, model):
        self.root = root
        self.model = model
        self.overlay_win = None
        self.display = None
        self.is_idle = False
        self.healing_saver = None
        
        # Starte die regelmäßige Inaktivitätsprüfung
        self.root.after(1000, self._poll_idle)

    def _get_toplevel_xwindow(self, win):
        try:
            if not self.display:
                self.display = display.Display()
            win_id = int(win.wm_frame(), 16)
            xwin = self.display.create_resource_object('window', win_id)
            tree = xwin.query_tree()
            parent = tree.parent
            root = tree.root
            depth = 0
            while parent and parent.id != root.id and depth < 20:
                xwin = parent
                tree = xwin.query_tree()
                parent = tree.parent
                root = tree.root
                depth += 1
            return xwin
        except Exception:
            try:
                return self.display.create_resource_object('window', int(win.wm_frame(), 16))
            except Exception:
                return None

    def _apply_click_through(self, win):
        d = None
        try:
            d = display.Display()
            
            # 1. Apply to the client window itself
            client_id = win.winfo_id()
            client_xwin = d.create_resource_object('window', client_id)
            
            # Ensure compositor does not suspend for this window (preventing solid black/brown screen)
            try:
                bypass_atom = d.intern_atom('_NET_WM_BYPASS_COMPOSITOR')
                cardinal_atom = d.intern_atom('CARDINAL')
                client_xwin.change_property(bypass_atom, cardinal_atom, 32, [2])
            except Exception:
                pass

            shape.rectangles(client_xwin, shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
            
            # 2. Walk up and apply to all parents in the hierarchy up to the child of root
            curr_xwin = client_xwin
            try:
                tree = curr_xwin.query_tree()
                parent = tree.parent
                root = tree.root
                depth = 0
                while parent and parent.id != root.id and depth < 20:
                    try:
                        parent.change_property(bypass_atom, cardinal_atom, 32, [2])
                    except Exception:
                        pass
                    shape.rectangles(parent, shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
                    curr_xwin = parent
                    tree = curr_xwin.query_tree()
                    parent = tree.parent
                    root = tree.root
                    depth += 1
                
                # Also apply to the top-level parent wrapper (root's child)
                if curr_xwin and curr_xwin.id != root.id:
                    try:
                        curr_xwin.change_property(bypass_atom, cardinal_atom, 32, [2])
                    except Exception:
                        pass
                    shape.rectangles(curr_xwin, shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
            except Exception:
                pass
                
            # 3. Apply to whatever wm_frame() returns if it's different
            try:
                frame_hex = win.wm_frame()
                if frame_hex:
                    frame_id = int(frame_hex, 16)
                    if frame_id != client_id:
                        frame_xwin = d.create_resource_object('window', frame_id)
                        try:
                            frame_xwin.change_property(bypass_atom, cardinal_atom, 32, [2])
                        except Exception:
                            pass
                        shape.rectangles(frame_xwin, shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
            except Exception:
                pass
                
            d.flush()
        except Exception as e:
            try:
                with open("/home/hsunman/Documents/antigravity/hopeful-volta/click_through_error.log", "a") as f:
                    import traceback
                    f.write(f"[{time.asctime()}] Error in _apply_click_through: {e}\n")
                    traceback.print_exc(file=f)
            except Exception:
                pass
        finally:
            if d:
                try:
                    d.close()
                except Exception:
                    pass

    def _setup_click_through(self, win):
        def reapply(event=None):
            self._apply_click_through(win)
        win.bind("<Configure>", reapply)

    def _is_compositor_active(self):
        try:
            if not self.display:
                self.display = display.Display()
            screen = self.display.get_default_screen()
            atom = self.display.intern_atom(f"_NET_WM_CM_S{screen}")
            owner = self.display.get_selection_owner(atom)
            return owner != 0
        except Exception:
            return False

    def update_overlay(self):
        mode = self.model.config.get("operating_mode", "Schutz")
        
        # If mode is Schutz, we only show it if compensation is enabled
        if mode == "Schutz" and not self.model.config["compensation_enabled"]:
            self.hide_overlay()
            return
            
        if self.is_idle:
            return
            
        if mode == "Gaming":
            self.hide_overlay()
            return
            
        # Sicherheitsprüfung: Ist ein Compositor aktiv?
        if not self._is_compositor_active():
            print("Warnung: Kein Compositor aktiv. Verberge Overlay zur Sicherheit.")
            self.hide_overlay()
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
            
            mode = self.model.config.get("operating_mode", "Schutz")
            
            if mode == "Nacht":
                if not self.overlay_win:
                    self.overlay_win = tk.Toplevel(self.root, takefocus=False)
                    self.overlay_win.withdraw() # Start hidden
                    self.overlay_win.overrideredirect(True)
                    self.overlay_win.geometry(f"{width}x{height}+0+0")
                    self.overlay_win.wm_attributes("-type", "notification")
                    self.overlay_win.attributes("-topmost", True)
                    self.overlay_win.config(bg="#221000") # Edler warmer Bernstein-Filter
                    
                    self.overlay_win.update_idletasks()
                    self._apply_click_through(self.overlay_win)
                    self._setup_click_through(self.overlay_win)
                    self.overlay_win.deiconify()

                self.overlay_win.config(bg="#221000") # Edler warmer Bernstein-Filter (Blaulichtfilter)
                night_dim = self.model.config.get("night_dim_percent", 30) / 100.0
                self.overlay_win.attributes("-alpha", night_dim)
                
                try:
                    self.overlay_win.update()
                    xwin = self._get_toplevel_xwindow(self.overlay_win)
                    if xwin:
                        # Reset Bounding shape to full screen
                        shape.rectangles(xwin, shape.SO.Set, shape.SK.Bounding, 0, 0, 0, [(0, 0, width, height)])
                    self._apply_click_through(self.overlay_win)
                except Exception as shape_err:
                    print(f"Fehler beim Zeichnen des Nacht-Overlays: {shape_err}")
                    self.hide_overlay()
                return
            
            # Berechne Dämpfungswerte für jeden Block
            with self.model.lock:
                # Physikalisches L_max: 1.0 - Abnutzung (Maximum der Kanäle für sichere Helligkeitskompensation)
                l_max = [[1.0 - max(self.model.wear_map[y][x]) for x in range(cols)] for y in range(rows)]
            
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
                self.overlay_win = tk.Toplevel(self.root, takefocus=False)
                self.overlay_win.withdraw() # Start hidden
                self.overlay_win.overrideredirect(True)
                self.overlay_win.geometry(f"{width}x{height}+0+0")
                self.overlay_win.wm_attributes("-type", "notification")
                self.overlay_win.attributes("-topmost", True)
                self.overlay_win.config(bg="black")
                
                self.overlay_win.update_idletasks()
                self._apply_click_through(self.overlay_win)
                self._setup_click_through(self.overlay_win)
                self.overlay_win.deiconify()
                
            self.overlay_win.config(bg="black")
            if self.display:
                self.display.flush()

            # Setze die Gesamt-Fenster-Opacity auf das Maximum der benötigten Dämpfung
            self.overlay_win.attributes("-alpha", max_dim)

            # Maskierung der abgenutzten Zonen via X11 Bounding Shape mit räumlichem Dithering
            # Wir dämpfen nur dort, wo der Helligkeitsverlust noch gering ist.
            rects = []
            block_w = width // cols
            block_h = height // rows
            
            dithering = self.model.config.get("dithering_percent", 1) / 100.0
            
            for y in range(rows):
                for x in range(cols):
                    # Deterministisches Rauschen basierend auf Position zur Vermeidung von Flackern
                    if dithering > 0:
                        val = math.sin(x * 12.9898 + y * 78.233) * 43758.5453
                        noise = -dithering + 2 * dithering * (val - math.floor(val))
                    else:
                        noise = 0
                    if max_dim > 0 and (dim_map[y][x] + noise) > 0.3 * max_dim:
                        rx = x * block_w
                        ry = y * block_h
                        rects.append((rx, ry, block_w, block_h))

            # Bounding Shape setzen
            try:
                self.overlay_win.update()
                xwin = self._get_toplevel_xwindow(self.overlay_win)
                if xwin:
                    # Bounding first, then Input shape to prevent shape resets!
                    if rects:
                        shape.rectangles(xwin, shape.SO.Set, shape.SK.Bounding, 0, 0, 0, rects)
                    else:
                        # Wenn keine Rechtecke vorhanden sind, verstecke das Fenster komplett
                        shape.rectangles(xwin, shape.SO.Set, shape.SK.Bounding, 0, 0, 0, [])
                    self._apply_click_through(self.overlay_win)
            except Exception as shape_err:
                print(f"Fehler beim Zeichnen des Schutz-Overlays: {shape_err}")
                self.hide_overlay()

        except Exception as e:
            print(f"Fehler im OverlayManager beim Zeichnen: {e}")
            self.hide_overlay()

    def hide_overlay(self):
        if self.healing_saver:
            try:
                self.healing_saver.hide()
            except Exception:
                pass
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
                        print("System im Leerlauf. Aktiviere Inaktivitäts-Schoner.")
                    
                    idle_mode = self.model.config.get("idle_mode", "Dimmen")
                    if idle_mode == "Heilung":
                        self.hide_overlay()
                        if not self.healing_saver:
                            self.healing_saver = ActiveHealingSaver(self.root, self.model, self.display, self)
                        self.healing_saver.show()
                    else:
                        if self.healing_saver:
                            self.healing_saver.hide()
                        self._apply_idle_dimming()
                else:
                    if self.is_idle:
                        self.is_idle = False
                        print("Aktivität erkannt. Deaktiviere Inaktivitäts-Schoner.")
                        if self.healing_saver:
                            self.healing_saver.hide()
                        self.update_overlay()
            else:
                if self.is_idle:
                    self.is_idle = False
                    if self.healing_saver:
                        self.healing_saver.hide()
                    self.update_overlay()
        except Exception as e:
            print(f"Fehler bei Inaktivitätsprüfung: {e}")
            
        self.root.after(1000, self._poll_idle)

    def _apply_idle_dimming(self):
        try:
            # Sicherheitsprüfung: Ist ein Compositor aktiv?
            if not self._is_compositor_active():
                self.hide_overlay()
                return

            width = self.display.screen().width_in_pixels
            height = self.display.screen().height_in_pixels
            
            if not self.overlay_win:
                self.overlay_win = tk.Toplevel(self.root, takefocus=False)
                self.overlay_win.withdraw() # Start hidden
                self.overlay_win.overrideredirect(True)
                self.overlay_win.geometry(f"{width}x{height}+0+0")
                self.overlay_win.wm_attributes("-type", "notification")
                self.overlay_win.attributes("-topmost", True)
                self.overlay_win.config(bg="black")
                
                self.overlay_win.update_idletasks()
                self._apply_click_through(self.overlay_win)
                self._setup_click_through(self.overlay_win)
                self.overlay_win.deiconify()

            # Helligkeit dämpfen
            idle_dim = self.model.config.get("idle_dim_percent", 60) / 100.0
            self.overlay_win.attributes("-alpha", idle_dim)

            try:
                self.overlay_win.update()
                xwin = self._get_toplevel_xwindow(self.overlay_win)
                if xwin:
                    # Reset Bounding shape to full screen for full dimming
                    shape.rectangles(xwin, shape.SO.Set, shape.SK.Bounding, 0, 0, 0, [(0, 0, width, height)])
                self._apply_click_through(self.overlay_win)
            except Exception as shape_err:
                print(f"Fehler beim Setzen des Idle-Shapes: {shape_err}")
                self.hide_overlay()
            
        except Exception as e:
            print(f"Fehler beim Anwenden des Inaktivitäts-Dimmers: {e}")
            self.hide_overlay()


class ActiveHealingSaver:
    """Visueller, animierter Bildschirmschoner zur aktiven Ausgleichs-Alterung (Panel Healing)."""
    def __init__(self, root, model, display_obj, overlay_manager):
        self.root = root
        self.model = model
        self.display = display_obj
        self.overlay_manager = overlay_manager
        self.window = None
        self.canvas = None
        self.running = False
        self.time_val = 0.0

    def show(self):
        if self.window:
            return
            
        width = self.display.screen().width_in_pixels
        height = self.display.screen().height_in_pixels
        
        self.window = tk.Toplevel(self.root, takefocus=False)
        self.window.withdraw() # Start hidden
        self.window.overrideredirect(True)
        self.window.geometry(f"{width}x{height}+0+0")
        self.window.wm_attributes("-type", "notification")
        self.window.attributes("-topmost", True)
        self.window.config(bg="black")
        
        # Enforce click-through on configure
        def reapply(event=None):
            if self.overlay_manager:
                self.overlay_manager._apply_click_through(self.window)
        self.window.bind("<Configure>", reapply)

        # Input Shape für Click-Through
        try:
            self.window.update_idletasks()
            if self.overlay_manager:
                self.overlay_manager._apply_click_through(self.window)
            self.window.deiconify()
            self.window.update()
        except Exception as shape_err:
            print(f"Fehler beim Setzen des Click-Through für HealingSaver: {shape_err}")
            self.hide()
        
        self.canvas = tk.Canvas(self.window, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        self.running = True
        self.time_val = 0.0
        self._animate()

    def hide(self):
        self.running = False
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None
            self.canvas = None

    def _animate(self):
        if not self.running or not self.window:
            return
            
        try:
            cw = self.window.winfo_width()
            ch = self.window.winfo_height()
            if cw <= 1 or ch <= 1:
                self.window.after(50, self._animate)
                return
                
            self.canvas.delete("all")
            
            cols = self.model.config["grid_cols"]
            rows = self.model.config["grid_rows"]
            
            with self.model.lock:
                wear = [[[val for val in cell] for cell in row] for row in self.model.wear_map]
                
            max_wear = 0.0
            for y in range(rows):
                for x in range(cols):
                    for c in range(3):
                        if wear[y][x][c] > max_wear:
                            max_wear = wear[y][x][c]
            
            # Pixel-Orbiting zur Minderung von Kantenalterung
            orbit_r = 15.0
            self.time_val += 0.15
            dx = int(orbit_r * math.cos(self.time_val * 0.1))
            dy = int(orbit_r * math.sin(self.time_val * 0.1))
            
            block_w = cw / cols
            block_h = ch / rows
            
            # Breathing Amplitude
            global_breath = 0.75 + 0.25 * math.sin(self.time_val)
            
            for y in range(rows):
                for x in range(cols):
                    w_rgb = wear[y][x]
                    
                    if max_wear < 0.0001:
                        hr, hg, hb = 0.01, 0.01, 0.01
                    else:
                        hr = max_wear - w_rgb[0]
                        hg = max_wear - w_rgb[1]
                        hb = max_wear - w_rgb[2]
                        
                    max_deficit = max(hr, hg, hb)
                    # Skalierung: Maximale Dämpfung auf 50% Leistung um das Panel zu schonen
                    scale = 0.5 * 255.0 / max_deficit if max_deficit > 0 else 0
                    
                    # Schimmernder Wave-Filter
                    wave_mod = 0.8 + 0.2 * math.sin(x * 0.35 + self.time_val) * math.cos(y * 0.35 - self.time_val * 0.6)
                    
                    r_val = int(min(255, max(0, hr * scale * global_breath * wave_mod)))
                    g_val = int(min(255, max(0, hg * scale * global_breath * wave_mod)))
                    b_val = int(min(255, max(0, hb * scale * global_breath * wave_mod)))
                    
                    color = f"#{r_val:02x}{g_val:02x}{b_val:02x}"
                    
                    x1 = x * block_w + dx
                    y1 = y * block_h + dy
                    x2 = x1 + block_w
                    y2 = y1 + block_h
                    
                    self.canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="")
                    
            tx = cw // 2 + dx
            ty = ch - 50 + dy
            self.canvas.create_text(tx, ty, text="OLED SAFE-GUARD: AKTIVE COMPLEMENTÄRE BILDSCHIRM-HEILUNG (AKTIVITÄT BEENDET SCHONER)", fill="#005577", font=("Outfit", 10, "bold"))
            
        except Exception as e:
            print(f"Fehler in Heilungs-Saver Animation: {e}")
            
        self.root.after(30, self._animate)


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
        
        # Screen Info Banner
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        screen_info = f"🖥   Gesamtauflösung: {screen_w}x{screen_h} px  |  📊 Messraster: {self.model.config['grid_cols']}x{self.model.config['grid_rows']} Zonen"
        ttk.Label(card3, text=screen_info, style="CardText.TLabel", foreground=self.accent_cyan, font=("Inter", 9, "bold")).pack(anchor="w", padx=20, pady=(0, 10))
        
        # Info Text
        desc_text = (
            "Dieser Hintergrundprozess gleicht physikalische Pixelabnutzungen (Einbrenneffekte) aus.\n"
            "Das System analysiert Ihren Bildschirm alle 60 Sekunden unauffällig im Speicher und dimmt gesunde\n"
            "Bereiche gezielt und unmerklich herunter, um eine absolut homogene Gesamthelligkeit zu garantieren."
        )
        ttk.Label(card3, text=desc_text, style="CardText.TLabel", justify="left").pack(anchor="w", padx=20, pady=(0, 20))
        
        # Mode selector Frame (Segmented-Style buttons)
        mode_frame = ttk.Frame(card3, style="Card.TFrame")
        mode_frame.pack(fill="x", padx=20, pady=(0, 20))
        
        ttk.Label(mode_frame, text="Betriebsmodus:", style="CardText.TLabel", font=("Outfit", 10, "bold")).pack(side="left", padx=(0, 15))
        
        self.mode_buttons = {}
        modes = [
            ("Schutz", "🛡️ Schutz-Modus"),
            ("Gaming", "🎮 Gaming (HDR)"),
            ("Nacht", "🌙 Nacht-Filter")
        ]
        
        for mode_key, mode_label in modes:
            btn = ttk.Button(
                mode_frame, 
                text=mode_label, 
                command=lambda m=mode_key: self.change_operating_mode(m)
            )
            btn.pack(side="left", padx=(0, 10))
            self.mode_buttons[mode_key] = btn
            
        self.update_mode_button_styles()
        
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
        
        # Channel Selection Frame
        chan_frame = ttk.Frame(card, style="Card.TFrame")
        chan_frame.pack(fill="x", padx=20, pady=(0, 10))
        
        ttk.Label(chan_frame, text="Subpixel-Kanal:", style="CardText.TLabel", font=("Outfit", 9, "bold")).pack(side="left", padx=(0, 10))
        
        self.val_heatmap_channel = tk.StringVar(value="Gesamt")
        self.channel_buttons = {}
        channels = [
            ("Gesamt", "🛡️ Gesamt"),
            ("Rot", "🟥 Rot-Kanal"),
            ("Grün", "🟩 Grün-Kanal"),
            ("Blau", "🟦 Blau-Kanal")
        ]
        for chan_key, chan_lbl in channels:
            btn = ttk.Button(chan_frame, text=chan_lbl, command=lambda c=chan_key: self.change_heatmap_channel(c))
            btn.pack(side="left", padx=(0, 10))
            self.channel_buttons[chan_key] = btn
            
        self.update_channel_button_styles()

        ttk.Label(card, text="Visuelle Darstellung der thermischen Belastung. Dunkel = Gesund | Hell/Rot/Gelb = Statische Zonen", style="StatLbl.TLabel").pack(anchor="w", padx=20, pady=(0, 15))
        
        # Heatmap Canvas
        self.map_canvas = tk.Canvas(card, bg="#080808", highlightthickness=1, highlightbackground="#333333")
        self.map_canvas.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # Export Button
        self.btn_export = ttk.Button(card, text="Hitze-Karte exportieren (.png)", command=self.export_heatmap_image)
        self.btn_export.pack(anchor="w", padx=20, pady=(0, 15))

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

        # Automatischer X11 Bypass
        ttk.Label(left_col, text="Automatischer X11 Bypass", font=("Outfit", 12, "bold"), background=self.bg_card, foreground=self.accent_cyan).pack(anchor="w", pady=(15, 10))
        self.val_bypass_fs = tk.BooleanVar(value=self.model.config.get("bypass_fullscreen", True))
        self.chk_bypass_fs = ttk.Checkbutton(left_col, text="Bypass bei Vollbild-Anwendungen", variable=self.val_bypass_fs, style="TCheckbutton")
        self.chk_bypass_fs.pack(anchor="w", pady=(5, 5))
        
        ttk.Label(left_col, text="Bypass bei diesen Apps (Komma-separiert):", style="CardText.TLabel").pack(anchor="w", pady=(5, 2))
        self.entry_bypass_apps = ttk.Entry(left_col, width=30)
        self.entry_bypass_apps.insert(0, self.model.config.get("bypass_apps", "vlc,steam,mpv"))
        self.entry_bypass_apps.pack(anchor="w", pady=(0, 15))

        # Rechte Spalte (Inaktivitäts-Dimmer)
        right_col = ttk.Frame(card, style="Card.TFrame")
        right_col.grid(row=1, column=1, padx=(10, 20), pady=10, sticky="nsew")

        ttk.Label(right_col, text="Inaktivitäts-Dimmer (Bildschirmschoner)", font=("Outfit", 12, "bold"), background=self.bg_card, foreground=self.accent_cyan).pack(anchor="w", pady=(0, 10))

        # Checkbox: Aktivieren
        self.val_idle_enabled = tk.BooleanVar(value=self.model.config.get("idle_dimming_enabled", True))
        self.chk_idle = ttk.Checkbutton(right_col, text="Schoner/Dimmer aktivieren", variable=self.val_idle_enabled, style="TCheckbutton", command=self.on_toggle_idle_dimming)
        self.chk_idle.pack(anchor="w", pady=(5, 10))

        # Inaktivitäts-Modus (Dimmen vs Heilung)
        ttk.Label(right_col, text="Inaktivitäts-Modus:", style="CardText.TLabel").pack(anchor="w", pady=(5, 2))
        self.val_idle_mode = tk.StringVar(value=self.model.config.get("idle_mode", "Dimmen"))
        self.combo_idle_mode = ttk.Combobox(right_col, textvariable=self.val_idle_mode, values=["Dimmen", "Heilung"], state="readonly", width=15)
        self.combo_idle_mode.pack(anchor="w", pady=(0, 15))

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

        # Zeitsteuerung (Nacht-Filter)
        ttk.Label(right_col, text="Automatischer Zeitplan", font=("Outfit", 12, "bold"), background=self.bg_card, foreground=self.accent_cyan).pack(anchor="w", pady=(10, 10))
        self.val_night_schedule = tk.BooleanVar(value=self.model.config.get("night_schedule_enabled", False))
        self.chk_night_schedule = ttk.Checkbutton(right_col, text="Nacht-Filter abends automatisch aktivieren\n(von 20:00 Uhr bis 06:00 Uhr)", variable=self.val_night_schedule, style="TCheckbutton", command=self.on_toggle_night_schedule)
        self.chk_night_schedule.pack(anchor="w", pady=(5, 10))

        # Slider 6: Dithering-Rauschen
        ttk.Label(right_col, text="Dithering-Stärke (Rauschen zur Minderung von Banding):", style="CardText.TLabel").pack(anchor="w", pady=(5, 2))
        self.val_dithering = tk.IntVar(value=self.model.config.get("dithering_percent", 1))
        self.lbl_dithering_num = ttk.Label(right_col, text=f"{self.val_dithering.get()}%", style="CardText.TLabel", foreground=self.accent_cyan)
        self.lbl_dithering_num.pack(anchor="w")
        self.slider_dithering = ttk.Scale(right_col, from_=0, to=5, variable=self.val_dithering, orient="horizontal", command=lambda e: self.lbl_dithering_num.config(text=f"{self.val_dithering.get()}%"))
        self.slider_dithering.pack(fill="x", pady=(0, 15))

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

    def change_operating_mode(self, mode):
        self.model.config["operating_mode"] = mode
        self.model.save_config()
        self.update_mode_button_styles()
        self.overlay_manager.update_overlay()

    def update_mode_button_styles(self):
        active_mode = self.model.config.get("operating_mode", "Schutz")
        for mode_key, btn in self.mode_buttons.items():
            if mode_key == active_mode:
                btn.config(style="Accent.TButton")
            else:
                btn.config(style="TButton")

    def change_heatmap_channel(self, channel):
        self.val_heatmap_channel.set(channel)
        self.update_channel_button_styles()
        self.draw_heatmap()

    def update_channel_button_styles(self):
        active_chan = self.val_heatmap_channel.get()
        for chan_key, btn in self.channel_buttons.items():
            if chan_key == active_chan:
                btn.config(style="Accent.TButton")
            else:
                btn.config(style="TButton")

    def on_toggle_idle_dimming(self):
        is_enabled = self.val_idle_enabled.get()
        self.model.config["idle_dimming_enabled"] = is_enabled
        self.model.save_config()

    def on_toggle_night_schedule(self):
        is_enabled = self.val_night_schedule.get()
        self.model.config["night_schedule_enabled"] = is_enabled
        self.model.save_config()

    def save_settings(self):
        self.model.config["tracking_interval_seconds"] = self.val_interval.get()
        self.model.config["aging_speed"] = self.val_speed.get()
        self.model.config["max_dimming_percent"] = self.val_max_dim.get()
        self.model.config["idle_dimming_enabled"] = self.val_idle_enabled.get()
        self.model.config["idle_timeout_seconds"] = self.val_idle_timeout.get()
        self.model.config["idle_dim_percent"] = self.val_idle_dim.get()
        self.model.config["night_schedule_enabled"] = self.val_night_schedule.get()
        self.model.config["dithering_percent"] = self.val_dithering.get()
        self.model.config["idle_mode"] = self.val_idle_mode.get()
        self.model.config["bypass_fullscreen"] = self.val_bypass_fs.get()
        self.model.config["bypass_apps"] = self.entry_bypass_apps.get()
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

    def export_heatmap_image(self):
        from tkinter import filedialog
        
        cols = self.model.config["grid_cols"]
        rows = self.model.config["grid_rows"]
        
        img_w = 1280
        img_h = 720
        block_w = img_w / cols
        block_h = img_h / rows
        
        img = Image.new("RGB", (img_w, img_h), "#080808")
        draw = ImageDraw.Draw(img)
        
        with self.model.lock:
            wear = [[[c for c in cell] for cell in row] for row in self.model.wear_map]
            
        channel = self.val_heatmap_channel.get()
        channel_wear = []
        for y in range(rows):
            row_vals = []
            for x in range(cols):
                w_rgb = wear[y][x]
                if channel == "Rot":
                    val = w_rgb[0]
                elif channel == "Grün":
                    val = w_rgb[1]
                elif channel == "Blau":
                    val = w_rgb[2]
                else:
                    val = max(w_rgb)
                row_vals.append(val)
            channel_wear.append(row_vals)
            
        max_w = max(max(row) for row in channel_wear) if channel_wear else 0
        
        def get_heat_color_rgb(val):
            if max_w == 0:
                return (0, 0, 0)
            nv = val / max_w if max_w > 0 else 0
            
            if channel == "Rot":
                return (int(nv * 255), 0, 0)
            elif channel == "Grün":
                return (0, int(nv * 255), 0)
            elif channel == "Blau":
                return (0, 0, int(nv * 255))
            else:
                if nv < 0.2:
                    return (0, 0, int((nv / 0.2) * 255))
                elif nv < 0.4:
                    return (int(((nv - 0.2) / 0.2) * 150), 0, 255)
                elif nv < 0.6:
                    return (255, 0, int((1.0 - (nv - 0.4) / 0.2) * 255))
                elif nv < 0.8:
                    return (255, int(((nv - 0.6) / 0.2) * 160), 0)
                else:
                    return (255, 255, int(((nv - 0.8) / 0.2) * 255))

        for y in range(rows):
            for x in range(cols):
                w_val = channel_wear[y][x]
                color = get_heat_color_rgb(w_val)
                x1 = x * block_w
                y1 = y * block_h
                x2 = x1 + block_w
                y2 = y1 + block_h
                draw.rectangle([x1, y1, x2, y2], fill=color, outline=(24, 24, 24))
                
        filepath = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG Image", "*.png")],
            initialfile="oled_safeguard_heatmap.png",
            title="Hitze-Karte speichern"
        )
        if filepath:
            try:
                img.save(filepath)
                messagebox.showinfo("Export erfolgreich", f"Die Hitze-Karte wurde unter {filepath} gespeichert.")
            except Exception as e:
                messagebox.showerror("Fehler beim Export", f"Die Hitze-Karte konnte nicht gespeichert werden: {e}")

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
            wear = [[[c for c in cell] for cell in row] for row in self.model.wear_map]

        channel = self.val_heatmap_channel.get()
        channel_wear = []
        for y in range(rows):
            row_vals = []
            for x in range(cols):
                w_rgb = wear[y][x]
                if channel == "Rot":
                    val = w_rgb[0]
                elif channel == "Grün":
                    val = w_rgb[1]
                elif channel == "Blau":
                    val = w_rgb[2]
                else:
                    val = max(w_rgb)
                row_vals.append(val)
            channel_wear.append(row_vals)

        max_w = max(max(row) for row in channel_wear) if channel_wear else 0
        
        def get_heat_color(val):
            if max_w == 0:
                return "#000000"
            nv = val / max_w if max_w > 0 else 0
            
            if channel == "Rot":
                r = int(nv * 255)
                return f"#{r:02x}0000"
            elif channel == "Grün":
                g = int(nv * 255)
                return f"#00{g:02x}00"
            elif channel == "Blau":
                b = int(nv * 255)
                return f"#0000{b:02x}"
            else:
                if nv < 0.2:
                    r, g, b = 0, 0, int((nv / 0.2) * 255)
                elif nv < 0.4:
                    r, g, b = int(((nv - 0.2) / 0.2) * 150), 0, 255
                elif nv < 0.6:
                    r, g, b = 255, 0, int((1.0 - (nv - 0.4) / 0.2) * 255)
                elif nv < 0.8:
                    r, g, b = 255, int(((nv - 0.6) / 0.2) * 160), 0
                else:
                    r, g, b = 255, 255, int(((nv - 0.8) / 0.2) * 255)
                return f"#{r:02x}{g:02x}{b:02x}"

        for y in range(rows):
            for x in range(cols):
                w_val = channel_wear[y][x]
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
            wear = [[[val for val in cell] for cell in row] for row in self.model.wear_map]
            
        max_wear = 0.0
        if wear:
            for row in wear:
                for cell in row:
                    for val in cell:
                        if val > max_wear:
                            max_wear = val
        homogeneity = 100.0 - (max_wear * 100.0)
        
        self.lbl_max_wear.config(text=f"{max_wear * 100.0:.3f}%")
        self.lbl_homogeneity.config(text=f"{homogeneity:.3f}%")

        # Aktualisiere Hitze-Karte
        self.draw_heatmap()

        # Nächstes Update planen (alle 500ms für responsive GUI)
        self.root.after(500, self.update_loop)


def start_instance_server(root, daemon, model, overlay_manager, run_headless):
    """Startet einen lokalen Socket-Server, um Mehrfachinstanzen zu steuern."""
    def server_thread():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', PORT))
            s.listen(1)
        except Exception:
            return  # Port besetzt (bereits aktiv)
            
        gui_created = [not run_headless]
        
        def show_gui():
            if not gui_created[0]:
                ControlGUI(root, model, daemon, overlay_manager)
                gui_created[0] = True
            root.deiconify()
            root.lift()
            
        while True:
            try:
                conn, addr = s.accept()
                data = conn.recv(1024).decode('utf-8')
                if data == "show":
                    root.after(0, show_gui)
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
    start_instance_server(root, daemon, model, overlay_manager, run_headless)

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
