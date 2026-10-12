#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoiceChanger — GUI-утилита изменения голоса в реальном времени (Linux).

ОДИН файл: здесь и сам войс-ченджер (DSP-движок), и графический интерфейс.
"""

import io
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave

import numpy as np

try:
    import sounddevice as sd
    SD_OK, SD_ERR = True, ""
except Exception as _exc:
    sd, SD_OK, SD_ERR = None, False, str(_exc)

from PyQt6.QtCore import (
    QEasingCurve, QEvent, QKeyCombination, QPoint, QPointF, QPropertyAnimation, QRectF, QSettings, QSize, Qt, QTimer, QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence, QLinearGradient, QPainter,
    QPainterPath, QPalette, QPen, QPolygonF, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGraphicsOpacityEffect, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget,
    QVBoxLayout, QWidget,
)

APP_TITLE = "Voice Changer"
APP_VERSION = "2.1.0"
SCOPE_N = 2048                      # сколько последних сэмплов видит спектр


# ===========================================================================
#  ВСПОМОГАТЕЛЬНОЕ
# ===========================================================================

def db2lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def lin2db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-9))


def gain_ramp(prev: float, target: float, n: int, ramp_n=None) -> np.ndarray:
    r = n if ramp_n is None else max(1, min(n, int(ramp_n)))
    g = np.full(n, target, dtype=np.float64)
    g[:r] = prev + (target - prev) * (np.arange(1, r + 1) / r)
    return g


# ===========================================================================
#  ПАРАМЕТРЫ И ПРЕСЕТЫ ГОЛОСА
# ===========================================================================

DEFAULTS = dict(
    pitch=0.0, gate=0.0, comp=0.0, gain=1.0,
    vibrato=0.0, vib_rate=5.0, tremolo=0.0, trem_rate=5.0,
    chorus=0.0, bass=0.0, treble=0.0, bandpass=False,
    ring=0.0, ring_freq=50.0, drive=0.0, crush=0.0,
    echo=0.0, echo_time=0.25, echo_fb=0.35,
    reverb=0.0, room=0.5, formant=0.0,
    harmony=0.0, harm_int=7.0, whisper=0.0, denoise=0.0,
    scream=0.0, scream_thr=-10.0, scream_boom=0.0,
)

_pct = lambda v: f"{v} %"

SLIDER_SPECS = {
    "pitch":     ("Тон:", -24, 24, 0.5, lambda v: f"{v * 0.5:+.1f} пт", "Сдвиг высоты голоса в полутонах"),
    "vibrato":   ("Вибрато:", 0, 100, 0.01, _pct, "Дрожание высоты тона"),
    "vib_rate":  ("Скорость вибрато:", 10, 120, 0.1, lambda v: f"{v / 10:.1f} Гц", ""),
    "tremolo":   ("Тремоло:", 0, 100, 0.01, _pct, "Дрожание громкости"),
    "trem_rate": ("Скорость тремоло:", 10, 200, 0.1, lambda v: f"{v / 10:.1f} Гц", ""),
    "chorus":    ("Хорус:", 0, 100, 0.01, _pct, "Эффект «несколько голосов»"),
    "bass":      ("Низы:", -12, 12, 1.0, lambda v: f"{v:+d} дБ", "Эквалайзер: низкие частоты"),
    "treble":    ("Верха:", -12, 12, 1.0, lambda v: f"{v:+d} дБ", "Эквалайзер: высокие частоты"),
    "ring":      ("Робот:", 0, 100, 0.01, _pct, "Кольцевой модулятор"),
    "ring_freq": ("Частота робота:", 20, 400, 1.0, lambda v: f"{v} Гц", ""),
    "drive":     ("Фузз:", 0, 100, 0.01, _pct, "Перегруз / дисторшн"),
    "crush":     ("Ретро (8-бит):", 0, 100, 0.01, _pct, "Снижение разрядности"),
    "echo":      ("Эхо:", 0, 100, 0.01, _pct, ""),
    "echo_time": ("Время эха:", 50, 800, 0.001, lambda v: f"{v} мс", ""),
    "echo_fb":   ("Повторы эха:", 0, 85, 0.01, _pct, "Сколько раз эхо повторяется"),
    "reverb":    ("Реверб:", 0, 100, 0.01, _pct, ""),
    "room":      ("Размер комнаты:", 0, 100, 0.01, _pct, "Длина хвоста реверба"),
    "formant":   ("Формант:", -50, 50, 0.01, lambda v: f"{v:+d} %", "Сдвиг формант"),
    "harmony":   ("Гармония:", 0, 100, 0.01, _pct, "Второй голос"),
    "harm_int":  ("Интервал гармонии:", -12, 12, 1.0, lambda v: f"{v:+d} пт", "Интервал второго голоса"),
    "whisper":   ("Шёпот:", 0, 100, 0.01, _pct, "Превращает голос в шёпот"),
    "denoise":   ("Шумоподавление:", 0, 100, 0.01, _pct, "Спектральное подавление шума"),
    "scream":      ("Крик-буст:", 0, 100, 0.01, _pct, "Когда кричишь, голос получает мощный бас и перегруз"),
    "scream_thr":  ("Порог крика:", -30, -3, 1.0, lambda v: f"{v} дБ", "Громче этого уровня включается крик-буст"),
    "scream_boom": ("Удар (бум):", 0, 100, 0.01, _pct, "Громовой удар в момент прорыва"),
    "gate":      ("Шумодав:", 0, 100, 0.0005, _pct, "Отсекает тихий шум микрофона"),
    "comp":      ("Компрессор:", 0, 100, 0.01, _pct, "Выравнивает громкость"),
    "gain":      ("Громкость:", 0, 250, 0.01, _pct, "Выходная громкость"),
}

GROUPS = (
    ("Голос", ("pitch", "formant", "harmony", "harm_int")),
    ("Тембр", ("bass", "treble", "bandpass")),
    ("Модуляция", ("vibrato", "vib_rate", "tremolo", "trem_rate", "chorus", "ring", "ring_freq")),
    ("Искажение", ("drive", "crush", "whisper")),
    ("Пространство", ("echo", "echo_time", "echo_fb", "reverb", "room")),
    ("Крик (для стримеров)", ("scream", "scream_thr", "scream_boom")),
    ("Динамика и чистота", ("denoise", "gate", "comp", "gain")),
)


def sanitize_params(d) -> dict:
    out = {}
    src = d if isinstance(d, dict) else {}
    for k, default in DEFAULTS.items():
        v = src.get(k, default)
        if isinstance(default, bool):
            out[k] = bool(v)
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = float(default)
        if not math.isfinite(v):
            v = float(default)
        spec = SLIDER_SPECS.get(k)
        if spec:
            v = min(max(v, spec[1] * spec[3]), spec[2] * spec[3])
        out[k] = v
    return out


def _p(**kw) -> dict:
    return sanitize_params({**DEFAULTS, **kw})


PRESETS = {
    "Без изменений":  _p(),
    "Бурундук":       _p(pitch=7),
    "Глубокий голос": _p(gate=0.01, pitch=-5, comp=0.4, gain=1.2),
    "Демон":          _p(gate=0.01, pitch=-7, ring=0.15, ring_freq=40, drive=0.5, reverb=0.25, comp=0.3, gain=1.3),
    "Монстр":         _p(pitch=-10, drive=0.3, ring=0.1, ring_freq=30, reverb=0.3, comp=0.4, gain=1.2),
    "Робот":          _p(bandpass=True, ring=1.0, ring_freq=50),
    "Рация / радио":  _p(gate=0.015, bandpass=True, drive=0.2, echo=0.25, echo_time=0.22, comp=0.6, gain=1.1),
    "Мегафон":        _p(gate=0.01, bandpass=True, drive=0.4, echo=0.15, echo_time=0.12, comp=0.7, gain=1.2),
    "Пришелец":       _p(bandpass=True, pitch=5, vibrato=0.4, ring=0.7, ring_freq=170, reverb=0.1),
    "Космонавт":      _p(gate=0.01, bandpass=True, pitch=-2, ring=0.3, ring_freq=80, echo=0.2, echo_time=0.22, reverb=0.5, comp=0.3, gain=1.2),
    "Призрак":        _p(gate=0.01, vibrato=0.6, echo=0.1, echo_time=0.22, reverb=0.6, gain=1.1),
    "Дрожащий":       _p(pitch=2, vibrato=0.3, vib_rate=7, tremolo=0.6, trem_rate=9),
    "Хор":            _p(chorus=0.8, reverb=0.25, room=0.6),
    "Пещера":         _p(echo=0.35, echo_time=0.45, echo_fb=0.5, reverb=0.6, room=0.9),
    "Ретро 8-бит":    _p(bandpass=True, crush=0.7),
    "Подкаст":        _p(gate=0.01, comp=0.5, bass=3, treble=2, gain=1.1),
    "Женский (реалист.)": _p(pitch=3.5, formant=0.16, treble=2, bass=-2, comp=0.2),
    "Мужской (реалист.)": _p(pitch=-3.5, formant=-0.14, bass=3, comp=0.3, gain=1.1),
    "Шёпот":          _p(whisper=0.9, reverb=0.15, comp=0.3, gain=1.3),
    "Дуэт":           _p(harmony=0.6, harm_int=7, chorus=0.3, reverb=0.15),
    "Октава вниз":    _p(harmony=0.7, harm_int=-12, bass=2),
    "Студия":         _p(denoise=0.6, gate=0.008, comp=0.5, bass=2, treble=2, gain=1.1),
    "Крик-буст": _p(gate=0.008, scream=1.0, scream_thr=-10.0, scream_boom=0.7),
}


# ===========================================================================
#  DSP ДВИЖОК
# ===========================================================================

class DelayLine:
    HEADROOM = 8192
    def __init__(self, sr: int, max_delay_s: float):
        self.B = max(64, int(sr * max_delay_s)) + self.HEADROOM + 2
        self.buf = np.zeros(self.B, dtype=np.float64)
        self.w = 0

    def write(self, x):
        self.buf[(self.w + np.arange(len(x))) % self.B] = x

    def read(self, n: int, delay) -> np.ndarray:
        d = np.broadcast_to(np.asarray(delay, dtype=np.float64), (n,))
        pos = (self.w + np.arange(n) - d) % self.B
        i0 = np.floor(pos).astype(np.int64)
        frac = pos - i0
        i1 = (i0 + 1) % self.B
        return self.buf[i0] * (1.0 - frac) + self.buf[i1] * frac

    def advance(self, n: int):
        self.w = (self.w + n) % self.B

    def clear(self):
        self.buf[:] = 0.0


class FIRFilter:
    def __init__(self, sr: int, taps: int = 401):
        self.sr, self.T = sr, taps | 1
        self.h = None
        self.hist = np.zeros(self.T - 1, dtype=np.float64)

    def set_response(self, fn):
        if fn is None:
            self.h = None
            return
        grid = 4096
        f = np.fft.rfftfreq(grid, 1.0 / self.sr)
        h = np.fft.irfft(np.asarray(fn(f), dtype=np.float64), grid)
        h = np.roll(h, self.T // 2)[:self.T] * np.hamming(self.T)
        self.h = h

    def process(self, x):
        h = self.h
        ext = np.concatenate((self.hist, x))
        self.hist = ext[-(self.T - 1):]
        if h is None:
            return x
        return np.convolve(ext, h, mode="valid")


class NoiseGate:
    HOLD_S = 0.15
    def __init__(self, sr):
        self.sr = sr
        self.threshold = 0.0
        self._gain, self._open, self._hold = 1.0, True, 0.0

    def process(self, x):
        n = len(x)
        if self.threshold <= 0.0:
            if self._gain >= 0.999:
                self._open = True
                return x
            target = 1.0
        else:
            peak = float(np.max(np.abs(x)))
            if peak >= self.threshold:
                self._open, self._hold = True, self.HOLD_S
            elif self._open:
                if peak < self.threshold * 0.7:
                    self._hold -= n / self.sr
                else:
                    self._hold = self.HOLD_S
                if self._hold <= 0.0:
                    self._open = False
            target = 1.0 if self._open else 0.0
        tau = 0.004 if target > self._gain else 0.05
        new = self._gain + (target - self._gain) * (1.0 - math.exp(-n / (self.sr * tau)))
        g = gain_ramp(self._gain, new, n)
        self._gain = new
        return x * g


class PitchShifter:
    def __init__(self, sr, window_s=0.07):
        self.D = max(32, int(sr * window_s))
        self.line = DelayLine(sr, window_s * 2.2)
        self.phase = 0.0
        self.ratio = 1.0

    def process(self, x):
        n = len(x)
        self.line.write(x)
        if abs(self.ratio - 1.0) < 1e-3:
            self.line.advance(n)
            return x
        ar = np.arange(n)
        slope = 1.0 - self.ratio
        d1 = (self.phase + slope * ar) % self.D
        d2 = (d1 + self.D * 0.5) % self.D
        s1 = self.line.read(n, d1)
        s2 = self.line.read(n, d2)
        g1 = 1.0 - np.abs(2.0 * d1 / self.D - 1.0)
        self.phase = (self.phase + slope * n) % self.D
        self.line.advance(n)
        return g1 * s1 + (1.0 - g1) * s2


class Vibrato:
    def __init__(self, sr, max_depth_s=0.010):
        self.sr = sr
        self.max_samples = max_depth_s * sr
        self.depth = 0.0
        self.rate = 5.0
        self._phase = 0.0
        self.line = DelayLine(sr, max_depth_s * 2 + 0.002)

    def process(self, x):
        n = len(x)
        self.line.write(x)
        depth = min(self.depth * self.sr, self.max_samples)
        if depth > 1e-3:
            step = 2.0 * np.pi * self.rate / self.sr
            ph = self._phase + step * np.arange(n)
            self._phase = (self._phase + step * n) % (2.0 * np.pi)
            d = depth * (1.0 - np.cos(ph)) * 0.5
            y = self.line.read(n, d + 1.0)
        else:
            y = self.line.read(n, 1.0)
        self.line.advance(n)
        return y


class Tremolo:
    def __init__(self, sr):
        self.sr, self.depth, self.rate, self._phase = sr, 0.0, 5.0, 0.0

    def process(self, x):
        if self.depth <= 1e-3:
            return x
        n = len(x)
        step = 2.0 * np.pi * self.rate / self.sr
        ph = self._phase + step * np.arange(n)
        self._phase = (self._phase + step * n) % (2.0 * np.pi)
        return x * (1.0 - self.depth * 0.5 * (1.0 - np.cos(ph)))


class RingModulator:
    def __init__(self, sr):
        self.sr, self.freq, self.mix, self._phase = sr, 50.0, 0.0, 0.0

    def process(self, x):
        if self.mix <= 1e-3:
            return x
        step = 2.0 * np.pi * self.freq / self.sr
        carrier = np.sin(self._phase + step * np.arange(len(x)))
        self._phase = (self._phase + step * len(x)) % (2.0 * np.pi)
        return (1.0 - self.mix) * x + self.mix * (x * carrier)


class Bitcrusher:
    def __init__(self):
        self.amount = 0.0
        self._pos, self._held = 0, 0.0

    def process(self, x):
        if self.amount <= 1e-3:
            return x
        n = len(x)
        bits = 16.0 - self.amount * 12.0
        hold = 1 + int(self.amount * 7)
        ar = np.arange(n)
        m = (self._pos + ar) % hold
        idx = ar - m
        y = np.where(idx >= 0, x[np.maximum(idx, 0)], self._held)
        last = (n - 1) - int((self._pos + n - 1) % hold)
        if last >= 0:
            self._held = float(x[last])
        self._pos = (self._pos + n) % hold
        levels = 2.0 ** (bits - 1.0)
        return np.round(y * levels) / levels


class Distortion:
    def __init__(self):
        self.drive = 0.0

    def process(self, x):
        if self.drive <= 1e-3:
            return x
        k = 1.0 + self.drive * 9.0
        return np.tanh(k * x) / np.tanh(k)


class Chorus:
    VOICES = ((0.018, 0.0035, 0.35), (0.024, 0.0045, 0.27), (0.013, 0.0030, 0.45))
    def __init__(self, sr):
        self.sr, self.mix, self._active = sr, 0.0, False
        self.line = DelayLine(sr, 0.05)
        self._phase = [0.0, 2.1, 4.2]

    def process(self, x):
        if self.mix <= 1e-3:
            if self._active:
                self.line.clear()
                self._active = False
            return x
        self._active = True
        n = len(x)
        self.line.write(x)
        wet = np.zeros(n)
        ar = np.arange(n)
        for i, (base, depth, rate) in enumerate(self.VOICES):
            step = 2.0 * np.pi * rate / self.sr
            ph = self._phase[i] + step * ar
            self._phase[i] = (self._phase[i] + step * n) % (2.0 * np.pi)
            wet += self.line.read(n, (base + depth * np.sin(ph)) * self.sr)
        self.line.advance(n)
        wet /= len(self.VOICES)
        return (1.0 - 0.4 * self.mix) * x + 0.8 * self.mix * wet


class Echo:
    def __init__(self, sr, max_s=1.0):
        self.sr = sr
        self.cap = int(sr * max_s) + 8
        self.buf = np.zeros(self.cap, dtype=np.float64)
        self.w = 0
        self.mix, self.feedback, self.delay_s = 0.0, 0.35, 0.25
        self._active = False

    def process(self, x):
        if self.mix <= 1e-3:
            if self._active:
                self.buf[:] = 0.0
                self._active = False
            return x
        self._active = True
        n = len(x)
        D = int(min(max(self.delay_s * self.sr, 16), self.cap - 4))
        wet = np.empty(n)
        i = 0
        while i < n:
            m = min(D, n - i)
            ar = np.arange(m)
            d = self.buf[(self.w - D + ar) % self.cap]
            self.buf[(self.w + ar) % self.cap] = x[i:i + m] + self.feedback * d
            wet[i:i + m] = d
            self.w = (self.w + m) % self.cap
            i += m
        return x + self.mix * wet


class Reverb:
    _COMB = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)
    _AP = (556, 441, 341, 225)
    _AP_G = 0.5

    def __init__(self, sr):
        k = sr / 44100.0
        self.combs = [np.zeros(max(8, int(round(t * k)))) for t in self._COMB]
        self.comb_w = [0] * len(self.combs)
        self.aps = [np.zeros(max(8, int(round(t * k)))) for t in self._AP]
        self.ap_w = [0] * len(self.aps)
        self.mix, self.room, self._active = 0.0, 0.5, False

    @staticmethod
    def _comb(x, buf, w, fb):
        n, B = len(x), len(buf)
        out = np.empty(n)
        i = 0
        while i < n:
            m = min(B, n - i)
            r = (w + np.arange(m)) % B
            d = buf[r]
            buf[r] = x[i:i + m] + fb * d
            out[i:i + m] = d
            w = (w + m) % B
            i += m
        return out, w

    @staticmethod
    def _allpass(x, buf, w, g):
        n, B = len(x), len(buf)
        out = np.empty(n)
        i = 0
        while i < n:
            m = min(B, n - i)
            r = (w + np.arange(m)) % B
            d = buf[r]
            xi = x[i:i + m]
            buf[r] = xi + g * d
            out[i:i + m] = -g * xi + (1.0 - g * g) * d
            w = (w + m) % B
            i += m
        return out, w

    def process(self, x):
        if self.mix <= 1e-3:
            if self._active:
                for b in self.combs + self.aps:
                    b[:] = 0.0
                self._active = False
            return x
        self._active = True
        fb = 0.65 + 0.27 * self.room
        norm = math.sqrt(1.0 - fb * fb) / math.sqrt(len(self.combs))
        wet = np.zeros(len(x))
        for i, buf in enumerate(self.combs):
            d, self.comb_w[i] = self._comb(x, buf, self.comb_w[i], fb)
            wet += d
        wet *= norm
        for i, buf in enumerate(self.aps):
            wet, self.ap_w[i] = self._allpass(wet, buf, self.ap_w[i], self._AP_G)
        return (1.0 - 0.4 * self.mix) * x + 1.2 * self.mix * wet


class Compressor:
    THRESH = 0.25
    def __init__(self, sr):
        self.sr, self.amount, self._env, self._gain = sr, 0.0, 0.0, 1.0

    def process(self, x):
        n = len(x)
        if self.amount <= 1e-3 and abs(self._gain - 1.0) < 1e-4:
            self._env, self._gain = 0.0, 1.0
            return x
        peak = float(np.max(np.abs(x)))
        self._env = max(peak, self._env * math.exp(-n / (self.sr * 0.3)))
        target = 1.0
        if self.amount > 1e-3:
            T, ratio, env = self.THRESH, 1.0 + self.amount * 7.0, self._env
            squashed = T + (env - T) / ratio if env > T else env
            makeup = 0.5 / (T + (0.5 - T) / ratio)
            target = (squashed / env if env > 1e-9 else 1.0) * makeup
        ramp = int(self.sr * 0.003) if target < self._gain else n
        g = gain_ramp(self._gain, target, n, ramp)
        self._gain = target
        return x * g


class Limiter:
    def __init__(self, sr, ceiling=0.97):
        self.sr, self.ceil, self.gain = sr, ceiling, 1.0

    def process(self, y):
        n = len(y)
        peak = float(np.max(np.abs(y)))
        need = self.ceil / peak if peak > self.ceil else 1.0
        if need < self.gain:
            target, ramp = need, min(n, 32)
        else:
            rel = 1.0 - math.exp(-n / (self.sr * 0.2))
            target, ramp = min(need, self.gain + (1.0 - self.gain) * rel), n
        out = y * gain_ramp(self.gain, target, n, ramp)
        self.gain = target
        return np.clip(out, -0.99, 0.99)


class SpectralProcessor:
    N, H = 1024, 256
    WHISPER_GAIN = 1.9

    def __init__(self, sr):
        self.sr = sr
        self.win = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(self.N) / self.N)
        self.formant, self.denoise, self.whisper = 1.0, 0.0, 0.0
        self.Q = max(8, int(sr * 0.002))
        self.rng = np.random.default_rng(7)
        self._reset()

    def _reset(self):
        self.inbuf = np.zeros(0)
        self.acc = np.zeros(self.N)
        self.out = np.zeros(self.N)
        self.noise = None
        self.gprev = None
        self.active = False

    def _frame(self, fr):
        N = self.N
        X = np.fft.rfft(fr * self.win)
        mag = np.abs(X)
        if self.denoise > 1e-3:
            if self.noise is None:
                self.noise = mag.copy()
            self.noise = np.where(mag < self.noise, 0.8 * self.noise + 0.2 * mag,
                                  self.noise * 1.003 + 1e-10)
            alpha = 1.0 + 3.0 * self.denoise
            floor = 0.05 + 0.25 * (1.0 - self.denoise)
            g = np.maximum(1.0 - alpha * self.noise / (mag + 1e-12), floor)
            g = np.convolve(g, (0.25, 0.5, 0.25), mode="same")
            if self.gprev is not None:
                g = np.where(g > self.gprev, g, 0.6 * self.gprev + 0.4 * g)
            self.gprev = g
            X = X * g
            mag = mag * g
        if abs(self.formant - 1.0) > 1e-3:
            cep = np.fft.irfft(np.log(mag + 1e-9), N)
            cep[self.Q:N - self.Q + 1] = 0.0
            env = np.fft.rfft(cep).real
            k = np.arange(N // 2 + 1)
            new_env = np.interp(k / self.formant, k, env)
            X = X * np.exp(np.clip(new_env - env, -4.0, 4.0))
        if self.whisper > 1e-3:
            ph = np.exp(1j * self.rng.uniform(0.0, 2.0 * np.pi, X.shape))
            ph[0] = ph[-1] = 1.0
            a = self.whisper * 0.5 * np.pi
            X = X * math.cos(a) + np.abs(X) * ph * math.sin(a) * self.WHISPER_GAIN
        return np.fft.irfft(X, N) * self.win / 1.5

    def process(self, x):
        if (abs(self.formant - 1.0) <= 1e-3 and self.denoise <= 1e-3
                and self.whisper <= 1e-3):
            if self.active:
                self._reset()
            return x
        self.active = True
        n = len(x)
        self.inbuf = np.concatenate((self.inbuf, x))
        N, H = self.N, self.H
        while len(self.inbuf) >= N:
            self.acc += self._frame(self.inbuf[:N])
            self.out = np.concatenate((self.out, self.acc[:H]))
            self.acc = np.concatenate((self.acc[H:], np.zeros(H)))
            self.inbuf = self.inbuf[H:]
        y, self.out = self.out[:n], self.out[n:]
        if len(y) < n:
            y = np.concatenate((y, np.zeros(n - len(y))))
        return y


class Harmonizer:
    def __init__(self, sr):
        self.ps = PitchShifter(sr)
        self.mix, self.interval, self._active = 0.0, 7.0, False

    def process(self, x):
        if self.mix <= 1e-3:
            if self._active:
                self.ps.line.clear()
                self._active = False
            return x
        self._active = True
        self.ps.ratio = 2.0 ** (self.interval / 12.0)
        sh = self.ps.process(x)
        return (1.0 - 0.25 * self.mix) * x + 0.75 * self.mix * sh


class ScreamBoost:
    """«Крик-буст» как у стримеров: пока говоришь тихо и нормально, звук чистый.
    Как только уровень переходит порог (крик), плавно включается тяжёлый бас:
    усиление низов + суб-октава + перегруз, а в момент прорыва звучит громовой «бум»."""
    def __init__(self, sr):
        self.sr = sr
        self.amount, self.thr_db, self.boom = 0.0, -10.0, 0.0
        self.a = 0.0                       # насколько «крик» сейчас (0..1), сглаженно
        self.cool = 0
        self.calm = 0                      # сколько сэмплов подряд было спокойно
        self.low = FIRFilter(sr)
        self.sub_lp = FIRFilter(sr)
        self.sub = PitchShifter(sr)
        self.sub.ratio = 0.5               # октава вниз
        self.low.set_response(lambda f: 10.0 ** ((6.0 / (1.0 + (f / 200.0) ** 2)) / 20.0))
        self.sub_lp.set_response(lambda f: 1.0 / (1.0 + (f / 160.0) ** 4))
        n = int(sr * 0.45)
        t = np.arange(n) / sr
        f = 38.0 + 70.0 * np.exp(-t / 0.07)                 # частота «падает» 108 -> 38 Гц
        env = np.minimum(t / 0.004, 1.0) * np.exp(-t / 0.11)
        crack = np.random.default_rng(5).uniform(-1.0, 1.0, n) * np.exp(-t / 0.012) * 0.35
        self.burst = np.sin(2.0 * np.pi * np.cumsum(f) / sr) * env * 0.9 + crack
        self.bpos = len(self.burst)

    def process(self, x):
        if self.amount <= 1e-3 and self.boom <= 1e-3:
            self.a, self.bpos = 0.0, len(self.burst)
            return x
        n = len(x)
        pk = float(np.max(np.abs(x)))
        t = min(1.0, max(0.0, (lin2db(pk) - self.thr_db) / 6.0))
        k = 1.0 - math.exp(-n / (self.sr * (0.015 if t > self.a else 0.07)))
        a_new = self.a + (t - self.a) * k
        env = gain_ramp(self.a, a_new, n)
        self.cool -= n
        if self.boom > 1e-3 and self.cool <= 0 and t > 0.5 and self.calm >= self.sr * 0.25:
            self.bpos, self.cool = 0, int(self.sr * 1.0)      # новый «бум», не чаще раза в секунду
        if t < 0.15:                                      # гистерезис: сброс только при настоящем крике
            self.calm += n
        elif t > 0.5:
            self.calm = 0
        self.a = a_new
        out = x
        if self.amount > 1e-3:
            w = self.amount
            gain = db2lin(12.0 + 14.0 * w)                 # +12...+26 дБ
            pre = self.low.process(x) + 0.7 * self.sub_lp.process(self.sub.process(x))
            loud = 0.95 * np.tanh(gain * pre)              # жёсткое ограничение: крик «упирается в потолок»
            out = x * (1.0 - env) + loud * env
        if self.bpos < len(self.burst):
            seg = self.burst[self.bpos:self.bpos + n]
            out = np.array(out, dtype=np.float64, copy=True)
            out[:len(seg)] += seg * (0.9 * self.boom)
            self.bpos += n
        return out


class EffectChain:
    def __init__(self, sample_rate: int):
        self.sr = int(sample_rate)
        self.p = dict(DEFAULTS)
        self._build()

    def _build(self):
        sr = self.sr
        self.spec = SpectralProcessor(sr)
        self.gate = NoiseGate(sr)
        self.tone = FIRFilter(sr)
        self.pitch = PitchShifter(sr)
        self.harm = Harmonizer(sr)
        self.vibrato = Vibrato(sr)
        self.tremolo = Tremolo(sr)
        self.ring = RingModulator(sr)
        self.crusher = Bitcrusher()
        self.dist = Distortion()
        self.chorus = Chorus(sr)
        self.echo = Echo(sr)
        self.reverb = Reverb(sr)
        self.scream = ScreamBoost(sr)
        self.comp = Compressor(sr)
        self.limiter = Limiter(sr)
        self._gain = 1.0
        self._tone_key = None
        self.set_params(self.p)

    def reset(self):
        self._build()

    def set_params(self, params: dict):
        p = sanitize_params(params)
        self.p = p
        self.gate.threshold = p["gate"]
        self.pitch.ratio = 2.0 ** (p["pitch"] / 12.0)
        self.spec.formant = 1.0 + p["formant"]
        self.spec.denoise, self.spec.whisper = p["denoise"], p["whisper"]
        self.harm.mix, self.harm.interval = p["harmony"], p["harm_int"]
        self.vibrato.depth = p["vibrato"] * 0.006
        self.vibrato.rate = p["vib_rate"]
        self.tremolo.depth = p["tremolo"]
        self.tremolo.rate = p["trem_rate"]
        self.ring.mix, self.ring.freq = p["ring"], p["ring_freq"]
        self.crusher.amount = p["crush"]
        self.dist.drive = p["drive"]
        self.chorus.mix = p["chorus"]
        self.echo.mix, self.echo.delay_s = p["echo"], p["echo_time"]
        self.echo.feedback = p["echo_fb"]
        self.reverb.mix, self.reverb.room = p["reverb"], p["room"]
        self.comp.amount = p["comp"]
        self.scream.amount, self.scream.thr_db, self.scream.boom = p["scream"], p["scream_thr"], p["scream_boom"]
        self.gain_target = p["gain"]
        self._update_tone(p)

    def _update_tone(self, p):
        key = (round(p["bass"], 2), round(p["treble"], 2), bool(p["bandpass"]))
        if key == self._tone_key:
            return
        self._tone_key = key
        bass, treble, phone = key
        if abs(bass) < 0.1 and abs(treble) < 0.1 and not phone:
            self.tone.set_response(None)
            return

        def response(f):
            low = 1.0 / (1.0 + (f / 250.0) ** 2)
            high = 1.0 / (1.0 + (3500.0 / np.maximum(f, 1.0)) ** 2)
            h = 10.0 ** ((bass * low + treble * high) / 20.0)
            if phone:
                lo = 1.0 / (1.0 + np.exp(np.clip((300.0 - f) / 50.0, -60, 60)))
                hi = 1.0 / (1.0 + np.exp(np.clip((f - 3400.0) / 50.0, -60, 60)))
                h = h * lo * hi
            return h

        self.tone.set_response(response)

    def process(self, x: np.ndarray) -> np.ndarray:
        y = np.asarray(x, dtype=np.float64)
        y = self.spec.process(y)
        y = self.gate.process(y)
        y = self.tone.process(y)
        y = self.pitch.process(y)
        y = self.harm.process(y)
        y = self.vibrato.process(y)
        y = self.tremolo.process(y)
        y = self.ring.process(y)
        y = self.crusher.process(y)
        y = self.dist.process(y)
        y = self.chorus.process(y)
        y = self.echo.process(y)
        y = self.reverb.process(y)
        y = self.scream.process(y)
        y = self.comp.process(y)
        y = y * gain_ramp(self._gain, self.gain_target, len(y))
        self._gain = self.gain_target
        return self.limiter.process(y).astype(np.float32)


class RingBuffer:
    def __init__(self, capacity: int):
        self.cap = int(capacity)
        self.buf = np.zeros(self.cap, dtype=np.float32)
        self.r = self.w = self.count = 0
        self.overruns = 0
        self.lock = threading.Lock()

    def write(self, x: np.ndarray):
        n = len(x)
        with self.lock:
            if n > self.cap:
                x, n = x[-self.cap:], self.cap
            over = self.count + n - self.cap
            if over > 0:
                self.r = (self.r + over) % self.cap
                self.count -= over
                self.overruns += 1
            first = min(n, self.cap - self.w)
            self.buf[self.w:self.w + first] = x[:first]
            if n > first:
                self.buf[:n - first] = x[first:n]
            self.w = (self.w + n) % self.cap
            self.count += n

    def read(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        with self.lock:
            k = min(n, self.count)
            first = min(k, self.cap - self.r)
            out[:first] = self.buf[self.r:self.r + first]
            if k > first:
                out[first:k] = self.buf[:k - first]
            self.r = (self.r + k) % self.cap
            self.count -= k
        return out

    def discard(self, n: int):
        with self.lock:
            n = max(0, min(n, self.count))
            self.r = (self.r + n) % self.cap
            self.count -= n


class AudioEngine:
    def __init__(self):
        self.in_stream = None
        self.out_stream = None
        self.chain = None
        self.q = None
        self.params = dict(DEFAULTS)
        self.sr = 48000
        self.block = 512
        self.in_ch = self.out_ch = 1
        self.bypass = False
        self.mute = False
        self.post_hook = None
        self.peak_in = 0.0
        self.peak_out = 0.0
        self.xruns = 0
        self.trims = 0
        self.error = ""
        self.scope = np.zeros(SCOPE_N, dtype=np.float32)
        self._primed = False
        self._rec = None
        self._rec_n = 0
        self._warm = 0                  # сколько сэмплов «прогрева» осталось после открытия устройства
        self._mg = self._mg_t = 0.0     # общая громкость выхода (плавный fade-in / fade-out)
        self._by = self._mu = 0.0       # плавные переключатели «Оригинал» и «Заглушить»

    @property
    def running(self) -> bool:
        return self.in_stream is not None

    @property
    def healthy(self) -> bool:
        try:
            return bool(self.in_stream and self.out_stream
                        and self.in_stream.active and self.out_stream.active)
        except Exception:
            return False

    @property
    def latency_ms(self) -> float:
        try:
            lat = float(self.in_stream.latency) + float(self.out_stream.latency)
            return (lat + (self.q.count if self.q else 0) / self.sr) * 1000.0
        except Exception:
            return 0.0

    def set_params(self, p: dict):
        self.params = sanitize_params(p)
        if self.chain is not None:
            self.chain.set_params(self.params)

    def take_levels(self):
        a, b = self.peak_in, self.peak_out
        self.peak_in = self.peak_out = 0.0
        return a, b

    @staticmethod
    def devices():
        ins, outs = [], []
        apis = [h["name"] for h in sd.query_hostapis()]
        for i, d in enumerate(sd.query_devices()):
            label = f"{d['name']}  [{apis[d['hostapi']]}]"
            if d["max_input_channels"] > 0:
                ins.append((i, label))
            if d["max_output_channels"] > 0:
                outs.append((i, label))
        return ins, outs

    @staticmethod
    def reinit_portaudio():
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass

    @staticmethod
    def _negotiate(in_idx, out_idx, preferred):
        def default_sr(idx):
            try:
                return int(sd.query_devices(idx)["default_samplerate"])
            except Exception:
                return 0

        cands = []
        for s in (preferred, 48000, 44100, default_sr(in_idx), default_sr(out_idx)):
            if s and s not in cands:
                cands.append(s)
        last = None
        for s in cands:
            in_ch = out_ch = None
            for ch in (1, 2):
                try:
                    sd.check_input_settings(device=in_idx, channels=ch, dtype="float32", samplerate=s)
                    in_ch = ch
                    break
                except Exception as exc:
                    last = exc
            for ch in (1, 2):
                try:
                    sd.check_output_settings(device=out_idx, channels=ch, dtype="float32", samplerate=s)
                    out_ch = ch
                    break
                except Exception as exc:
                    last = exc
            if in_ch and out_ch:
                return s, in_ch, out_ch
        raise RuntimeError(f"Устройства не поддерживают общий формат звука ({last})")

    def start(self, in_idx, out_idx, samplerate, blocksize, params):
        if self.running:
            return
        sr, self.in_ch, self.out_ch = self._negotiate(int(in_idx), int(out_idx), int(samplerate))
        self.sr, self.block = sr, int(blocksize)
        self.error = ""
        self.xruns = self.trims = 0
        self.peak_in = self.peak_out = 0.0
        self.scope = np.zeros(SCOPE_N, dtype=np.float32)
        self.params = sanitize_params(params)
        self.chain = EffectChain(sr)
        self.chain.set_params(self.params)
        cap = max(int(sr * 0.25), self.block * 10)
        self.q = RingBuffer(cap)
        self.prime_n = self.block * 3
        self.max_fill = max(self.block * 6, int(sr * 0.08))
        self._primed = False
        # Старт без щелчков: ~250 мс после открытия устройства звук выбрасываем
        # (драйвер часто выдаёт громкий переходной «стук»), затем плавно нарастаем.
        self._warm = int(sr * 0.25)
        self._mg = self._mg_t = 0.0
        self._by = 1.0 if self.bypass else 0.0
        self._mu = 1.0 if self.mute else 0.0
        try:
            self.in_stream = sd.InputStream(
                device=int(in_idx), samplerate=sr, channels=self.in_ch,
                dtype="float32", blocksize=self.block, callback=self._in_cb)
            self.out_stream = sd.OutputStream(
                device=int(out_idx), samplerate=sr, channels=self.out_ch,
                dtype="float32", blocksize=self.block, callback=self._out_cb)
            self.in_stream.start()
            deadline = time.time() + 0.5
            while self.q.count < self.prime_n and time.time() < deadline:
                time.sleep(0.005)
            self.out_stream.start()
        except Exception:
            self.stop()
            raise

    def stop(self):
        try:
            if self.healthy:                 # плавно гасим звук, потом закрываем потоки
                self._mg_t = 0.0
                time.sleep(0.09)
        except Exception:
            pass
        for s in (self.in_stream, self.out_stream):
            if s is not None:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
        self.in_stream = self.out_stream = None
        self.peak_in = self.peak_out = 0.0

    def _smooth(self, cur, tgt, n, ms):
        """Плавный переход cur -> tgt за ~ms мс. Возвращает (новое значение, массив по сэмплам | None)."""
        if cur == tgt:
            return cur, None
        step = n / max(1.0, self.sr * ms / 1000.0)
        new = tgt if abs(tgt - cur) <= step else cur + (step if tgt > cur else -step)
        return new, cur + (new - cur) * (np.arange(1, n + 1) / n)

    def _in_cb(self, indata, frames, time_info, status):
        try:
            if status and status.input_overflow:
                self.xruns += 1
            x = indata[:, 0] if self.in_ch == 1 else indata.mean(axis=1)
            n = len(x)
            if self._warm > 0:
                self._warm -= n
                if self._warm <= 0:
                    self._mg_t = 1.0
                y = np.zeros(n, dtype=np.float32)
            else:
                self.peak_in = max(self.peak_in, float(np.max(np.abs(x))))
                dry = np.asarray(x, dtype=np.float32)
                wet = self.chain.process(x)        # цепочка работает всегда: эффекты не «залипают»
                if not np.isfinite(wet).all():
                    self.chain.reset()
                    wet = np.zeros(n, dtype=np.float32)
                by, r = self._smooth(self._by, 1.0 if self.bypass else 0.0, n, 20.0)
                self._by = by
                if r is not None:
                    y = (wet * (1.0 - r) + dry * r).astype(np.float32)
                else:
                    y = dry if by >= 1.0 else wet
                mu, r = self._smooth(self._mu, 1.0 if self.mute else 0.0, n, 10.0)
                self._mu = mu
                if r is not None:
                    y = (y * (1.0 - r)).astype(np.float32)
                elif mu >= 1.0:
                    y = np.zeros(n, dtype=np.float32)
            hook = self.post_hook
            if hook is not None:
                y = hook(y)
        except Exception as exc:
            self.error = str(exc)
            y = np.zeros(frames, dtype=np.float32)
        self.peak_out = max(self.peak_out, float(np.max(np.abs(y))))
        self.scope = np.concatenate((self.scope, y))[-SCOPE_N:]
        rec = self._rec
        if rec is not None:
            rec.append((np.clip(y, -1, 1) * 32767).astype(np.int16))
            self._rec_n += len(y)
            if self._rec_n > self.sr * 60 * 30:
                self._rec = None
        self.q.write(y)

    def _out_cb(self, outdata, frames, time_info, status):
        q = self.q
        if status and status.output_underflow:
            self.xruns += 1
        if not self._primed:
            if q.count >= self.prime_n:
                self._primed = True
            else:
                outdata.fill(0)
                return
        if q.count < frames:
            self.xruns += 1
            self._primed = False
            outdata.fill(0)
            return
        data = q.read(frames)
        over = q.count - self.max_fill
        if over > 0:
            q.discard(over)
            self.trims += 1
        mg, tgt = self._mg, self._mg_t
        if mg != 1.0 or tgt != 1.0:                      # fade-in при старте / fade-out при остановке
            step = frames / max(1.0, self.sr * 0.06)
            new = tgt if abs(tgt - mg) <= step else mg + (step if tgt > mg else -step)
            if new == mg:
                data = data * np.float32(mg)
            else:
                data = data * (mg + (new - mg) * (np.arange(1, frames + 1) / frames)).astype(np.float32)
            self._mg = new
        outdata[:] = data.reshape(-1, 1)

    def start_record(self):
        self._rec_n = 0
        self._rec = []

    def stop_record(self):
        rec, self._rec = self._rec, None
        return np.concatenate(rec) if rec else None


class VirtualMic:
    SINK = "voicechanger_sink"
    SOURCE = "voicechanger_mic"

    @staticmethod
    def available() -> bool:
        return shutil.which("pactl") is not None

    @staticmethod
    def _run(*args):
        env = dict(os.environ, LC_ALL="C")
        try:
            r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=6, env=env)
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except Exception as exc:
            return 1, "", str(exc)

    @classmethod
    def _module_ids(cls, needle):
        _, out, _ = cls._run("list", "short", "modules")
        ids = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and needle in line:
                ids.append(parts[0])
        return ids

    @classmethod
    def destroy(cls):
        for needle in (f"source={cls.SINK}.monitor", f"source_name={cls.SOURCE}", f"sink_name={cls.SINK}"):
            for mid in cls._module_ids(needle):
                cls._run("unload-module", mid)

    @classmethod
    def create(cls):
        cls.destroy()
        rc, out, err = cls._run("load-module", "module-null-sink", f"sink_name={cls.SINK}", "sink_properties=device.description=VoiceChanger_Sink")
        if rc != 0:
            raise RuntimeError(err or out or "pactl не смог создать виртуальный выход")
        rc, out, err = cls._run("load-module", "module-remap-source", f"master={cls.SINK}.monitor", f"source_name={cls.SOURCE}", "source_properties=device.description=VoiceChanger_Mic")
        if rc != 0:
            cls.destroy()
            raise RuntimeError(err or out or "pactl не смог создать виртуальный микрофон")

    @classmethod
    def start_loopback(cls):
        if cls._module_ids(f"source={cls.SINK}.monitor"):
            return
        rc, out, err = cls._run("load-module", "module-loopback", f"source={cls.SINK}.monitor", "latency_msec=40")
        if rc != 0:
            raise RuntimeError(err or out or "не удалось включить прослушивание")

    @classmethod
    def stop_loopback(cls):
        for mid in cls._module_ids(f"source={cls.SINK}.monitor"):
            cls._run("unload-module", mid)

    @classmethod
    def route_process(cls, pid: int) -> int:
        _, out, _ = cls._run("list", "sink-inputs")
        moved = 0
        for block in re.split(r"\n(?=Sink Input #)", out):
            m = re.match(r"\s*Sink Input #(\d+)", block)
            if m and re.search(rf'application\.process\.id = "{pid}"', block):
                rc, _, _ = cls._run("move-sink-input", m.group(1), cls.SINK)
                moved += rc == 0
        return moved


class KeepAwake:
    """Держит выход звуковой карты «разбуженным» тихим потоком (одни нули),
    пока движок выключен. Когда движок работает, этот поток закрыт: у движка
    есть свой, и два потока на одной карте друг другу только мешают."""
    def __init__(self):
        self.stream = None
        self.key = None

    def start(self, device, rate=None):
        key = (device, rate)
        if self.stream is not None and self.key == key:
            return True
        self.stop()
        saved = os.environ.pop("PULSE_SINK", None)     # нужна реальная карта, а не виртуальный выход
        try:
            try:
                info = sd.query_devices(device, "output") if device is not None else sd.query_devices(kind="output")
                dsr = int(info["default_samplerate"] or 48000)
                chans = (2, 1) if int(info["max_output_channels"]) >= 2 else (1,)
            except Exception:
                dsr, chans = 48000, (2, 1)
            rates = [r for r in (rate, dsr, 48000) if r]
            rates = [r for n, r in enumerate(rates) if r not in rates[:n]]
            for sr in rates:
                for ch in chans:
                    try:
                        st = sd.OutputStream(device=device, samplerate=sr, channels=ch, dtype="float32",
                                             blocksize=2048, callback=lambda out, f, t, s: out.fill(0))
                        st.start()
                        self.stream, self.key = st, key
                        return True
                    except Exception:
                        continue
            return False
        finally:
            if saved is not None:
                os.environ["PULSE_SINK"] = saved

    def stop(self):
        st, self.stream, self.key = self.stream, None, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass


# ===========================================================================
#  САУНДБОРД
# ===========================================================================

class SoundBoard:
    def __init__(self):
        self.volume = 1.0
        self._active = []
        self._lock = threading.Lock()
        self._cache = {}
        self._paused = False
        self._pg = 1.0               # плавная «громкость паузы» (1 — играет, 0 — пауза)
        self._fading = []            # затухающие хвосты прерванных треков

    @staticmethod
    def read_wave(src):
        with wave.open(src, "rb") as w:
            ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
        if sw == 1:
            a = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sw == 2:
            a = np.frombuffer(raw[:len(raw) // 2 * 2], "<i2").astype(np.float32) / 32768.0
        elif sw == 4:
            a = np.frombuffer(raw[:len(raw) // 4 * 4], "<i4").astype(np.float32) / 2147483648.0
        else:
            raise ValueError("Поддерживается WAV 8/16/32 бит.")
        if ch > 1:
            a = a[:len(a) // ch * ch].reshape(-1, ch).mean(axis=1)
        return a, sr

    @staticmethod
    def resample(a, sr_from, sr_to):
        if sr_from == sr_to or len(a) < 2:
            return a.astype(np.float32)
        n = max(1, int(len(a) * sr_to / sr_from))
        return np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)

    FADE = 288          # ~6 мс: короткие фейды вместо резких обрывов (иначе слышны щелчки)

    def _push_fading(self, item, gain=1.0):
        a, pos = item[0], item[1]
        if pos < len(a):
            self._fading.append([a, pos, self.FADE, float(gain)])

    def _silent(self):
        return self._paused and self._pg <= 0.0

    def play_array(self, a):
        with self._lock:
            if self._active and not self._silent():
                self._push_fading(self._active[0], self._pg)     # прежний трек плавно затухает
            self._paused = False
            self._pg = 1.0
            self._active = [[a, 0, 0]]      # один трек за раз: новый заменяет текущий
        return a

    def play_file(self, path, sr):
        key = (path, sr)
        if key not in self._cache:
            a, fsr = decode_audio(path)
            self._cache[key] = self.resample(a, fsr, sr)
        return self.play_array(self._cache[key])

    def stop(self):
        with self._lock:
            if self._active and not self._silent():
                self._push_fading(self._active[0], self._pg)
            self._active = []
            self._paused = False
            self._pg = 1.0

    @property
    def busy(self):
        return bool(self._active)

    def get_progress(self):
        with self._lock:
            if not self._active:
                return 0, 0, self._paused, False
            a, pos = self._active[0][0], self._active[0][1]
            return pos, len(a), self._paused, True

    def toggle_pause(self):
        with self._lock:
            self._paused = not self._paused
            return self._paused

    def seek(self, fraction):
        with self._lock:
            if self._active:
                item = self._active[0]
                if not self._silent():
                    self._push_fading(item, self._pg)
                item[1] = int(min(max(fraction, 0.0), 1.0) * len(item[0]))
                item[2] = 0

    def mix(self, y):
        with self._lock:
            if not self._active and not self._fading:
                return y
            out = np.array(y, dtype=np.float32, copy=True)
            n, F = len(out), float(self.FADE)
            if self._active and not self._silent():
                item = self._active[0]
                a, pos, fin = item
                chunk = a[pos:pos + n]
                m = len(chunk)
                if m:
                    s = np.arange(m)
                    g = np.minimum(np.minimum((fin + s + 1) / F, (len(a) - pos - s) / F), 1.0)
                    target = 0.0 if self._paused else 1.0
                    if self._pg != target:
                        d = np.arange(1, m + 1) / F
                        pg = np.clip(self._pg + (d if target > self._pg else -d), 0.0, 1.0)
                        self._pg = float(pg[-1])
                        g = g * pg
                    out[:m] += chunk * g * self.volume
                item[1], item[2] = pos + m, fin + m
                if item[1] >= len(a):
                    self._active = []
            keep = []
            for f in self._fading:
                a, pos, left, g0 = f
                m = min(n, left, len(a) - pos)
                if m > 0:
                    g = g0 * (left - np.arange(m)) / F
                    out[:m] += a[pos:pos + m] * g * self.volume
                    f[1], f[2] = pos + m, left - m
                if f[2] > 0 and f[1] < len(a):
                    keep.append(f)
            self._fading = keep
        return np.clip(out, -0.99, 0.99)

AUDIO_EXTS = (".wav", ".mp3", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus", ".wma", ".aiff", ".aif")


def decode_audio(path):
    try:
        return SoundBoard.read_wave(path)
    except Exception as wav_err:
        ff = shutil.which("ffmpeg")
        if not ff:
            raise RuntimeError(f"{wav_err}\nДля MP3/OGG/FLAC нужно установить ffmpeg.")
        r = subprocess.run([ff, "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "48000", "-"], capture_output=True, timeout=120)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(r.stderr.decode(errors="ignore").strip() or "ffmpeg не смог прочитать файл")
        a = np.frombuffer(r.stdout[:len(r.stdout) // 2 * 2], "<i2")
        return a.astype(np.float32) / 32768.0, 48000


def sound_bars(path: str, n: int = 40) -> np.ndarray:
    a, _ = decode_audio(path)
    if len(a) < n:
        return np.zeros(n)
    pk = np.array([c.max() if len(c) else 0.0 for c in np.array_split(np.abs(a), n)])
    m = pk.max()
    return pk / m if m > 0 else pk


def array_bars(a, n: int = 96) -> np.ndarray:
    """Огибающая звука для волны в плеере (быстро, без декодирования файла)."""
    if a is None or len(a) < n:
        return np.zeros(n)
    idx = np.linspace(0, len(a), n, endpoint=False).astype(np.int64)
    pk = np.maximum.reduceat(np.abs(np.asarray(a, dtype=np.float32)), idx)
    m = float(np.percentile(pk, 98))
    return np.minimum(pk / m, 1.0) if m > 1e-6 else np.zeros(n)


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def espeak_exe():
    return shutil.which("espeak-ng") or shutil.which("espeak")


# ---------------------------------------------------------------------------
#  ОЗВУЧКА ТЕКСТА: Piper (нейросеть, офлайн) + espeak-ng как запасной вариант
# ---------------------------------------------------------------------------

PIPER_REPO = "rhasspy/piper-voices/resolve/main/"
# Пробуем по очереди: HuggingFace, затем зеркало (на случай блокировки)
PIPER_HOSTS = ("https://huggingface.co/", "https://hf-mirror.com/")

# (язык, название, путь в репозитории rhasspy/piper-voices без расширения)
PIPER_CATALOG = (
    ("ru", "Ирина (жен.)",   "ru/ru_RU/irina/medium/ru_RU-irina-medium"),
    ("ru", "Денис (муж.)",   "ru/ru_RU/denis/medium/ru_RU-denis-medium"),
    ("ru", "Дмитрий (муж.)", "ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium"),
    ("ru", "Руслан (муж.)",  "ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium"),
    ("en", "Lessac (US)",    "en/en_US/lessac/medium/en_US-lessac-medium"),
    ("en", "Ryan (US)",      "en/en_US/ryan/high/en_US-ryan-high"),
    ("uk", "Ukrainian TTS",  "uk/uk_UA/ukrainian_tts/medium/uk_UA-ukrainian_tts-medium"),
    ("de", "Thorsten",       "de/de_DE/thorsten/medium/de_DE-thorsten-medium"),
    ("es", "Davefx",         "es/es_ES/davefx/medium/es_ES-davefx-medium"),
)
_VOICE_NAMES = {os.path.basename(rel): label for _l, label, rel in PIPER_CATALOG}
_PIPER_CACHE = {}


def voices_dir() -> str:
    return os.path.join(_config_dir(), "voices")


def voice_label(base: str) -> str:
    if base in _VOICE_NAMES:
        return _VOICE_NAMES[base]
    parts = base.split("-")
    return f"{parts[1]} ({parts[-1]})" if len(parts) >= 3 else base


def piper_voices() -> list:
    """Установленные голоса Piper: [(язык, имя файла без .onnx, полный путь)]."""
    d, out = voices_dir(), []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for fn in names:
        if fn.endswith(".onnx") and os.path.isfile(os.path.join(d, fn + ".json")):
            base = fn[:-5]
            out.append((base.split("_")[0].split("-")[0].lower(), base, os.path.join(d, fn)))
    return out


def piper_cmd():
    """Команда запуска Piper или None. Ищем в PATH, в ~/.local/bin и в venv pipx
    (pipx кладёт piper в ~/.local/bin, которого может не быть в PATH).
    Для найденного в PATH проверяем, что это именно TTS: в Arch пакет `piper` —
    совсем другая программа (настройка мышей)."""
    if _PIPER_CACHE.get("cmd"):
        return _PIPER_CACHE["cmd"]
    home = os.path.expanduser("~")
    cands = []                                   # (команда, доверенный путь)
    for n in ("piper-tts", "piper"):
        p = shutil.which(n)
        if p:
            cands.append(([p], False))
    for n in ("piper", "piper-tts"):
        p = os.path.join(home, ".local", "bin", n)
        if os.path.isfile(p):
            cands.append(([p], True))
    for root in (os.environ.get("PIPX_HOME"), os.path.join(home, ".local", "share", "pipx"),
                 os.path.join(home, ".local", "pipx")):
        if root:
            py = os.path.join(root, "venvs", "piper-tts", "bin", "python")
            if os.path.isfile(py):
                cands.append(([py, "-m", "piper"], True))
    cands.append(([sys.executable, "-m", "piper"], False))
    found = None
    for c, trusted in cands:
        if trusted:
            found = c
            break
        try:
            r = subprocess.run(c + ["--help"], capture_output=True, timeout=30)
            if "--model" in (r.stdout + r.stderr).decode(errors="ignore"):
                found = c
                break
        except Exception:
            continue
    if found:
        _PIPER_CACHE["cmd"] = found              # неудачу не кэшируем: можно доустановить на лету
    return found


def synth_piper(text: str, model: str, speed: float = 1.0):
    cmd = piper_cmd()
    if not cmd:
        raise RuntimeError("Piper не найден (искал в PATH и ~/.local/bin). Установи: pipx install piper-tts, "
                           "затем pipx ensurepath и перезапусти терминал")
    text = " ".join(text.split())
    fd, out = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        base = cmd + ["-m", model, "-f", out]
        scale = 1.0 / max(0.4, speed)           # length_scale < 1 — быстрее
        last = ""
        for c in (base + ["--length_scale", f"{scale:.3f}"], base):
            r = subprocess.run(c, input=text.encode("utf-8"), capture_output=True, timeout=120)
            if r.returncode == 0 and os.path.getsize(out) > 100:
                return SoundBoard.read_wave(out)
            last = r.stderr.decode(errors="ignore").strip()[-400:]
        raise RuntimeError(last or "Piper не вернул звук")
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def synth_speech(text: str, lang: str, speed: float = 1.0):
    """Запасной вариант — espeak-ng (звучит как робот)."""
    exe = espeak_exe()
    if not exe:
        raise RuntimeError("Не найден espeak-ng.")
    wpm = int(max(80, min(320, 150 * speed)))
    r = subprocess.run([exe, "-v", lang, "-s", str(wpm), "-g", "3", "--stdout", "--stdin"],
                       input=text.encode("utf-8"), capture_output=True, timeout=25)
    if r.returncode != 0 or len(r.stdout) < 100:
        raise RuntimeError(r.stderr.decode(errors="ignore") or "espeak не вернул звук")
    try:
        return SoundBoard.read_wave(io.BytesIO(r.stdout))
    except Exception:
        return np.frombuffer(r.stdout[44:], "<i2").astype(np.float32) / 32768.0, 22050


def _fetch_voice(opener, url_base, rel, progress):
    base = os.path.basename(rel)
    for ext in (".onnx.json", ".onnx"):          # модель последней — пока не докачана, голоса в списке нет
        dest = os.path.join(voices_dir(), base + ext)
        tmp = dest + ".part"
        try:
            with opener.open(url_base + ext, timeout=20) as resp, open(tmp, "wb") as f:
                total, got = int(resp.headers.get("Content-Length") or 0), 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress and total and ext == ".onnx":
                        progress(got / total)
            os.replace(tmp, dest)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def download_piper_voice(rel: str, progress=None, proxy: str = "", hosts=None):
    """Скачивает голос, перебирая зеркала. proxy — HTTP(S)-прокси вида http://127.0.0.1:7890;
    если пусто, используются переменные окружения HTTPS_PROXY/HTTP_PROXY."""
    os.makedirs(voices_dir(), exist_ok=True)
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    errors = []
    for host in (hosts or PIPER_HOSTS):
        try:
            _fetch_voice(opener, host + PIPER_REPO + rel, rel, progress)
            return
        except Exception as exc:
            errors.append(f"{host.split('/')[2]}: {exc}")
    raise RuntimeError("; ".join(errors))


def resample_hq(a, sr_from: int, sr_to: int):
    """Ресемплинг через FFT. Линейная интерполяция (np.interp) даёт «жестяной»
    звон на 22 кГц -> 48 кГц, а тут верха остаются чистыми."""
    a = np.asarray(a, dtype=np.float32)
    sr_from, sr_to = int(sr_from), int(sr_to)
    if sr_from == sr_to or len(a) < 2:
        return a
    g = math.gcd(sr_from, sr_to)
    u_in, u_out = sr_from // g, sr_to // g
    k, need = 1, len(a) + int(sr_from * 0.2)
    while u_in * k < need:
        k *= 2
    n_in, n_out = u_in * k, u_out * k
    pad = (n_in - len(a)) // 2
    x = np.zeros(n_in, dtype=np.float64)
    x[pad:pad + len(a)] = a
    spec = np.fft.rfft(x)
    new = np.zeros(n_out // 2 + 1, dtype=np.complex128)
    m = min(len(spec), len(new))
    new[:m] = spec[:m]
    y = np.fft.irfft(new, n_out) * (n_out / n_in)
    start = int(round(pad * n_out / n_in))
    length = int(round(len(a) * sr_to / sr_from))
    return y[start:start + length].astype(np.float32)


def tts_finish(a, sr_from: int, sr_to: int):
    """Доводка озвучки: чистый ресемплинг, убрать DC, нормализация, мягкие края."""
    a = resample_hq(a, sr_from, sr_to)
    if len(a) == 0:
        return a
    a = a - float(np.mean(a))
    pk = float(np.max(np.abs(a)))
    if pk > 1e-4:
        a = a * (0.9 / pk)
    fade = int(sr_to * 0.012)
    if len(a) > 2 * fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        a[:fade] *= ramp
        a[-fade:] *= ramp[::-1]
    return np.concatenate([a, np.zeros(int(sr_to * 0.15), dtype=np.float32)]).astype(np.float32)


def _config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "VoiceChanger")


def load_user_presets() -> dict:
    try:
        with open(os.path.join(_config_dir(), "presets.json"), encoding="utf-8") as f:
            raw = json.load(f)
        return {str(k): sanitize_params(v) for k, v in raw.items() if isinstance(v, dict) and str(k) not in PRESETS}
    except Exception:
        return {}


def save_user_presets(presets: dict):
    try:
        os.makedirs(_config_dir(), exist_ok=True)
        with open(os.path.join(_config_dir(), "presets.json"), "w", encoding="utf-8") as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def random_params() -> dict:
    r = random.Random()
    p = dict(DEFAULTS)
    p["pitch"] = r.choice([-1, 1]) * r.uniform(1, 9)
    p["gain"] = 1.1
    pool = ["vibrato", "tremolo", "chorus", "ring", "drive", "crush", "echo", "reverb", "harmony", "whisper"]
    for k in r.sample(pool, r.randint(1, 3)):
        p[k] = r.uniform(0.15, 0.7)
    p["ring_freq"] = r.choice([30, 50, 80, 120, 170, 250])
    p["bandpass"] = r.random() < 0.3
    if r.random() < 0.5:
        p["formant"] = r.uniform(-0.3, 0.3)
    p["bass"], p["treble"] = r.randint(-4, 4), r.randint(-4, 4)
    return sanitize_params(p)


# ===========================================================================
#  ГОРЯЧИЕ КЛАВИШИ
# ===========================================================================

HOTKEY_ACTIONS = (
    ("run",         "Старт / стоп смены голоса",          "Ctrl+Return"),
    ("mute",        "Заглушить микрофон",                 "Ctrl+M"),
    ("bypass",      "Оригинальный голос (без эффектов)",  "Ctrl+B"),
    ("rec",         "Запись в WAV",                       "Ctrl+R"),
    ("preset_prev", "Предыдущий пресет",                  "Ctrl+Left"),
    ("preset_next", "Следующий пресет",                   "Ctrl+Right"),
    ("snd_pause",   "Звук: пауза / продолжить",           ""),
    ("snd_prev",    "Звук: предыдущий",                   ""),
    ("snd_next",    "Звук: следующий",                    ""),
)
HOTKEY_DEFAULTS = {a: d for a, _t, d in HOTKEY_ACTIONS}
# русская раскладка -> латинские клавиши, чтобы сочетания не зависели от раскладки
_RU2LAT = dict(zip("йцукенгшщзфывапролдячсмить", "qwertyuiopasdfghjklzxcvbnm"))


def norm_key(seq) -> str:
    """Каноническая запись сочетания («ctrl+m» -> «Ctrl+M»); пустая строка — не назначено."""
    if not isinstance(seq, str) or not seq.strip():
        return ""
    ks = QKeySequence.fromString(seq.strip(), QKeySequence.SequenceFormat.PortableText)
    return "" if ks.isEmpty() else ks.toString(QKeySequence.SequenceFormat.PortableText)


def key_text(seq) -> str:
    """Сочетание в виде для показа пользователю."""
    seq = norm_key(seq)
    if not seq:
        return ""
    return QKeySequence.fromString(seq, QKeySequence.SequenceFormat.PortableText).toString(
        QKeySequence.SequenceFormat.NativeText)


# ===========================================================================
#  ВИДЖЕТЫ ИНТЕРФЕЙСА
# ===========================================================================

class LevelMeter(QWidget):
    def __init__(self, title: str):
        super().__init__()
        self._title = title
        self._lvl = self._peak = 0.0
        self._hold = 0
        self.setFixedHeight(22)
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    @staticmethod
    def _pos(lin: float) -> float:
        return min(1.0, max(0.0, (lin2db(lin) + 60.0) / 60.0))

    def set_level(self, lin: float):
        p = self._pos(lin)
        self._lvl = p if p >= self._lvl else max(p, self._lvl - 0.05)
        if p >= self._peak:
            self._peak, self._hold = p, 30
        elif self._hold > 0:
            self._hold -= 1
        else:
            self._peak = max(0.0, self._peak - 0.015)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        label_w = 34
        p.setPen(QColor(T("text")))
        p.drawText(QRectF(0, 0, label_w, self.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._title)
        bar = QRectF(label_w, 3, self.width() - label_w - 1, self.height() - 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T("input")))
        p.setPen(QPen(QColor(T("border")), 1))
        p.drawRoundedRect(bar, 4, 4)
        p.setPen(Qt.PenStyle.NoPen)
        grad = QLinearGradient(bar.left(), 0, bar.right(), 0)
        grad.setColorAt(0.0, QColor("#1fa971"))
        grad.setColorAt(0.70, QColor("#35c27f"))
        grad.setColorAt(0.90, QColor("#e0c93b"))
        grad.setColorAt(1.0, QColor("#e5484d"))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * self._lvl, bar.height()), 4, 4)
        x = int(bar.left() + bar.width() * self._peak)
        p.setPen(QPen(QColor(240, 240, 240), 2))
        p.drawLine(x, int(bar.top()), x, int(bar.bottom()))


class SpectrumWidget(QWidget):
    BARS = 48
    def __init__(self):
        super().__init__()
        self._vals = np.zeros(self.BARS)
        self._win = np.hanning(SCOPE_N)
        self.setMinimumHeight(86)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_samples(self, x, sr):
        if x is None:
            self._vals *= 0.85
        else:
            mag = np.abs(np.fft.rfft(np.asarray(x[-SCOPE_N:], dtype=np.float64) * self._win))
            mag /= (SCOPE_N / 4.0)
            freqs = np.fft.rfftfreq(SCOPE_N, 1.0 / sr)
            centers = np.geomspace(80.0, min(10000.0, sr * 0.45), self.BARS)
            db = np.interp(centers, freqs, 20.0 * np.log10(mag + 1e-9))
            pos = np.clip((db + 80.0) / 70.0, 0.0, 1.0)
            self._vals = np.maximum(pos, self._vals * 0.85)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T("input")))
        p.drawRoundedRect(r, 8, 8)
        acc = UI.accent_color()
        grad = QLinearGradient(0, r.bottom(), 0, r.top())
        grad.setColorAt(0.0, blend(acc, "#000000", 0.5))
        grad.setColorAt(0.65, acc)
        grad.setColorAt(1.0, blend(acc, "#ffffff", 0.4))
        p.setBrush(QBrush(grad))
        n = len(self._vals)
        w, h = r.width() - 8, r.height() - 8
        bw = w / n
        for i, v in enumerate(self._vals):
            bh = float(v) * h
            if bh >= 1.0:
                p.drawRect(QRectF(4 + i * bw + 1, r.bottom() - 4 - bh, max(1.0, bw - 2), bh))


# ===========================================================================
#  ТЕМЫ И ГРАДИЕНТЫ
# ===========================================================================

THEMES = {
    "dark":  dict(bg="#0f1017", side="#13141d", card="#14151f", card_hi="#1b1d2c",
                  border="#262a3b", text="#e7e9f1", muted="#8b90a5", input="#0d0e14"),
    "light": dict(bg="#f2f3f8", side="#ffffff", card="#ffffff", card_hi="#eef0ff",
                  border="#d9dce8", text="#1b1d2a", muted="#6b7085", input="#ffffff"),
}

ACCENTS = {
    "Индиго": "#6366f1",
    "Изумруд": "#10b981",
    "Янтарь": "#f59e0b",
    "Розовый": "#ec4899",
    "Бирюза": "#06b6d4",
    "Красный": "#ef4444",
}

# 7 конкретных кастомных градиентных пресетов
GRADIENTS = {
    "Асфальт и Пламя":     ("#2B2D42", "#EF233C"),
    "Винный Лимон":        ("#5A1020", "#FFF44F"),
    "Морская Пена":        ("#00B4D8", "#FFFFFF"),
    "Киберпанк":           ("#39FF14", "#C2185B"),
    "Затмение":            ("#FFC400", "#2F3640"),
    "Полуночный мандарин": ("#FB8500", "#023047"),
    "Кислотный Уголь":     ("#C6FF00", "#1C1C1E"),
}


CUSTOM_SOLID = "★ Свой цвет"
CUSTOM_GRAD = "★ Свой градиент"


class UI:
    theme = "dark"
    custom_solid = "#a855f7"
    custom_g1 = "#ff5f6d"
    custom_g2 = "#ffc371"
    accent_name = "Индиго"
    accent_c1 = "#6366f1"
    accent_c2 = "#6366f1"
    is_gradient = False
    anim = True

    @classmethod
    def accent_color(cls) -> QColor:
        if cls.is_gradient:
            return blend(cls.accent_c1, cls.accent_c2, 0.5)
        return QColor(cls.accent_c1)

    @classmethod
    def accent_qss(cls) -> str:
        if cls.is_gradient:
            return f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {cls.accent_c1}, stop:1 {cls.accent_c2})"
        return cls.accent_c1

    @classmethod
    def on_accent(cls) -> str:
        """Цвет текста на акцентной заливке: тёмный на светлом акценте, белый на тёмном."""
        return "#14151f" if _lum(cls.accent_color()) > 150 else "#ffffff"

    @classmethod
    def set_accent(cls, name):
        if name in ACCENTS:
            cls.accent_name = name
            cls.accent_c1 = ACCENTS[name]
            cls.accent_c2 = ACCENTS[name]
            cls.is_gradient = False

    @classmethod
    def set_custom_solid(cls, col):
        cls.custom_solid = col
        cls.accent_name = CUSTOM_SOLID
        cls.accent_c1 = cls.accent_c2 = col
        cls.is_gradient = False

    @classmethod
    def set_custom_gradient(cls, c1, c2):
        cls.custom_g1, cls.custom_g2 = c1, c2
        cls.accent_name = CUSTOM_GRAD
        cls.accent_c1, cls.accent_c2 = c1, c2
        cls.is_gradient = True

    @classmethod
    def set_gradient(cls, name):
        if name in GRADIENTS:
            cls.accent_name = name
            cls.accent_c1, cls.accent_c2 = GRADIENTS[name]
            cls.is_gradient = True

    @classmethod
    def load(cls, s):
        th = s.value("ui_theme", "dark", type=str)
        cls.theme = th if th in THEMES else "dark"
        g_name = s.value("ui_gradient", "", type=str)
        ac_name = s.value("ui_accent", "Индиго", type=str)
        if g_name in GRADIENTS:
            cls.set_gradient(g_name)
        elif ac_name in ACCENTS:
            cls.set_accent(ac_name)
        else:
            cls.set_accent("Индиго")
        cls.anim = s.value("ui_anim", True, type=bool)
        for attr, key in (("custom_solid", "ui_custom_solid"), ("custom_g1", "ui_custom_g1"),
                          ("custom_g2", "ui_custom_g2")):
            val = s.value(key, getattr(cls, attr), type=str)
            if QColor(val).isValid():
                setattr(cls, attr, QColor(val).name())
        mode = s.value("ui_custom_mode", "", type=str)
        if mode == "solid":
            cls.set_custom_solid(cls.custom_solid)
        elif mode == "grad":
            cls.set_custom_gradient(cls.custom_g1, cls.custom_g2)


def T(key: str) -> str:
    return THEMES[UI.theme][key]


def blend(c1, c2, t: float) -> QColor:
    a, b = QColor(c1), QColor(c2)
    t = min(1.0, max(0.0, float(t)))
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t),
                  int(a.alpha() + (b.alpha() - a.alpha()) * t))


def with_alpha(c, alpha: int) -> QColor:
    q = QColor(c)
    q.setAlpha(max(0, min(255, int(alpha))))
    return q


class Smooth:
    def _sm_init(self, speed=0.22):
        self._sv, self._st, self._sp = {}, {}, speed
        self._stimer = QTimer(self)
        self._stimer.setInterval(16)
        self._stimer.timeout.connect(self._sm_step)

    def sm(self, key, default=0.0):
        return self._sv.get(key, default)

    def sm_set(self, key, target, instant=False):
        self._st[key] = target
        if instant or not UI.anim:
            self._sv[key] = target
            self.update()
            return
        if not self._stimer.isActive():
            self._stimer.start()

    def sm_sync(self, key, target):
        if self._st.get(key) != target:
            self.sm_set(key, target, instant=key not in self._st)

    def _sm_step(self):
        moving = False
        for k, t in self._st.items():
            v = self._sv.get(k, 0.0)
            d = t - v
            if abs(d) < 0.003:
                self._sv[k] = t
            else:
                self._sv[k] = v + d * self._sp
                moving = True
        self.update()
        if not moving:
            self._stimer.stop()


class ResetSlider(Smooth, QSlider):
    """Ползунок со своей отрисовкой: градиентная дорожка и круглая ручка.
    Двойной клик — сброс к значению по умолчанию."""
    R = 8.0

    def __init__(self, default=0):
        super().__init__(Qt.Orientation.Horizontal)
        self._sm_init(0.25)
        self.default_value = default
        self._drag = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(26)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self):
        return QSize(140, 26)

    def minimumSizeHint(self):
        return QSize(60, 26)

    def _set_from(self, x: float):
        span = max(1.0, float(self.width()) - 2 * self.R)
        frac = min(1.0, max(0.0, (x - self.R) / span))
        self.setValue(self.minimum() + int(round(frac * (self.maximum() - self.minimum()))))

    def enterEvent(self, e):
        self.sm_set("hov", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.sm_set("hov", 0.0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self.setSliderDown(True)
            self.sm_set("press", 1.0)
            self.setFocus()
            self._set_from(e.position().x())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._set_from(e.position().x())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.setSliderDown(False)
            self.sm_set("press", 0.0)

    def mouseDoubleClickEvent(self, event):
        self.setValue(self.default_value)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        W, H, R = float(self.width()), float(self.height()), self.R
        cy = H / 2.0
        lo, hi = self.minimum(), self.maximum()
        frac = 0.0 if hi == lo else (self.value() - lo) / float(hi - lo)
        hov, prs = self.sm("hov"), self.sm("press")
        c1, c2 = _vis(UI.accent_c1, 55), _vis(UI.accent_c2, 55)
        x0, x1 = R, W - R
        xh = x0 + (x1 - x0) * frac

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T("border")))
        p.drawRoundedRect(QRectF(x0 - 2, cy - 3, (x1 - x0) + 4, 6), 3, 3)
        if frac > 0.0:
            g = QLinearGradient(x0, 0.0, max(x1, x0 + 1.0), 0.0)
            g.setColorAt(0.0, c1)
            g.setColorAt(1.0, c2)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRectF(x0 - 2, cy - 3, max(6.0, xh - x0 + 2), 6), 3, 3)

        edge = blend(c1, c2, frac)
        r = R - 1.0 + 1.5 * hov - 1.0 * prs
        glow = 0.35 * hov + 0.35 * prs + (0.25 if self.hasFocus() else 0.0)
        if glow > 0.01:
            p.setBrush(with_alpha(edge, int(70 * min(1.0, glow))))
            p.drawEllipse(QPointF(xh, cy), r + 4.5, r + 4.5)
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(edge, 2.6))
        p.drawEllipse(QPointF(xh, cy), r - 1.3, r - 1.3)


def _pen(col, width=1.6):
    return QPen(col, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def draw_nav_icon(p, name, r, col):
    x, y = r.left(), r.top()
    P = QPointF
    p.setPen(_pen(col, 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    if name == "presets":
        for i in range(2):
            for j in range(2):
                p.drawRoundedRect(QRectF(x + 1 + i * 9, y + 1 + j * 9, 7, 7), 2, 2)
    elif name == "effects":
        for i, ky in enumerate((6, 12, 8)):
            xx = x + 3 + i * 6
            p.drawLine(P(xx, y + 1), P(xx, y + 17))
            p.setBrush(col)
            p.drawEllipse(P(xx, y + ky), 2.2, 2.2)
            p.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "sound":
        for i, h in enumerate((6, 12, 16, 10, 5)):
            xx = x + 2 + i * 3.6
            p.drawLine(P(xx, y + 9 - h / 2), P(xx, y + 9 + h / 2))
    elif name == "device":
        p.drawRoundedRect(QRectF(x + 6, y + 1, 6, 10), 3, 3)
        p.drawArc(QRectF(x + 3, y + 5, 12, 10), 180 * 16, 180 * 16)
        p.drawLine(P(x + 9, y + 15), P(x + 9, y + 17.5))
        p.drawLine(P(x + 6, y + 17.5), P(x + 12, y + 17.5))
    elif name == "ui":
        box = QRectF(x + 2, y + 2, 14, 14)
        p.drawEllipse(box)
        p.setBrush(col)
        p.drawPie(box, 90 * 16, 180 * 16)
    elif name == "keys":
        p.drawRoundedRect(QRectF(x + 1, y + 2.5, 16, 13), 3, 3)
        for yy in (6.2, 9.4):
            for xx in (5, 9, 13):
                p.drawPoint(P(x + xx, y + yy))
        p.drawLine(P(x + 6, y + 12.6), P(x + 12, y + 12.6))
    elif name == "io":
        p.drawLine(P(x + 5, y + 16), P(x + 5, y + 3))
        p.drawLine(P(x + 2, y + 6), P(x + 5, y + 3))
        p.drawLine(P(x + 8, y + 6), P(x + 5, y + 3))
        p.drawLine(P(x + 13, y + 2), P(x + 13, y + 15))
        p.drawLine(P(x + 10, y + 12), P(x + 13, y + 15))
        p.drawLine(P(x + 16, y + 12), P(x + 13, y + 15))
    else:
        p.drawEllipse(QRectF(x + 1, y + 1, 16, 16))
        p.drawLine(P(x + 9, y + 8), P(x + 9, y + 13))
        p.drawPoint(P(x + 9, y + 5.3))


def wave_points(p: dict, n: int = 72) -> np.ndarray:
    p = sanitize_params(p)
    x = np.linspace(0.0, 1.0, n)
    cyc = float(np.clip(2.6 * 2.0 ** (p["pitch"] / 12.0), 1.2, 7.0))
    ph = 2 * np.pi * cyc * x + p["vibrato"] * 1.6 * np.sin(2 * np.pi * 3 * x)
    y = np.sin(ph)
    if abs(p["formant"]) > 1e-3:
        y = y + 0.9 * abs(p["formant"]) * np.sin(3 * ph)
    if p["harmony"] > 0:
        y = y + 0.6 * p["harmony"] * np.sin(ph * 2.0 ** (p["harm_int"] / 12.0))
    if p["chorus"] > 0:
        y = y + 0.5 * p["chorus"] * np.sin(ph * 1.04 + 0.8)
    if p["tremolo"] > 0:
        y = y * (1 - 0.7 * p["tremolo"] * (0.5 - 0.5 * np.cos(2 * np.pi * 4 * x)))
    if p["ring"] > 0:
        y = (1 - p["ring"]) * y + p["ring"] * y * np.sin(2 * np.pi * 9 * x)
    if p["drive"] > 0:
        y = np.tanh((1 + 6 * p["drive"]) * y)
    if p["crush"] > 0:
        q = 12 - 9 * p["crush"]
        y = np.round(y / (np.max(np.abs(y)) + 1e-9) * q) / q
    if p["whisper"] > 0:
        w = p["whisper"]
        y = (1 - w) * y + w * np.random.default_rng(3).uniform(-1, 1, n)
    if p["scream"] > 0:
        y = np.tanh((1 + 4 * p["scream"]) * y) + 0.35 * p["scream"] * np.sin(0.5 * ph)
    y = y * np.sin(np.pi * x) ** 0.6
    if p["echo"] > 0:
        sh = max(1, int(n * 0.16))
        y2 = np.zeros(n)
        y2[sh:] = y[:-sh]
        y = y + 0.6 * p["echo"] * y2
    if p["reverb"] > 0:
        tail = (x > 0.55) * np.exp(-4 * np.clip(x - 0.55, 0, None)) * np.sin(ph * 0.7)
        y = y + 0.5 * p["reverb"] * tail
    m = float(np.max(np.abs(y)))
    return y / m if m > 1e-6 else y


def preset_icon(params: dict):
    pts = wave_points(params)
    def draw(p, r, col):
        n = len(pts)
        p.setPen(with_alpha(col, 70))
        p.drawLine(QPointF(r.left(), r.center().y()), QPointF(r.right(), r.center().y()))
        path = QPainterPath()
        for i, v in enumerate(pts):
            xx = r.left() + r.width() * i / (n - 1)
            yy = r.center().y() - float(v) * r.height() / 2
            if i == 0:
                path.moveTo(xx, yy)
            else:
                path.lineTo(xx, yy)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen(col, 1.8))
        p.drawPath(path)
    return draw


def sound_icon(bars):
    def draw(p, r, col):
        n = len(bars)
        p.setPen(_pen(col, 2.4))
        for i, v in enumerate(bars):
            xx = r.left() + 2 + (r.width() - 4) * i / max(1, n - 1)
            h = max(2.0, float(v) * r.height())
            p.drawLine(QPointF(xx, r.center().y() - h / 2), QPointF(xx, r.center().y() + h / 2))
    return draw


AL_C = Qt.AlignmentFlag.AlignCenter
AL_VL = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft


class NavButton(Smooth, QPushButton):
    def __init__(self, text, icon):
        super().__init__()
        self._sm_init(0.25)
        self._label, self._icon = text, icon
        self.setCheckable(True)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def enterEvent(self, e):
        self.sm_set("hov", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.sm_set("hov", 0.0)
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.sm_sync("sel", 1.0 if self.isChecked() else 0.0)
        sel, hov = self.sm("sel"), self.sm("hov")
        acc = UI.accent_color()
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setBrush(blend(T("bg"), acc, 0.14 * sel + 0.05 * hov))
        p.setPen(QPen(blend(T("border"), acc, max(sel, hov * 0.55)), 1.2))
        p.drawRoundedRect(r, 9, 9)
        if sel > 0.02:
            h = 18.0 * sel
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(acc)
            p.drawRoundedRect(QRectF(r.left() + 3, r.center().y() - h / 2, 3, h), 1.5, 1.5)
        draw_nav_icon(p, self._icon, QRectF(r.left() + 14, r.center().y() - 9, 18, 18),
                      blend(blend(T("muted"), T("text"), hov * 0.6), acc, sel))
        f = p.font()
        f.setBold(sel > 0.5)
        p.setFont(f)
        p.setPen(blend(T("muted"), T("text"), max(sel, hov * 0.75)))
        p.drawText(QRectF(r.left() + 42, r.top(), r.width() - 48, r.height()), AL_VL, self._label)


class Card(Smooth, QPushButton):
    W, H = 224, 108
    def __init__(self, title, icon_fn, badge=""):
        super().__init__()
        self._sm_init(0.2)
        self.title, self.icon_fn, self.badge = title, icon_fn, badge
        self.on_context = None
        self.setCheckable(True)
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(title)

    def enterEvent(self, e):
        self.sm_set("hov", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.sm_set("hov", 0.0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self.sm_set("press", 1.0)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self.sm_set("press", 0.0)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        if self.on_context:
            self.on_context(self, e.globalPos())

    @staticmethod
    def _wrap_title(fm, text, width):
        """Название в одну или две строки (вторая обрезается посередине) — длинные имена читаются."""
        if fm.horizontalAdvance(text) <= width:
            return [text]
        k = len(text)
        while k > 1 and fm.horizontalAdvance(text[:k]) > width:
            k -= 1
        sp = text.rfind(" ", 0, k + 1)
        if sp > k // 2:
            k = sp
        first, rest = text[:k].rstrip(), text[k:].lstrip()
        return [first, fm.elidedText(rest, Qt.TextElideMode.ElideMiddle, width)]

    def flash(self):
        self.sm_set("flash", 1.0, instant=True)
        self.sm_set("flash", 0.0)

    def appear(self, delay_ms):
        if not UI.anim:
            self.sm_set("appear", 1.0, instant=True)
            return
        self.sm_set("appear", 0.0, instant=True)
        def go():
            try:
                self.sm_set("appear", 1.0)
            except RuntimeError:
                pass
        QTimer.singleShot(int(delay_ms), go)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.sm_sync("sel", 1.0 if self.isChecked() else 0.0)
        ap = min(1.0, max(0.0, self.sm("appear", 1.0)))
        sel, hov, prs, fl = self.sm("sel"), self.sm("hov"), self.sm("press"), self.sm("flash")
        p.setOpacity(ap)
        acc = UI.accent_color()
        lift = -2.0 * hov + 1.5 * prs + (1.0 - ap) * 12.0
        r = QRectF(4, 4 + lift, self.width() - 8, self.height() - 8)
        glow = max(sel, hov * 0.5, fl)
        if glow > 0.02:
            p.setPen(Qt.PenStyle.NoPen)
            for k in (3, 2, 1):
                p.setBrush(with_alpha(acc, 16 * glow * (4 - k)))
                p.drawRoundedRect(r.adjusted(-k * 1.2, -k * 1.2, k * 1.2, k * 1.2), 12 + k, 12 + k)
        fill = blend(T("card"), T("card_hi"), max(hov * 0.7, sel * 0.5))
        fill = blend(fill, acc, 0.10 * sel + 0.35 * fl)
        p.setBrush(fill)
        p.setPen(QPen(blend(T("border"), acc, max(sel, hov * 0.55, fl)), 1.0 + 1.3 * sel))
        p.drawRoundedRect(r, 12, 12)
        icol = blend(blend(T("muted"), T("text"), hov * 0.4), acc, sel * 0.9 + fl)
        self.icon_fn(p, QRectF(r.left() + (r.width() - 100) / 2, r.top() + 14, 100, 40), icol)
        f = p.font()
        f.setBold(True)
        f.setPointSizeF(10.0)
        p.setFont(f)
        p.setPen(QColor(T("text")))
        w = int(r.width() - 20)
        lines = self._wrap_title(p.fontMetrics(), self.title, w)
        if len(lines) > 1:
            f.setPointSizeF(9.0)
            p.setFont(f)
            lines = self._wrap_title(p.fontMetrics(), self.title, w)
        p.setPen(QColor(T("text")))
        p.drawText(QRectF(r.left() + 10, r.bottom() - 40, r.width() - 20, 34), AL_C, "\n".join(lines))
        if self.badge:
            f.setPointSizeF(8.0)
            p.setFont(f)
            p.setPen(_vis(blend(T("muted"), acc, 0.7), 60))
            bt = p.fontMetrics().elidedText(self.badge, Qt.TextElideMode.ElideRight, int(r.width() - 20))
            p.drawText(QRectF(r.left() + 10, r.top() + 6, r.width() - 20, 16), AL_VL, bt)

class Switch(Smooth, QCheckBox):
    TW, TH = 40, 22
    def __init__(self, text=""):
        super().__init__(text)
        self._sm_init(0.3)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def hitButton(self, pos):
        return True

    def sizeHint(self):
        return QSize(self.TW + 14 + self.fontMetrics().horizontalAdvance(self.text()) + 6, 30)

    def minimumSizeHint(self):
        return self.sizeHint()

    def enterEvent(self, e):
        self.sm_set("hov", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.sm_set("hov", 0.0)
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.sm_sync("on", 1.0 if self.isChecked() else 0.0)
        on, hov = self.sm("on"), self.sm("hov")
        enabled = self.isEnabled()
        p.setOpacity(1.0 if enabled else 0.45)
        h = self.height()
        tr = QRectF(1, (h - self.TH) / 2, self.TW, self.TH)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(blend(blend(T("border"), T("muted"), hov * 0.25), UI.accent_color(), on))
        p.drawRoundedRect(tr, self.TH / 2, self.TH / 2)
        d = self.TH - 6
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(tr.left() + 3 + (self.TW - self.TH) * on, tr.top() + 3, d, d))
        p.setPen(QColor(T("text")))
        p.drawText(QRectF(self.TW + 14, 0, max(0, self.width() - self.TW - 14), h), AL_VL, self.text())


class Swatch(Smooth, QPushButton):
    """Плашка выбора цвета / градиента."""
    def __init__(self, color_data):
        super().__init__()
        self._sm_init(0.3)
        self.color_data = color_data
        self.setCheckable(True)
        self.setFixedSize(36, 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def enterEvent(self, e):
        self.sm_set("hov", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.sm_set("hov", 0.0)
        super().leaveEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.sm_sync("sel", 1.0 if self.isChecked() else 0.0)
        sel, hov = self.sm("sel"), self.sm("hov")
        c = QPointF(self.width() / 2, self.height() / 2)
        rad = 11.0 + 1.5 * hov
        if sel > 0.02:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(with_alpha(T("text"), 255 * sel), 2.0))
            p.drawEllipse(c, 15.0, 15.0)
        p.setPen(Qt.PenStyle.NoPen)
        if isinstance(self.color_data, (tuple, list)):
            grad = QLinearGradient(c.x() - rad, c.y() - rad, c.x() + rad, c.y() + rad)
            grad.setColorAt(0.0, QColor(self.color_data[0]))
            grad.setColorAt(1.0, QColor(self.color_data[1]))
            p.setBrush(QBrush(grad))
        else:
            p.setBrush(QColor(self.color_data))
        p.drawEllipse(c, rad, rad)


class PowerButton(Smooth, QPushButton):
    def __init__(self):
        super().__init__()
        self._sm_init(0.2)
        self._label = "Включить"
        self.setCheckable(True)
        self.setFixedSize(176, 58)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_label(self, t):
        self._label = t
        self.update()

    def enterEvent(self, e):
        self.sm_set("hov", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.sm_set("hov", 0.0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self.sm_set("press", 1.0)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self.sm_set("press", 0.0)
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.sm_sync("on", 1.0 if self.isChecked() else 0.0)
        on, hov, prs = self.sm("on"), self.sm("hov"), self.sm("press")
        r = QRectF(8, 8 + prs, self.width() - 16, self.height() - 16)
        base = blend(UI.accent_color(), "#22c55e", on)
        fg = QColor("#14151f") if _lum(base) > 160 else QColor("#ffffff")
        if on > 0.05 and UI.anim:
            t = (time.time() % 1.6) / 1.6
            g = 2.0 + 6.0 * t
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(with_alpha(base, 110 * (1 - t) * on), 2.0))
            p.drawRoundedRect(r.adjusted(-g, -g, g, g), 12 + g, 12 + g)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(blend(base, "#ffffff", 0.14 * hov))
        p.drawRoundedRect(r, 12, 12)
        p.setBrush(fg)
        cx, cy = r.left() + 26, r.center().y()
        if 1.0 - on > 0.01:
            p.setOpacity(1.0 - on)
            p.drawPolygon(QPolygonF([QPointF(cx - 5, cy - 7), QPointF(cx - 5, cy + 7), QPointF(cx + 7, cy)]))
        if on > 0.01:
            p.setOpacity(on)
            p.drawRoundedRect(QRectF(cx - 6, cy - 6, 12, 12), 2, 2)
        p.setOpacity(1.0)
        f = p.font()
        f.setBold(True)
        f.setPointSizeF(10.5)
        p.setFont(f)
        p.setPen(fg)
        p.drawText(QRectF(r.left() + 44, r.top(), r.width() - 50, r.height()), AL_VL, self._label)


def _lum(c) -> float:
    c = QColor(c)
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()


def _vis(c, min_diff: float = 70.0) -> QColor:
    """Подтягивает цвет так, чтобы он читался на фоне карточки
    (тёмные концы градиентов вроде «Кислотный Уголь» иначе сливаются с фоном)."""
    c, bg = QColor(c), QColor(T("card"))
    if abs(_lum(c) - _lum(bg)) >= min_diff:
        return c
    target = QColor("#ffffff") if _lum(bg) < 128 else QColor("#000000")
    for i in range(1, 11):
        cand = blend(c, target, i / 10.0)
        if abs(_lum(cand) - _lum(bg)) >= min_diff:
            return cand
    return blend(c, target, 1.0)


AL_VR = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight


class PaintLabel(QLabel):
    """Подпись, которая сама рисуется цветом темы и обрезает длинный текст «…»."""
    def __init__(self, text="", role="text", bold=False, pt=None, align=AL_VL):
        super().__init__(text)
        self._role, self._bold, self._pt, self._al = role, bold, pt, align
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        f = p.font()
        f.setBold(self._bold)
        if self._pt:
            f.setPointSizeF(self._pt)
        p.setFont(f)
        p.setPen(QColor(T(self._role)))
        txt = p.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.width())
        p.drawText(QRectF(self.rect()), self._al, txt)


class TransportButton(Smooth, QPushButton):
    """Предыдущий / следующий звук."""
    def __init__(self, kind):
        super().__init__()
        self._sm_init(0.25)
        self.kind = kind
        self.setFixedSize(38, 38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("Предыдущий звук" if kind == "prev" else "Следующий звук")

    def enterEvent(self, e):
        self.sm_set("hov", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.sm_set("hov", 0.0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self.sm_set("press", 1.0)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self.sm_set("press", 0.0)
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hov, prs = self.sm("hov"), self.sm("press")
        acc = UI.accent_color()
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        p.setPen(Qt.PenStyle.NoPen)
        if hov > 0.01 or prs > 0.01:
            p.setBrush(with_alpha(acc, int(36 * hov + 34 * prs)))
            p.drawEllipse(r)
        p.setBrush(blend(T("muted"), _vis(acc, 60), hov))
        c, s = r.center(), 1.0 - 0.10 * prs
        x0, x1, h = c.x() - 7 * s, c.x() + 7 * s, 6.5 * s
        if self.kind == "prev":
            p.drawRoundedRect(QRectF(x0, c.y() - h, 2 * s, 2 * h), 1, 1)
            p.drawPolygon(QPolygonF([QPointF(x0 + 3.5 * s, c.y()), QPointF(x1, c.y() - h), QPointF(x1, c.y() + h)]))
        else:
            p.drawRoundedRect(QRectF(x1 - 2 * s, c.y() - h, 2 * s, 2 * h), 1, 1)
            p.drawPolygon(QPolygonF([QPointF(x1 - 3.5 * s, c.y()), QPointF(x0, c.y() - h), QPointF(x0, c.y() + h)]))


class PlayButton(Smooth, QPushButton):
    """Круглая кнопка play/pause с акцентным градиентом, свечением и плавной сменой иконки."""
    def __init__(self):
        super().__init__()
        self._sm_init(0.2)
        self.setFixedSize(58, 58)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_playing(self, on: bool):
        self.sm_sync("pl", 1.0 if on else 0.0)

    def enterEvent(self, e):
        self.sm_set("hov", 1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.sm_set("hov", 0.0)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self.sm_set("press", 1.0)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self.sm_set("press", 0.0)
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pl, hov, prs = self.sm("pl"), self.sm("hov"), self.sm("press")
        c1, c2 = QColor(UI.accent_c1), QColor(UI.accent_c2)
        if not UI.is_gradient:
            c1 = blend(c1, "#ffffff", 0.22)          # лёгкий объём у обычных акцентов
        c1, c2 = _vis(c1, 45), _vis(c2, 45)
        base = UI.accent_color()
        cx, cy = self.width() / 2.0, self.height() / 2.0
        rad = 21.0 + 0.8 * hov - 1.2 * prs
        c = QPointF(cx, cy)
        if pl > 0.05 and UI.anim:
            t = (time.time() % 1.8) / 1.8
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(with_alpha(base, int(120 * (1 - t) * pl)), 2.0))
            p.drawEllipse(c, rad + 2 + 6 * t, rad + 2 + 6 * t)
        p.setPen(Qt.PenStyle.NoPen)
        a = 8 + 10 * hov + 14 * pl
        for k in (3, 2, 1):
            p.setBrush(with_alpha(base, int(a * (4 - k) * 0.6)))
            p.drawEllipse(c, rad + k * 2.0, rad + k * 2.0)
        g = QLinearGradient(cx - rad, cy - rad, cx + rad, cy + rad)
        g.setColorAt(0.0, blend(c1, "#ffffff", 0.12 * hov))
        g.setColorAt(1.0, blend(c2, "#ffffff", 0.12 * hov))
        p.setBrush(QBrush(g))
        p.drawEllipse(c, rad, rad)

        mid = blend(c1, c2, 0.5)
        ic = QColor("#14151f") if _lum(mid) > 160 else QColor("#ffffff")
        p.setBrush(ic)
        if 1.0 - pl > 0.01:
            p.setOpacity(1.0 - pl)
            p.setPen(QPen(ic, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPolygon(QPolygonF([QPointF(cx - 3, cy - 7), QPointF(cx - 3, cy + 7), QPointF(cx + 7.5, cy)]))
        if pl > 0.01:
            p.setOpacity(pl)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(cx - 6.5, cy - 7, 4.5, 14), 1.6, 1.6)
            p.drawRoundedRect(QRectF(cx + 2.0, cy - 7, 4.5, 14), 1.6, 1.6)
        p.setOpacity(1.0)


class SeekBar(Smooth, QWidget):
    """Перемотка в виде волны + время по краям (рисуется внутри виджета, поэтому
    не наезжает на волну и не обрезается). Клик/перетаскивание — перемотка."""
    seeked = pyqtSignal(float)
    BAR_W, GAP, PAD = 3.0, 2.0, 10.0

    def __init__(self):
        super().__init__()
        self._sm_init(0.25)
        self._bars = None
        self._total_s = 0.0
        self._hover_x = None
        self._active = False
        self._cache = None
        self._cur_txt, self._dur_txt = "0:00", "0:00"
        self.dragging = False
        self.setFixedHeight(40)
        self.setMinimumWidth(200)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # --- состояние
    def set_bars(self, bars):
        self._bars = None if bars is None else np.asarray(bars, dtype=np.float64)
        self._cache = None
        self.update()

    def set_total(self, sec: float):
        self._total_s = float(sec)

    def set_times(self, cur: str, dur: str):
        if cur != self._cur_txt or dur != self._dur_txt:
            self._cur_txt, self._dur_txt = cur, dur
            self.update()

    def set_active(self, on: bool):
        self._active = bool(on)

    def fraction(self) -> float:
        return min(1.0, max(0.0, self.sm("pos")))

    def set_fraction(self, f: float):
        if self.dragging:
            return
        f = min(1.0, max(0.0, f))
        jump = abs(f - self.sm("pos")) > 0.12
        self.sm_set("pos", f, instant=jump or not self._active)

    # --- геометрия: слева и справа резервируем место под время
    def _time_font(self):
        f = QFont(self.font())
        f.setPointSizeF(9.0)
        f.setBold(False)
        return f

    def _gutter(self) -> float:
        fm = QFontMetrics(self._time_font())
        return float(fm.horizontalAdvance("0:00:00" if self._total_s >= 3600 else "00:00")) + self.PAD + 2.0

    def _frac_at(self, x: float) -> float:
        gw = self._gutter()
        rw = max(20.0, float(self.width()) - 2 * gw)
        return min(1.0, max(0.0, (x - gw) / rw))

    # --- мышь
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self._hover_x = e.position().x()
            self.sm_set("pos", self._frac_at(self._hover_x), instant=True)

    def mouseMoveEvent(self, e):
        self._hover_x = e.position().x()
        if self.dragging:
            self.sm_set("pos", self._frac_at(self._hover_x), instant=True)
        self.update()

    def mouseReleaseEvent(self, e):
        if self.dragging:
            self.dragging = False
            f = self._frac_at(e.position().x())
            self.sm_set("pos", f, instant=True)
            self.seeked.emit(f)

    def leaveEvent(self, e):
        self._hover_x = None
        self.update()
        super().leaveEvent(e)

    # --- рисование
    def _path(self, w, h):
        n = max(8, int((w + self.GAP) / (self.BAR_W + self.GAP)))
        key = (n, int(w), int(h), id(self._bars))
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        step = w / n
        bw = max(1.5, step - self.GAP)
        amp = None
        if self._bars is not None and len(self._bars) > 1:
            amp = np.interp(np.linspace(0, len(self._bars) - 1, n), np.arange(len(self._bars)), self._bars)
        mid, maxh = h / 2.0, h - 8.0
        path = QPainterPath()
        for i in range(n):
            bh = 3.0 if amp is None else 3.0 + (maxh - 3.0) * float(amp[i]) ** 0.8
            path.addRoundedRect(QRectF(i * step + (step - bw) / 2.0, mid - bh / 2.0, bw, bh), bw / 2.0, bw / 2.0)
        self._cache = (key, path)
        return path

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        W, h = float(self.width()), float(self.height())
        gw = self._gutter()
        w = max(20.0, W - 2 * gw)

        # время по краям
        p.setFont(self._time_font())
        p.setPen(QColor(T("muted")))
        p.drawText(QRectF(0.0, 0.0, gw - self.PAD, h), AL_VR, self._cur_txt)
        p.drawText(QRectF(W - gw + self.PAD, 0.0, gw - self.PAD, h), AL_VL, self._dur_txt)

        # волна
        p.translate(gw, 0.0)
        hx = None if self._hover_x is None else min(max(self._hover_x - gw, 0.0), w)
        self.sm_sync("knob", 1.0 if (hx is not None or self.dragging) else 0.0)
        pos, knob = self.fraction(), self.sm("knob")
        xp = w * pos
        c1, c2 = _vis(UI.accent_c1), _vis(UI.accent_c2)
        path = self._path(w, h)

        p.fillPath(path, QBrush(blend(T("border"), T("muted"), 0.35)))
        if hx is not None and hx > xp + 1:                              # превью перемотки
            p.save()
            p.setClipRect(QRectF(xp, 0, hx - xp, h))
            p.fillPath(path, QBrush(with_alpha(c1, 90)))
            p.restore()
        if xp > 0.5:
            g = QLinearGradient(0.0, 0.0, max(w, 1.0), 0.0)
            g.setColorAt(0.0, c1)
            g.setColorAt(1.0, c2)
            p.save()
            p.setClipRect(QRectF(0, 0, xp, h))
            p.fillPath(path, QBrush(g))
            p.restore()

        vis = max(knob, 1.0 if (self._active and pos > 0.0) else 0.0)
        if vis > 0.02:                                                  # линия + ползунок
            p.setOpacity(vis)
            p.setPen(QPen(c1, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(xp, 4.0), QPointF(xp, h - 4.0))
            if knob > 0.02:
                p.setBrush(c1)
                p.setPen(QPen(QColor("#ffffff"), 2.0))
                p.drawEllipse(QPointF(xp, h / 2.0), 6.0 * knob, 6.0 * knob)
            p.setOpacity(1.0)

        if hx is not None and self._total_s > 0:                        # время под курсором
            f = p.font()
            f.setPointSizeF(8.5)
            f.setBold(True)
            p.setFont(f)
            txt = fmt_time(hx / w * self._total_s)
            tw = p.fontMetrics().horizontalAdvance(txt) + 14
            x = min(max(hx - tw / 2.0, 0.0), w - tw)
            pill = QRectF(x, 0.0, tw, 17.0)
            p.setPen(QPen(QColor(T("border")), 1.0))
            p.setBrush(QColor(T("card_hi")))
            p.drawRoundedRect(pill, 8, 8)
            p.setPen(QColor(T("text")))
            p.drawText(pill, AL_C, txt)


class EqBars(QWidget):
    """Крошечный анимированный эквалайзер рядом с названием, пока что-то играет."""
    def __init__(self):
        super().__init__()
        self.setFixedSize(16, 14)
        self._on = False

    def set_active(self, on: bool):
        self._on = bool(on)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_vis(UI.accent_color(), 60) if self._on else QColor(T("muted")))
        t = time.time()
        for i in range(4):
            if self._on and UI.anim:
                hh = 3.0 + 9.0 * (0.5 + 0.5 * math.sin(t * (5.5 + i * 1.7) + i * 1.3))
            else:
                hh = 3.0 + (i % 2) * 2.0
            p.drawRoundedRect(QRectF(i * 4.0, 14.0 - hh, 2.4, hh), 1.2, 1.2)


class PlayerBar(Smooth, QWidget):
    """Карточка плеера саундборда — в стиле остальных карточек приложения."""
    def __init__(self):
        super().__init__()
        self._sm_init(0.2)
        self._has_track, self._last_total = False, 0.0
        self.setFixedHeight(88)

        self.prev_btn = TransportButton("prev")
        self.play_btn = PlayButton()
        self.next_btn = TransportButton("next")
        self.eq = EqBars()
        self.title = PaintLabel("Ничего не играет", "text", bold=True, pt=10.5)
        self.status = PaintLabel("", "muted", pt=9, align=AL_VR)
        self.status.setFixedWidth(80)
        self.seek = SeekBar()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 18, 10)
        lay.setSpacing(6)
        lay.addWidget(self.prev_btn)
        lay.addWidget(self.play_btn)
        lay.addWidget(self.next_btn)
        lay.addSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.eq)
        top.addWidget(self.title, 1)
        top.addWidget(self.status)
        bot = QHBoxLayout()
        bot.setSpacing(0)
        bot.addWidget(self.seek, 1)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addLayout(top)
        col.addLayout(bot)
        lay.addLayout(col, 1)

    @staticmethod
    def _set(label, text):
        if label.text() != text:
            label.setText(text)

    def set_track(self, title: str, bars):
        self._has_track = True
        self.title.setText(title)
        self.title.setToolTip(title)
        self.seek.set_bars(bars)

    def update_state(self, pos, total, sr, paused, busy):
        playing = bool(busy) and not paused
        self.play_btn.set_playing(playing)
        self.eq.set_active(playing)
        self.seek.set_active(bool(busy))
        self.sm_sync("act", 1.0 if playing else 0.0)
        if busy and total > 0 and sr > 0:
            dur = total / float(sr)
            self._last_total = dur
            self.seek.set_total(dur)
            self.seek.set_fraction(pos / float(total))
            cur = self.seek.fraction() * dur if self.seek.dragging else pos / float(sr)
            self.seek.set_times(fmt_time(cur), fmt_time(dur))
            self._set(self.status, "Играет" if playing else "Пауза")
        else:
            self.seek.set_total(self._last_total)
            self.seek.set_fraction(0.0)
            self.seek.set_times("0:00", fmt_time(self._last_total) if self._has_track else "0:00")
            self._set(self.status, "Готов" if self._has_track else "")
        if playing:
            self.eq.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        a = self.sm("act")
        acc = _vis(UI.accent_color(), 60)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setBrush(blend(T("card"), acc, 0.05 * a))
        p.setPen(QPen(blend(T("border"), acc, 0.45 * a), 1.0 + 0.3 * a))
        p.drawRoundedRect(r, 12, 12)


_MOD_KEYS = {Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr,
             Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock, Qt.Key.Key_ScrollLock,
             Qt.Key.Key_Super_L, Qt.Key.Key_Super_R, Qt.Key.Key_Hyper_L, Qt.Key.Key_Hyper_R}


class HotkeyEdit(QPushButton):
    """Поле для записи сочетания клавиш: клик, затем нажать нужные клавиши.
    Esc — отмена, Backspace/Delete — убрать назначение."""
    changed = pyqtSignal(str)

    def __init__(self, seq=""):
        super().__init__()
        self._seq = norm_key(seq)
        self._rec = False
        self.setCheckable(True)
        self.setFixedSize(190, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.toggled.connect(self._on_toggled)
        self._refresh()

    def sequence(self) -> str:
        return self._seq

    def set_sequence(self, seq):
        self._seq = norm_key(seq)
        self._refresh()

    def _refresh(self):
        self.setText("Нажмите клавиши…" if self._rec else (key_text(self._seq) or "Не назначено"))

    def _on_toggled(self, on):
        if on and not self._rec:
            self._rec = True
            self._refresh()
            self.setFocus()
        elif not on and self._rec:
            self._rec = False
            self._refresh()

    def stop_rec(self):
        self._rec = False
        self.blockSignals(True)
        self.setChecked(False)
        self.blockSignals(False)
        self._refresh()

    def focusOutEvent(self, e):
        if self._rec:
            self.stop_rec()
        super().focusOutEvent(e)

    def event(self, e):
        if self._rec:
            t = e.type()
            if t == QEvent.Type.ShortcutOverride:      # не давать окну перехватить клавишу
                e.accept()
                return True
            if t == QEvent.Type.KeyPress:
                self._capture(e)
                return True
            if t == QEvent.Type.KeyRelease:
                return True
        return super().event(e)

    def _capture(self, e):
        key = e.key()
        if key in _MOD_KEYS or key <= 0:
            return                                      # ждём «настоящую» клавишу
        mods = e.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        bare = (mods == Qt.KeyboardModifier.NoModifier)
        if bare and key == Qt.Key.Key_Escape:
            self.stop_rec()
            return
        if bare and key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self.stop_rec()
            self.set_sequence("")
            self.changed.emit("")
            return
        if key >= 0x80:                                 # кириллица -> соответствующая латинская клавиша
            lat = _RU2LAT.get(e.text().lower())
            if lat:
                key = ord(lat.upper())
        try:
            k = Qt.Key(key)
        except ValueError:
            return
        try:
            ks = QKeySequence(QKeyCombination(mods, k))
        except Exception:
            ks = QKeySequence(int(mods.value) | int(key))
        seq = norm_key(ks.toString(QKeySequence.SequenceFormat.PortableText))
        if not seq:
            return
        self.stop_rec()
        self.set_sequence(seq)
        self.changed.emit(seq)


class FadeStack(QStackedWidget):
    def __init__(self):
        super().__init__()
        self._anim = None
        self._fw = None

    def fade_to(self, i):
        if i == self.currentIndex():
            return
        self._finish()
        self.setCurrentIndex(i)
        if not UI.anim:
            return
        w = self.currentWidget()
        eff = QGraphicsOpacityEffect(w)
        eff.setOpacity(0.0)
        w.setGraphicsEffect(eff)
        an = QPropertyAnimation(eff, b"opacity", self)
        an.setDuration(280)
        an.setStartValue(0.0)
        an.setEndValue(1.0)
        an.setEasingCurve(QEasingCurve.Type.OutCubic)
        an.finished.connect(self._finish)
        self._anim, self._fw = an, w
        an.start()

    def _finish(self):
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        if self._fw is not None:
            try:
                self._fw.setGraphicsEffect(None)
            except RuntimeError:
                pass
            self._fw = None


class CardGrid(QWidget):
    GAP = 6
    def __init__(self):
        super().__init__()
        self.cards = []

    def set_cards(self, cards):
        for c in self.cards:
            c.setParent(None)
            c.deleteLater()
        self.cards = list(cards)
        for c in self.cards:
            c.setParent(self)
            c.show()
        self._relayout()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self):
        if not self.cards:
            self.setMinimumHeight(0)
            return
        cw, ch = Card.W, Card.H
        W = max(self.width(), cw)
        cols = max(1, (W + self.GAP) // (cw + self.GAP))
        cell = max(cw, (W - (cols - 1) * self.GAP) // cols)
        for i, c in enumerate(self.cards):
            if c.width() != cell:
                c.setFixedSize(cell, ch)
            c.move((i % cols) * (cell + self.GAP), (i // cols) * (ch + self.GAP))
        rows = (len(self.cards) + cols - 1) // cols
        self.setMinimumHeight(rows * (ch + self.GAP))


# ===========================================================================
#  СТИЛИ И ИХ ДИНАМИЧЕСКАЯ СБОРКА
# ===========================================================================

QSS = """
QMainWindow, QDialog, QMessageBox, QInputDialog { background: @bg; }
QWidget#root { background: @bg; }
QFrame#side { background: @side; border: none; border-right: 1px solid @border; }
QFrame#dock { background: @side; border: none; border-top: 1px solid @border; }
QFrame#section { background: @card; border: 1px solid @border; border-radius: 12px; }
QLabel { background: transparent; color: @text; }
QLabel#h1 { font-size: 13pt; font-weight: 700; }
QLabel#brand { font-size: 14pt; font-weight: 800; padding: 6px 4px 10px 4px; }
QLabel#muted, QLabel#hint, QLabel#value, QLabel#chip { color: @muted; }
QLabel#sectionTitle { font-weight: 700; font-size: 10.5pt; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QComboBox { background: @input; color: @text; border: 1px solid @border; border-radius: 8px;
            padding: 6px 10px; min-height: 20px; }
QComboBox:hover, QComboBox:focus { border-color: @accent_c1; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: @card; color: @text; border: 1px solid @border;
            selection-background-color: @accent_c1; selection-color: @on_accent; outline: 0; padding: 4px; }
QLineEdit { background: @input; color: @text; border: 1px solid @border; border-radius: 8px;
            padding: 7px 10px; selection-background-color: @accent_c1; }
QLineEdit:focus { border-color: @accent_c1; }
QPushButton { background: @card; color: @text; border: 1px solid @border; border-radius: 8px;
              padding: 7px 14px; }
QPushButton:hover { border-color: @accent_c1; }
QPushButton:pressed { background: @card_hi; }
QPushButton:disabled { color: @muted; }
QPushButton:checked { background: @accent; border-color: @accent_c1; color: @on_accent; }
QPushButton#primary { background: @accent; border-color: @accent_c1; color: @on_accent; font-weight: 600; }
QPushButton#primary:disabled { background: @border; border-color: @border; color: @muted; }
QPushButton#danger:checked { background: #ef4444; border-color: #ef4444; color: white; }
QSlider::groove:horizontal { height: 6px; background: @border; border-radius: 3px; }
QSlider::sub-page:horizontal { background: @accent; border-radius: 3px; }
QSlider::handle:horizontal { background: #ffffff; border: 2px solid @accent_c1; width: 12px;
                             height: 12px; margin: -6px 0; border-radius: 8px; }
QSlider::handle:horizontal:hover { background: @accent_c1; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: @border; border-radius: 4px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: @muted; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { background: @card; color: @text; border: 1px solid @border; padding: 5px; }
QMenu { background: @card; color: @text; border: 1px solid @border; border-radius: 8px;
        padding: 4px; }
QMenu::item { padding: 7px 18px; border-radius: 6px; }
QMenu::item:selected { background: @accent; color: @on_accent; }
QStatusBar { background: transparent; color: @muted; }
QStatusBar::item { border: none; }

"""


def build_qss() -> str:
    tokens = dict(THEMES[UI.theme])
    tokens["accent"] = UI.accent_qss()
    tokens["accent_c1"] = UI.accent_c1
    tokens["accent_c2"] = UI.accent_c2
    tokens["on_accent"] = UI.on_accent()
    q = QSS
    for k in sorted(tokens, key=len, reverse=True):
        q = q.replace("@" + k, tokens[k])
    return q


def apply_ui_theme(qapp):
    pal = QPalette()
    R = QPalette.ColorRole
    for role, key in ((R.Window, "bg"), (R.Base, "input"), (R.AlternateBase, "card"),
                      (R.Button, "card"), (R.ToolTipBase, "card")):
        pal.setColor(role, QColor(T(key)))
    for role in (R.WindowText, R.Text, R.ButtonText, R.ToolTipText):
        pal.setColor(role, QColor(T("text")))
    pal.setColor(R.PlaceholderText, QColor(T("muted")))
    pal.setColor(R.Highlight, UI.accent_color())
    pal.setColor(R.HighlightedText, QColor(UI.on_accent()))
    pal.setColor(R.Link, UI.accent_color())
    for role in (R.Text, R.ButtonText, R.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(T("muted")))
    qapp.setPalette(pal)
    qapp.setStyleSheet(build_qss())


# ===========================================================================
#  ГЛАВНОЕ ОКНО
# ===========================================================================

PAGES = (
    ("Пресеты", "presets", "Все доступные пресеты"),
    ("Эффекты", "effects", "Тонкая настройка эффектов"),
    ("Саундборд", "sound", "Саундборд и озвучка текста"),
    ("Устройства", "device", "Аудио-устройства"),
    ("Интерфейс", "ui", "Интерфейс и темы"),
    ("Горячие клавиши", "keys", "Горячие клавиши"),
    ("Экспорт/Импорт", "io", "Экспорт и импорт пресетов"),
    ("О программе", "info", "О программе"),
)


PAGE_HOTKEYS = 5


class MainWindow(QMainWindow):
    tts_done = pyqtSignal(object, str)
    tts_failed = pyqtSignal(str)
    tts_status = pyqtSignal(str)
    voice_downloaded = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.engine = AudioEngine()
        self.board = SoundBoard()
        self.engine.post_hook = self.board.mix
        self.settings = QSettings("VoiceChanger", "VoiceChanger")
        self.user_presets = load_user_presets()
        self.current_preset = None
        self.cards, self.sound_cards = {}, []
        self.sound_paths, self._bars = [], {}
        self.sliders, self.labels, self.nav, self.swatches, self.grad_swatches = {}, {}, [], {}, {}
        self._updating = False
        self._vm_active = False
        self._tick_n = 0
        self._health_warned = False
        self._cur_page = 0
        self._is_user_seeking = False
        self._tts_busy = False
        self._voice_dl_busy = False
        self._tts_proxy = ''
        self._awake = KeepAwake()
        self.hotkeys = dict(HOTKEY_DEFAULTS)
        self.sound_keys = {}
        self.hk_edits, self._shortcuts = {}, []
        self.tts_done.connect(self._on_tts_done)
        self.tts_failed.connect(self._on_tts_failed)
        self.tts_status.connect(lambda t: self.say_info.setText(t))
        self.voice_downloaded.connect(self._on_voice_downloaded)
        threading.Thread(target=piper_cmd, daemon=True).start()   # прогрев проверки Piper

        self.setWindowTitle(f"{APP_TITLE} {APP_VERSION}")
        self.setMinimumSize(1120, 700)
        self.resize(1240, 780)
        self._build_ui()
        self._refresh_devices()
        self._load_settings()

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(224)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(12, 14, 12, 12)
        sv.setSpacing(8)
        brand = QLabel(APP_TITLE.replace(" ", ""))
        brand.setObjectName("brand")
        sv.addWidget(brand)
        for i, (name, icon, _t) in enumerate(PAGES):
            b = NavButton(name, icon)
            b.clicked.connect(lambda _=False, i=i: self._goto(i))
            self.nav.append(b)
            sv.addWidget(b)
        sv.addStretch(1)
        ver = QLabel(f"v{APP_VERSION}")
        ver.setObjectName("muted")
        sv.addWidget(ver)
        h.addWidget(side)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(24, 20, 24, 12)
        cv.setSpacing(12)
        self.page_title = QLabel(PAGES[0][2])
        self.page_title.setObjectName("h1")
        cv.addWidget(self.page_title)
        self.stack = FadeStack()
        for build in (self._page_presets, self._page_effects, self._page_sound,
                      self._page_device, self._page_ui, self._page_hotkeys, self._page_io, self._page_about):
            self.stack.addWidget(build())
        cv.addWidget(self.stack, 1)
        right.addWidget(content, 1)
        right.addWidget(self._build_dock())
        h.addLayout(right, 1)
        self.setCentralWidget(root)
        self.nav[0].setChecked(True)

        self._apply_hotkeys()

    def _build_dock(self):
        dock = QFrame()
        dock.setObjectName("dock")
        h = QHBoxLayout(dock)
        h.setContentsMargins(20, 8, 20, 8)
        h.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(2)
        self.status = QLabel("● Остановлено")
        self.status.setObjectName("h1")
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("muted")
        self.status.setMinimumWidth(170)
        self.stats_label.setMinimumWidth(170)
        self.stats_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        left.addStretch(1)
        left.addWidget(self.status)
        left.addWidget(self.stats_label)
        left.addStretch(1)
        h.addLayout(left, 2)

        self.spectrum = SpectrumWidget()
        self.spectrum.setFixedHeight(58)
        self.spectrum.setMinimumWidth(90)
        h.addWidget(self.spectrum, 3)

        meters = QVBoxLayout()
        meters.setSpacing(4)
        self.meter_in = LevelMeter("IN")
        self.meter_out = LevelMeter("OUT")
        meters.addStretch(1)
        meters.addWidget(self.meter_in)
        meters.addWidget(self.meter_out)
        meters.addStretch(1)
        h.addLayout(meters, 2)

        self.mute_btn = QPushButton("Заглушить")
        self.mute_btn.setObjectName("danger")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Полностью выключить звук (Ctrl+M)")
        self.bypass_btn = QPushButton("Оригинал")
        self.bypass_btn.setCheckable(True)
        self.bypass_btn.setToolTip("Пропустить голос БЕЗ эффектов (Ctrl+B)")
        self.rec_btn = QPushButton("● Запись")
        self.rec_btn.setObjectName("danger")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setEnabled(False)
        self.rec_btn.setToolTip("Записать обработанный голос в WAV (Ctrl+R)")
        self.mute_btn.toggled.connect(lambda c: setattr(self.engine, "mute", c))
        self.bypass_btn.toggled.connect(lambda c: setattr(self.engine, "bypass", c))
        self.rec_btn.toggled.connect(self._on_record_toggled)
        self._fit_btn(self.mute_btn)
        self._fit_btn(self.bypass_btn)
        self._fit_btn(self.rec_btn, "● Запись", "■ Идёт запись…")
        small = QVBoxLayout()
        small.setSpacing(6)
        row1 = QHBoxLayout()
        row1.addWidget(self.mute_btn)
        row1.addWidget(self.bypass_btn)
        small.addLayout(row1)
        small.addWidget(self.rec_btn)
        h.addLayout(small)

        self.power_btn = PowerButton()
        self.power_btn.setToolTip("Старт / стоп (Ctrl+Enter)")
        self.power_btn.toggled.connect(self._on_power)
        h.addWidget(self.power_btn)
        return dock

    @staticmethod
    def _fit_btn(btn, *texts):
        """Ширина кнопки не меньше ширины её текста — подпись никогда не обрезается."""
        fm = btn.fontMetrics()
        w = max(fm.horizontalAdvance(t) for t in (texts or (btn.text(),))) + 42
        btn.setMinimumWidth(w)
        btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    @staticmethod
    def _scroll():
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return sa

    @staticmethod
    def _section(title):
        f = QFrame()
        f.setObjectName("section")
        v = QVBoxLayout(f)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(10)
        t = QLabel(title)
        t.setObjectName("sectionTitle")
        v.addWidget(t)
        return f, v

    @staticmethod
    def _page(spacing=10):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(spacing)
        return w, v

    def _slider_row(self, key):
        title, lo, hi, scale, _fmt, tip = SLIDER_SPECS[key]
        s = ResetSlider(int(round(DEFAULTS[key] / scale)))
        s.setRange(lo, hi)
        s.setToolTip(tip)
        s.valueChanged.connect(self._on_param_changed)
        name = QLabel(title)
        name.setFixedWidth(170)
        val = QLabel()
        val.setObjectName("value")
        val.setFixedWidth(80)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row = QHBoxLayout()
        row.addWidget(name)
        row.addWidget(s, 1)
        row.addWidget(val)
        self.sliders[key], self.labels[key] = s, val
        return row

    def _msg(self, text, ms=6000):
        self.statusBar().showMessage(text, ms)

    # ------------------------------------------------------------ страницы
    def _page_presets(self):
        w, v = self._page()
        bar = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск пресета…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _t: self._rebuild_cards())
        self.preset_chip = QLabel("")
        self.preset_chip.setObjectName("chip")
        rnd = QPushButton("Случайный голос")
        rnd.clicked.connect(self._randomize)
        sav = QPushButton("Сохранить как пресет…")
        sav.setObjectName("primary")
        sav.clicked.connect(self._save_preset)
        bar.addWidget(self.search_edit, 1)
        bar.addWidget(self.preset_chip)
        bar.addWidget(rnd)
        bar.addWidget(sav)
        v.addLayout(bar)
        sc = self._scroll()
        self.grid = CardGrid()
        sc.setWidget(self.grid)
        v.addWidget(sc, 1)
        self.preset_hint = QLabel("")
        self.preset_hint.setObjectName("hint")
        v.addWidget(self.preset_hint)
        return w

    def _page_effects(self):
        w, v = self._page()
        top = QHBoxLayout()
        hint = QLabel("Двойной клик по слайдеру — вернуть значение по умолчанию.")
        hint.setObjectName("hint")
        reset = QPushButton("Сбросить всё")
        reset.clicked.connect(lambda: self._select_preset("Без изменений"))
        top.addWidget(hint, 1)
        top.addWidget(reset)
        v.addLayout(top)
        sc = self._scroll()
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 8, 0)
        iv.setSpacing(12)
        for title, keys in GROUPS:
            f, fv = self._section(title)
            for key in keys:
                if key == "bandpass":
                    self.bp_check = Switch("Телефонный фильтр (300–3400 Гц)")
                    self.bp_check.toggled.connect(self._on_param_changed)
                    fv.addWidget(self.bp_check)
                else:
                    fv.addLayout(self._slider_row(key))
            iv.addWidget(f)
        iv.addStretch(1)
        sc.setWidget(inner)
        v.addWidget(sc, 1)
        return w

    def _page_sound(self):
        w, v = self._page()
        bar = QHBoxLayout()
        add = QPushButton("Добавить звуки…")
        add.setObjectName("primary")
        add.clicked.connect(self._add_sounds)
        vol_l = QLabel("Громкость:")
        self.vol_slider = ResetSlider(100)
        self.vol_slider.setRange(0, 200)
        self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(160)
        self.vol_slider.valueChanged.connect(lambda x: setattr(self.board, "volume", x / 100.0))
        bar.addWidget(add)
        bar.addStretch(1)
        bar.addWidget(vol_l)
        bar.addWidget(self.vol_slider)
        v.addLayout(bar)

        self.player = PlayerBar()
        self.player.prev_btn.clicked.connect(self._play_prev_sound)
        self.player.next_btn.clicked.connect(self._play_next_sound)
        self.player.play_btn.clicked.connect(self._toggle_player_pause)
        self.player.seek.seeked.connect(self.board.seek)
        v.addWidget(self.player)

        sc = self._scroll()
        self.sound_grid = CardGrid()
        sc.setWidget(self.sound_grid)
        v.addWidget(sc, 1)

        f, fv = self._section("Озвучка текста")
        row = QHBoxLayout()
        self.say_edit = QLineEdit()
        self.say_edit.setPlaceholderText("Напишите текст — он будет озвучен в микрофон…")
        self.say_edit.returnPressed.connect(self._speak)
        self.lang_combo = QComboBox()
        for code, nm in (("ru", "Русский"), ("en", "English"), ("uk", "Українська"), ("de", "Deutsch"), ("es", "Español")):
            self.lang_combo.addItem(nm, code)
        self.lang_combo.currentIndexChanged.connect(self._refresh_voices)
        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(190)
        self.speak_btn = QPushButton("Сказать")
        self.speak_btn.setObjectName("primary")
        self.speak_btn.clicked.connect(self._speak)
        row.addWidget(self.say_edit, 1)
        row.addWidget(self.lang_combo)
        row.addWidget(self.voice_combo)
        row.addWidget(self.speak_btn)
        fv.addLayout(row)

        row2 = QHBoxLayout()
        self.tts_speed = ResetSlider(100)
        self.tts_speed.setRange(60, 160)
        self.tts_speed.setValue(100)
        self.tts_speed.setFixedWidth(180)
        self.tts_speed_lbl = QLabel("1.00×")
        self.tts_speed_lbl.setObjectName("value")
        self.tts_speed_lbl.setFixedWidth(52)
        self.tts_speed.valueChanged.connect(lambda x: self.tts_speed_lbl.setText(f"{x / 100:.2f}×"))
        self.voice_dl_btn = QPushButton("Скачать голос…")
        self.voice_dl_btn.clicked.connect(self._show_voice_menu)
        row2.addWidget(QLabel("Скорость речи:"))
        row2.addWidget(self.tts_speed)
        row2.addWidget(self.tts_speed_lbl)
        row2.addStretch(1)
        row2.addWidget(self.voice_dl_btn)
        fv.addLayout(row2)

        self.say_info = QLabel("")
        self.say_info.setObjectName("hint")
        self.say_info.setWordWrap(True)
        fv.addWidget(self.say_info)
        self._refresh_voices()
        v.addWidget(f)
        return w

    # ------------------------------------------------------------ горячие клавиши
    def _page_hotkeys(self):
        w, v = self._page()
        top = QHBoxLayout()
        hint = QLabel("Нажмите на поле и затем на нужное сочетание.  Esc — отмена, Backspace — убрать.")
        hint.setObjectName("hint")
        reset = QPushButton("Сбросить всё")
        reset.clicked.connect(self._hk_reset_all)
        self._fit_btn(reset)
        top.addWidget(hint, 1)
        top.addWidget(reset)
        v.addLayout(top)
        sc = self._scroll()
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 8, 0)
        iv.setSpacing(12)
        f, fv = self._section("Основные действия")
        for aid, title, _d in HOTKEY_ACTIONS:
            fv.addWidget(self._hk_row("a:" + aid, title, resettable=True))
        iv.addWidget(f)
        f2, f2v = self._section("Звуки саундборда")
        self.hk_sounds_lay = QVBoxLayout()
        self.hk_sounds_lay.setSpacing(8)
        f2v.addLayout(self.hk_sounds_lay)
        iv.addWidget(f2)
        note = QLabel(f"Alt+1…{len(PAGES)} — переключение страниц (не меняется).  "
                      "Сочетания работают, когда окно программы активно.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        iv.addWidget(note)
        iv.addStretch(1)
        sc.setWidget(inner)
        v.addWidget(sc, 1)
        self._rebuild_hk_sounds()
        return w

    def _hk_row(self, key, title, resettable=False):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        name = PaintLabel(title, "text")
        name.setMinimumWidth(120)
        name.setToolTip(title)
        edit = HotkeyEdit(self._hk_get(key))
        edit.changed.connect(lambda s, k=key: self._on_hotkey_changed(k, s))
        h.addWidget(name, 1)
        h.addWidget(edit)
        if resettable:
            rb = QPushButton("По умолчанию")
            rb.clicked.connect(lambda _=False, k=key: self._on_hotkey_changed(k, HOTKEY_DEFAULTS[k[2:]]))
            self._fit_btn(rb)
            h.addWidget(rb)
        clear = QPushButton("Убрать")
        clear.clicked.connect(lambda _=False, k=key: self._on_hotkey_changed(k, ""))
        self._fit_btn(clear)
        h.addWidget(clear)
        self.hk_edits[key] = edit
        return row

    def _rebuild_hk_sounds(self):
        for k in [k for k in self.hk_edits if k.startswith("s:")]:
            self.hk_edits.pop(k, None)
        lay = self.hk_sounds_lay
        while lay.count():
            w = lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if not self.sound_paths:
            lbl = QLabel("Пока нет звуков — добавьте их на странице «Саундборд».")
            lbl.setObjectName("hint")
            lay.addWidget(lbl)
        for p in self.sound_paths:
            lay.addWidget(self._hk_row("s:" + p, os.path.splitext(os.path.basename(p))[0]))

    def _sync_hk_actions(self):
        for k, ed in self.hk_edits.items():
            if k.startswith("a:"):
                ed.set_sequence(self._hk_get(k))

    def _hk_owners(self):
        return ["a:" + a for a, _t, _d in HOTKEY_ACTIONS] + ["s:" + p for p in self.sound_paths]

    def _hk_get(self, key):
        kind, _, ident = key.partition(":")
        return (self.hotkeys if kind == "a" else self.sound_keys).get(ident, "")

    def _hk_put(self, key, seq):
        kind, _, ident = key.partition(":")
        (self.hotkeys if kind == "a" else self.sound_keys)[ident] = seq

    def _hk_title(self, key):
        kind, _, ident = key.partition(":")
        if kind == "a":
            return next((t for a, t, _d in HOTKEY_ACTIONS if a == ident), ident)
        return os.path.splitext(os.path.basename(ident))[0]

    def _assign_default_sound_keys(self):
        """Новым звукам автоматически даём первое свободное Ctrl+1…Ctrl+9."""
        used = {v for v in list(self.hotkeys.values()) + list(self.sound_keys.values()) if v}
        for p in self.sound_paths:
            if p in self.sound_keys:
                continue
            for n in range(1, 10):
                k = norm_key(f"Ctrl+{n}")
                if k not in used:
                    self.sound_keys[p] = k
                    used.add(k)
                    break
            else:
                self.sound_keys[p] = ""

    def _on_hotkey_changed(self, key, seq):
        seq = norm_key(seq)
        note = ""
        reserved = {norm_key(f"Alt+{i + 1}") for i in range(len(PAGES))}
        if seq and seq in reserved:
            note = f"{key_text(seq)} занято: Alt+цифры переключают страницы"
            seq = self._hk_get(key)
        elif seq:
            for other in self._hk_owners():
                if other != key and self._hk_get(other) == seq:
                    self._hk_put(other, "")
                    if other in self.hk_edits:
                        self.hk_edits[other].set_sequence("")
                    note = f"{key_text(seq)} забрано у «{self._hk_title(other)}»"
        self._hk_put(key, seq)
        if key in self.hk_edits:
            self.hk_edits[key].set_sequence(seq)
        self._hk_commit()
        if note:
            self._msg(note, 6000)

    def _hk_reset_all(self):
        self.hotkeys = dict(HOTKEY_DEFAULTS)
        self.sound_keys = {}
        self._assign_default_sound_keys()
        self._sync_hk_actions()
        self._rebuild_hk_sounds()
        self._hk_commit()
        self._msg("Горячие клавиши сброшены", 3000)

    def _hk_commit(self, rebuild_cards=True):
        self._apply_hotkeys()
        self.settings.setValue("hotkeys", json.dumps(self.hotkeys))
        self.settings.setValue("sound_keys", json.dumps(self.sound_keys))
        if rebuild_cards:
            self._rebuild_sound_cards(appear=False)

    def _after_sounds_changed(self):
        self._assign_default_sound_keys()
        self._rebuild_hk_sounds()
        self._hk_commit(rebuild_cards=False)

    def _load_hotkeys(self):
        s = self.settings
        try:
            saved = json.loads(s.value("hotkeys", "{}", type=str) or "{}")
        except Exception:
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        self.hotkeys = {a: norm_key(saved[a] if isinstance(saved.get(a), str) else d)
                        for a, _t, d in HOTKEY_ACTIONS}
        try:
            sk = json.loads(s.value("sound_keys", "{}", type=str) or "{}")
        except Exception:
            sk = {}
        sk = sk if isinstance(sk, dict) else {}
        self.sound_keys = {p: norm_key(sk[p]) for p in self.sound_paths if isinstance(sk.get(p), str)}
        seen = set()                                    # на случай дублей в сохранённых настройках
        for key in self._hk_owners():
            v = self._hk_get(key)
            if v and v in seen:
                self._hk_put(key, "")
            elif v:
                seen.add(v)
        self._assign_default_sound_keys()
        self._sync_hk_actions()
        self._rebuild_hk_sounds()
        self._apply_hotkeys()

    def _apply_hotkeys(self):
        for sc in self._shortcuts:
            sc.setEnabled(False)
            sc.setParent(None)
            sc.deleteLater()
        self._shortcuts = []

        def add(seq, fn):
            if not seq:
                return
            sc = QShortcut(QKeySequence.fromString(seq, QKeySequence.SequenceFormat.PortableText), self)
            sc.setAutoRepeat(False)
            sc.activated.connect(fn)
            self._shortcuts.append(sc)

        actions = {
            "run": self._toggle_run,
            "mute": self.mute_btn.toggle,
            "bypass": self.bypass_btn.toggle,
            "rec": self.rec_btn.click,
            "preset_prev": lambda: self._cycle_preset(-1),
            "preset_next": lambda: self._cycle_preset(1),
            "snd_pause": self._toggle_player_pause,
            "snd_prev": self._play_prev_sound,
            "snd_next": self._play_next_sound,
        }
        for aid, fn in actions.items():
            add(self.hotkeys.get(aid, ""), fn)
        for path, seq in self.sound_keys.items():
            if path in self.sound_paths:
                add(seq, lambda p=path: self._play_path(p))
        for i in range(len(PAGES)):
            add(f"Alt+{i + 1}", lambda i=i: self._goto(i))
        self._refresh_hotkey_labels()

    def _refresh_hotkey_labels(self):
        def tip(text, aid):
            k = key_text(self.hotkeys.get(aid, ""))
            return f"{text} ({k})" if k else text
        self.mute_btn.setToolTip(tip("Полностью выключить звук", "mute"))
        self.bypass_btn.setToolTip(tip("Пропустить голос БЕЗ эффектов", "bypass"))
        self.rec_btn.setToolTip(tip("Записать обработанный голос в WAV", "rec"))
        self.power_btn.setToolTip(tip("Старт / стоп", "run"))
        keys = " / ".join(k for k in (key_text(self.hotkeys.get("preset_prev", "")),
                                      key_text(self.hotkeys.get("preset_next", ""))) if k)
        self.preset_hint.setText("ПКМ по своему пресету — удалить"
                                 + (f"  •  {keys} — переключение пресетов" if keys else ""))

    def _page_device(self):
        w, v = self._page(12)
        f, fv = self._section("Устройства ввода и вывода")
        self.in_combo, self.out_combo = QComboBox(), QComboBox()
        self.sr_combo = QComboBox()
        self.sr_combo.addItems(["44100", "48000"])
        self.sr_combo.setCurrentText("48000")
        self.block_combo = QComboBox()
        self.block_combo.addItems(["128", "256", "512", "1024"])
        self.block_combo.setCurrentText("512")
        self.refresh_btn = QPushButton("Обновить список")
        self.refresh_btn.clicked.connect(lambda: self._refresh_devices(reinit=True))
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.addRow("Микрофон:", self.in_combo)
        form.addRow("Выход:", self.out_combo)
        form.addRow("Частота, Гц:", self.sr_combo)
        form.addRow("Размер блока:", self.block_combo)
        fv.addLayout(form)
        fv.addWidget(self.refresh_btn)
        self.awake_check = Switch("Не усыплять звуковую карту (убирает щелчок при включении)")
        self.awake_check.toggled.connect(self._apply_awake)
        self.out_combo.currentIndexChanged.connect(self._apply_awake)
        fv.addWidget(self.awake_check)
        awake_hint = QLabel("Тихий фоновый поток не даёт карте «заснуть»: щёлкает она именно при пробуждении.")
        awake_hint.setObjectName("hint")
        awake_hint.setWordWrap(True)
        fv.addWidget(awake_hint)
        v.addWidget(f)

        f2, f2v = self._section("Виртуальный микрофон")
        self.vm_check = Switch("Виртуальный микрофон (для Discord / OBS / игр)")
        self.vm_check.toggled.connect(self._on_virtual_toggled)
        self.vm_check.toggled.connect(lambda *_: QTimer.singleShot(0, self._apply_awake))
        self.monitor_check = Switch("Слышать себя в наушниках")
        self.monitor_check.setEnabled(False)
        if not VirtualMic.available():
            self.vm_check.setEnabled(False)
        f2v.addWidget(self.vm_check)
        f2v.addWidget(self.monitor_check)
        v.addWidget(f2)
        v.addStretch(1)
        return w

    def _page_ui(self):
        w, v = self._page(12)
        f, fv = self._section("Оформление")
        self.theme_switch = Switch("Тёмная тема")
        self.theme_switch.setChecked(UI.theme == "dark")
        self.theme_switch.toggled.connect(self._on_theme_switch)
        fv.addWidget(self.theme_switch)
        v.addWidget(f)

        f_grad, f_grad_v = self._section("Градиентные темы")
        row_grad = QHBoxLayout()
        row_grad.setSpacing(10)
        for name, pair in GRADIENTS.items():
            sw = Swatch(pair)
            sw.setToolTip(f"Тема «{name}» ({pair[0]} → {pair[1]})")
            sw.setChecked(name == UI.accent_name and UI.is_gradient)
            sw.clicked.connect(lambda _=False, n=name: self._on_gradient(n))
            self.grad_swatches[name] = sw
            row_grad.addWidget(sw)
        row_grad.addStretch(1)
        f_grad_v.addLayout(row_grad)
        v.addWidget(f_grad)

        f2, f2v = self._section("Однотонные акценты")
        row_acc = QHBoxLayout()
        row_acc.setSpacing(10)
        for name, col in ACCENTS.items():
            sw = Swatch(col)
            sw.setToolTip(name)
            sw.setChecked(name == UI.accent_name and not UI.is_gradient)
            sw.clicked.connect(lambda _=False, n=name: self._on_accent(n))
            self.swatches[name] = sw
            row_acc.addWidget(sw)
        row_acc.addStretch(1)
        f2v.addLayout(row_acc)
        v.addWidget(f2)

        f_c, f_cv = self._section("Свой цвет")
        row_cs = QHBoxLayout()
        row_cs.setSpacing(10)
        self.custom_solid_sw = Swatch(UI.custom_solid)
        self.custom_solid_sw.setToolTip("Применить свой однотонный цвет")
        self.custom_solid_sw.setChecked(UI.accent_name == CUSTOM_SOLID)
        self.custom_solid_sw.clicked.connect(lambda _=False: self._apply_custom_solid())
        btn_cs = QPushButton("Выбрать цвет…")
        btn_cs.clicked.connect(lambda _=False: self._pick_custom_solid())
        row_cs.addWidget(self.custom_solid_sw)
        row_cs.addWidget(QLabel("Однотонный"))
        row_cs.addStretch(1)
        row_cs.addWidget(btn_cs)
        f_cv.addLayout(row_cs)

        row_cg = QHBoxLayout()
        row_cg.setSpacing(10)
        self.custom_grad_sw = Swatch((UI.custom_g1, UI.custom_g2))
        self.custom_grad_sw.setToolTip("Применить свой градиент")
        self.custom_grad_sw.setChecked(UI.accent_name == CUSTOM_GRAD)
        self.custom_grad_sw.clicked.connect(lambda _=False: self._apply_custom_gradient())
        btn_g1 = QPushButton("Цвет 1…")
        btn_g1.clicked.connect(lambda _=False: self._pick_grad_stop(0))
        btn_g2 = QPushButton("Цвет 2…")
        btn_g2.clicked.connect(lambda _=False: self._pick_grad_stop(1))
        row_cg.addWidget(self.custom_grad_sw)
        row_cg.addWidget(QLabel("Градиент"))
        row_cg.addStretch(1)
        row_cg.addWidget(btn_g1)
        row_cg.addWidget(btn_g2)
        f_cv.addLayout(row_cg)
        v.addWidget(f_c)

        f3, f3v = self._section("Анимации")
        self.anim_switch = Switch("Плавные анимации")
        self.anim_switch.setChecked(UI.anim)
        self.anim_switch.toggled.connect(self._on_anim)
        f3v.addWidget(self.anim_switch)
        v.addWidget(f3)
        v.addStretch(1)
        return w

    def _page_io(self):
        w, v = self._page(12)
        f, fv = self._section("Свои пресеты")
        row = QHBoxLayout()
        ex = QPushButton("Экспорт в JSON…")
        ex.clicked.connect(self._export_presets)
        im = QPushButton("Импорт из JSON…")
        im.setObjectName("primary")
        im.clicked.connect(self._import_presets)
        row.addWidget(ex)
        row.addWidget(im)
        row.addStretch(1)
        fv.addLayout(row)
        v.addWidget(f)
        f2, f2v = self._section("Папка настроек")
        op = QPushButton("Открыть папку")
        op.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(_config_dir())))
        f2v.addWidget(op)
        v.addWidget(f2)
        v.addStretch(1)
        return w

    def _page_about(self):
        w, v = self._page(12)
        f, fv = self._section(f"{APP_TITLE} {APP_VERSION}")
        t = QLabel("Голосовой эффект-процессор реального времени для Linux.")
        t.setWordWrap(True)
        fv.addWidget(t)
        v.addWidget(f)
        f2, f2v = self._section("Горячие клавиши")
        k = QLabel("Все сочетания клавиш настраиваются на странице «Горячие клавиши».\n"
                   f"Alt+1…{len(PAGES)} — переключение страниц.")
        f2v.addWidget(k)
        v.addWidget(f2)
        f3, f3v = self._section("Диагностика")
        d = QLabel(self._diagnostics())
        d.setObjectName("hint")
        f3v.addWidget(d)
        v.addWidget(f3)
        v.addStretch(1)
        return w

    @staticmethod
    def _diagnostics() -> str:
        lines = [f"Python {sys.version.split()[0]}", f"numpy {np.__version__}"]
        try:
            lines.append(f"sounddevice {sd.__version__}")
        except Exception:
            lines.append("sounddevice: недоступен")
        lines.append("pactl: " + ("найден" if VirtualMic.available() else "не найден"))
        lines.append("espeak-ng: " + ("найден" if espeak_exe() else "не найден"))
        return "\n".join(lines)

    def _goto(self, i):
        for k, b in enumerate(self.nav):
            b.setChecked(k == i)
        self.page_title.setText(PAGES[i][2])
        self.stack.fade_to(i)
        if i != self._cur_page:
            if i == 0:
                self._appear(list(self.cards.values()))
            elif i == 2:
                self._appear(self.sound_cards)
        self._cur_page = i

    @staticmethod
    def _appear(cards):
        for i, c in enumerate(cards):
            c.appear(i * 22)

    def _all_presets(self) -> dict:
        d = dict(PRESETS)
        d.update(self.user_presets)
        return d

    def _rebuild_cards(self, appear=True):
        q = self.search_edit.text().strip().lower()
        cards, self.cards = [], {}
        for name, params in self._all_presets().items():
            if q and q not in name.lower():
                continue
            user = name in self.user_presets
            c = Card(name, preset_icon(params), "МОЁ" if user else "")
            c.clicked.connect(lambda _=False, n=name: self._select_preset(n))
            if user:
                c.on_context = self._card_menu
            if name == self.current_preset:
                c.setChecked(True)
                c.sm_set("sel", 1.0, instant=True)
            cards.append(c)
            self.cards[name] = c
        self.grid.set_cards(cards)
        if appear:
            self._appear(cards)
        self._update_chip()

    def _update_chip(self):
        self.preset_chip.setText("" if self.current_preset else "● Свои настройки")

    def _set_current_preset(self, name):
        self.current_preset = name
        for n, c in self.cards.items():
            c.setChecked(n == name)
        self._update_chip()

    def _select_preset(self, name):
        params = self._all_presets().get(name)
        if params is None:
            return
        self._set_widgets(params)
        self._set_current_preset(name)
        self._apply_params_to_chain()

    def _cycle_preset(self, step):
        names = list(self._all_presets())
        if not names:
            return
        i = names.index(self.current_preset) if self.current_preset in names else -1
        self._select_preset(names[(i + step) % len(names)])
        self._msg(f"Пресет: {self.current_preset}", 2500)

    def _card_menu(self, card, pos):
        menu = QMenu(self)
        act = menu.addAction("Удалить пресет")
        if menu.exec(pos) is act:
            self._delete_preset(card.title)

    def _set_widgets(self, params: dict):
        p = sanitize_params(params)
        self._updating = True
        try:
            for key, s in self.sliders.items():
                s.setValue(int(round(p[key] / SLIDER_SPECS[key][3])))
            self.bp_check.setChecked(p["bandpass"])
        finally:
            self._updating = False
        self._update_labels()

    def _params_from_widgets(self) -> dict:
        p = {k: round(s.value() * SLIDER_SPECS[k][3], 6) for k, s in self.sliders.items()}
        p["bandpass"] = self.bp_check.isChecked()
        return sanitize_params(p)

    def _on_param_changed(self, _value=None):
        if self._updating:
            return
        if self.current_preset is not None:
            self._set_current_preset(None)
        self._update_labels()
        self._apply_params_to_chain()

    def _update_labels(self):
        for key, s in self.sliders.items():
            self.labels[key].setText(SLIDER_SPECS[key][4](s.value()))

    def _apply_params_to_chain(self):
        self.engine.set_params(self._params_from_widgets())

    def _randomize(self):
        self._set_widgets(random_params())
        self._set_current_preset(None)
        self._apply_params_to_chain()
        self._msg("Случайный голос сохранён в настройки", 4000)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "Новый пресет", "Название пресета:")
        name = name.strip()
        if not ok or not name:
            return
        if name in PRESETS:
            QMessageBox.warning(self, "Имя занято", f"«{name}» — встроенный пресет.")
            return
        self.user_presets[name] = self._params_from_widgets()
        save_user_presets(self.user_presets)
        self.current_preset = name
        self._rebuild_cards()
        self._msg(f"Пресет «{name}» сохранён", 3000)

    def _delete_preset(self, name):
        if name not in self.user_presets:
            return
        self.user_presets.pop(name, None)
        save_user_presets(self.user_presets)
        if self.current_preset == name:
            self.current_preset = None
        self._rebuild_cards(appear=False)

    def _export_presets(self):
        if not self.user_presets:
            QMessageBox.information(self, "Экспорт", "Своих пресетов пока нет.")
            return
        path, _f = QFileDialog.getSaveFileName(self, "Экспорт", os.path.expanduser("~/voicechanger_presets.json"), "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"presets": self.user_presets}, f, ensure_ascii=False, indent=2)
            self._msg(f"Экспортировано: {path}", 6000)
        except Exception as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))

    def _import_presets(self):
        path, _f = QFileDialog.getOpenFileName(self, "Импорт", os.path.expanduser("~"), "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("presets", data) if isinstance(data, dict) else {}
            for k, val in raw.items():
                if isinstance(val, dict) and str(k) not in PRESETS:
                    self.user_presets[str(k)] = sanitize_params(val)
        except Exception as exc:
            QMessageBox.warning(self, "Ошибка", str(exc))
            return
        save_user_presets(self.user_presets)
        self._rebuild_cards()

    def _restyle(self):
        apply_ui_theme(QApplication.instance())
        for wdg in (self.findChildren(QWidget) or []):
            wdg.update()

    def _on_theme_switch(self, dark: bool):
        UI.theme = "dark" if dark else "light"
        self._restyle()

    def _sync_swatches(self):
        for n, sw in self.swatches.items():
            sw.setChecked(n == UI.accent_name and not UI.is_gradient)
        for n, sw in self.grad_swatches.items():
            sw.setChecked(n == UI.accent_name and UI.is_gradient)
        self.custom_solid_sw.setChecked(UI.accent_name == CUSTOM_SOLID)
        self.custom_grad_sw.setChecked(UI.accent_name == CUSTOM_GRAD)

    def _on_accent(self, name: str):
        UI.set_accent(name)
        self._restyle()
        self._sync_swatches()

    def _on_gradient(self, name: str):
        UI.set_gradient(name)
        self._restyle()
        self._sync_swatches()

    def _pick_color(self, initial: str, title: str):
        col = QColorDialog.getColor(QColor(initial), self, title)
        return col.name() if col.isValid() else None

    def _apply_custom_solid(self):
        UI.set_custom_solid(UI.custom_solid)
        self.custom_solid_sw.color_data = UI.custom_solid
        self.custom_solid_sw.update()
        self._restyle()
        self._sync_swatches()

    def _pick_custom_solid(self):
        c = self._pick_color(UI.custom_solid, "Свой цвет")
        if c:
            UI.custom_solid = c
            self._apply_custom_solid()

    def _apply_custom_gradient(self):
        UI.set_custom_gradient(UI.custom_g1, UI.custom_g2)
        self.custom_grad_sw.color_data = (UI.custom_g1, UI.custom_g2)
        self.custom_grad_sw.update()
        self._restyle()
        self._sync_swatches()

    def _pick_grad_stop(self, which: int):
        cur = UI.custom_g1 if which == 0 else UI.custom_g2
        c = self._pick_color(cur, "Градиент: цвет %d" % (which + 1))
        if not c:
            return
        if which == 0:
            UI.custom_g1 = c
        else:
            UI.custom_g2 = c
        self._apply_custom_gradient()

    def _on_anim(self, on: bool):
        UI.anim = bool(on)

    def _refresh_devices(self, reinit=False):
        if reinit and not self.engine.running:
            self._awake.stop()
            AudioEngine.reinit_portaudio()
        prev_in, prev_out = self.in_combo.currentText(), self.out_combo.currentText()
        self.in_combo.blockSignals(True)
        self.out_combo.blockSignals(True)
        self.in_combo.clear()
        self.out_combo.clear()
        try:
            ins, outs = AudioEngine.devices()
        except Exception:
            ins, outs = [], []
        for i, label in ins:
            self.in_combo.addItem(label, i)
        for i, label in outs:
            self.out_combo.addItem(label, i)
        self.in_combo.blockSignals(False)
        self.out_combo.blockSignals(False)
        try:
            d_in, d_out = sd.default.device
            for combo, dev in ((self.in_combo, d_in), (self.out_combo, d_out)):
                k = combo.findData(int(dev)) if dev is not None and int(dev) >= 0 else -1
                if k >= 0:
                    combo.setCurrentIndex(k)
        except Exception:
            pass
        for combo, prev in ((self.in_combo, prev_in), (self.out_combo, prev_out)):
            k = combo.findText(prev) if prev else -1
            if k >= 0:
                combo.setCurrentIndex(k)
        if not self.engine.running:
            self.power_btn.setEnabled(bool(ins and outs))
        self._apply_awake()

    def _awake_device(self):
        """Физический выход, на котором мы слышим звук: при виртуальном микрофоне — системный по умолчанию."""
        if self.vm_check.isChecked() and self._vm_active:
            return None
        return self.out_combo.currentData()

    def _apply_awake(self, *_):
        chk = getattr(self, "awake_check", None)
        if chk is None:
            return
        if chk.isChecked() and not self.engine.running:
            try:
                rate = int(self.sr_combo.currentText())
            except Exception:
                rate = None
            self._awake.start(self._awake_device(), rate)
        else:
            self._awake.stop()

    def _on_virtual_toggled(self, checked: bool):
        if checked:
            try:
                VirtualMic.create()
                self._vm_active = True
                self._msg("Виртуальный микрофон создан", 6000)
            except Exception as exc:
                QMessageBox.warning(self, "Ошибка", str(exc))
                self.vm_check.blockSignals(True)
                self.vm_check.setChecked(False)
                self.vm_check.blockSignals(False)
                checked = False
        else:
            VirtualMic.destroy()
            self._vm_active = False
        self.monitor_check.setEnabled(checked and not self.engine.running)

    def _virtual_output_row(self) -> int:
        for pat in ("pulse", "pipewire", "default"):
            for k in range(self.out_combo.count()):
                if pat in self.out_combo.itemText(k).lower():
                    return k
        return -1

    def _route_virtual(self):
        if not self.engine.running:
            return
        VirtualMic.route_process(os.getpid())

    def _toggle_run(self):
        if self.power_btn.isEnabled() or self.engine.running:
            self.power_btn.toggle()

    def _set_running_ui(self, running: bool):
        self.rec_btn.setEnabled(running)
        for w in (self.in_combo, self.out_combo, self.sr_combo, self.block_combo, self.refresh_btn):
            w.setEnabled(not running)
        self.vm_check.setEnabled(not running and VirtualMic.available())
        self.monitor_check.setEnabled(not running and self.vm_check.isChecked())

    def _on_power(self, on: bool):
        if on:
            if not self._start():
                self.power_btn.blockSignals(True)
                self.power_btn.setChecked(False)
                self.power_btn.blockSignals(False)
                self.power_btn.update()
                return
            self.power_btn.set_label("Остановить")
        else:
            self._stop()
            self.power_btn.set_label("Включить")

    def _start(self) -> bool:
        in_idx, out_idx = self.in_combo.currentData(), self.out_combo.currentData()
        if in_idx is None or out_idx is None:
            QMessageBox.warning(self, "Нет устройств", "Выберите микрофон и динамики.")
            return False
        virtual = self.vm_check.isChecked() and self._vm_active
        if virtual:
            row = self._virtual_output_row()
            if row >= 0:
                self.out_combo.setCurrentIndex(row)
                out_idx = self.out_combo.currentData()
            os.environ["PULSE_SINK"] = VirtualMic.SINK
        else:
            os.environ.pop("PULSE_SINK", None)
        try:
            self.engine.start(in_idx, out_idx, int(self.sr_combo.currentText()),
                              int(self.block_combo.currentText()), self._params_from_widgets())
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка аудио", str(exc))
            return False
        self.engine.mute = self.mute_btn.isChecked()
        self.engine.bypass = self.bypass_btn.isChecked()
        self._awake.stop()          # у движка свой поток, второй на этой карте не нужен
        self._health_warned = False
        self._set_running_ui(True)
        self.status.setText(f"<span style='color:#22c55e'>●</span> Работает — {self.engine.sr} Гц")
        if virtual:
            if self.monitor_check.isChecked():
                try:
                    VirtualMic.start_loopback()
                except Exception:
                    pass
            QTimer.singleShot(700, self._route_virtual)
        return True

    def _stop(self):
        if self.rec_btn.isChecked():
            self.rec_btn.setChecked(False)
        self.board.stop()
        self.engine.stop()
        if self._vm_active:
            VirtualMic.stop_loopback()
        self._set_running_ui(False)
        self.status.setText("● Остановлено")
        self.stats_label.setText("")
        self._apply_awake()

    def _on_record_toggled(self, checked: bool):
        if checked:
            if not self.engine.running:
                self.rec_btn.blockSignals(True)
                self.rec_btn.setChecked(False)
                self.rec_btn.blockSignals(False)
                return
            self.engine.start_record()
            self.rec_btn.setText("■ Идёт запись…")
            return
        self.rec_btn.setText("● Запись")
        data = self.engine.stop_record()
        if data is None or len(data) == 0:
            return
        folder = os.path.expanduser("~/Music")
        if not os.path.isdir(folder):
            folder = os.path.expanduser("~")
        path = os.path.join(folder, time.strftime("VoiceChanger_%Y%m%d_%H%M%S.wav"))
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.engine.sr)
                w.writeframes(data.tobytes())
            self._msg(f"Запись сохранена: {path}", 10000)
        except Exception as exc:
            QMessageBox.warning(self, "Запись", str(exc))

    def _add_sounds(self):
        filt = ("Звуки (*.wav *.mp3 *.ogg *.flac *.m4a *.aac *.opus);;Все файлы (*)")
        files, _f = QFileDialog.getOpenFileNames(self, "Добавить звуки", os.path.expanduser("~"), filt)
        for f in files:
            if f in self.sound_paths:
                continue
            try:
                self._bars[f] = sound_bars(f)
                self.sound_paths.append(f)
            except Exception:
                pass
        self._after_sounds_changed()
        self._rebuild_sound_cards()

    def _remove_sound(self, card, pos):
        menu = QMenu(self)
        a_key = menu.addAction("Горячая клавиша…")
        a_del = menu.addAction("Убрать из списка")
        act = menu.exec(pos)
        if act is a_key:
            self._goto(PAGE_HOTKEYS)
        elif act is a_del and card.path in self.sound_paths:
            self.sound_paths.remove(card.path)
            self.sound_keys.pop(card.path, None)
            self._rebuild_hk_sounds()
            self._hk_commit(rebuild_cards=False)
            self._rebuild_sound_cards(appear=False)

    def _rebuild_sound_cards(self, appear=True):
        cards = []
        for i, path in enumerate(self.sound_paths):
            bars = self._bars.get(path)
            if bars is None:
                try:
                    bars = self._bars[path] = sound_bars(path)
                except Exception:
                    bars = np.zeros(40)
            name = os.path.splitext(os.path.basename(path))[0]
            c = Card(name, sound_icon(bars), key_text(self.sound_keys.get(path, "")))
            c.setCheckable(False)
            c.path = path
            c.clicked.connect(lambda _=False, idx=i: self._play_index(idx))
            c.on_context = self._remove_sound
            cards.append(c)
        self.sound_cards = cards
        self.sound_grid.set_cards(cards)
        if appear:
            self._appear(cards)

    def _play_index(self, i):
        if not (0 <= i < len(self.sound_paths)):
            return
        if not self.engine.running:
            self._msg("Сначала включите смену голоса", 4000)
            return
        try:
            self.current_sound_index = i
            arr = self.board.play_file(self.sound_paths[i], self.engine.sr)
            if i < len(self.sound_cards):
                self.sound_cards[i].flash()
            name = os.path.splitext(os.path.basename(self.sound_paths[i]))[0]
            self.player.set_track(name, array_bars(arr))
        except Exception as exc:
            QMessageBox.warning(self, "Звук", str(exc))

    def _play_path(self, path):
        if path in self.sound_paths:
            self._play_index(self.sound_paths.index(path))

    def _play_next_sound(self):
        if not self.sound_paths:
            return
        next_idx = (getattr(self, "current_sound_index", -1) + 1) % len(self.sound_paths)
        self._play_index(next_idx)

    def _play_prev_sound(self):
        if not self.sound_paths:
            return
        prev_idx = (getattr(self, "current_sound_index", 0) - 1) % len(self.sound_paths)
        self._play_index(prev_idx)

    def _toggle_player_pause(self):
        if not self.board.busy:
            if self.sound_paths:
                idx = getattr(self, "current_sound_index", 0)
                if idx < 0:
                    idx = 0
                self._play_index(idx)
            return
        self.board.toggle_pause()

    # ------------------------------------------------------------ озвучка
    SAY_HINT = "Форматы звуков: WAV, MP3, OGG, FLAC, M4A, AAC, OPUS. Горячие клавиши звуков — на странице «Горячие клавиши»."

    def _refresh_voices(self, *_):
        lang = self.lang_combo.currentData()
        cb = self.voice_combo
        cb.blockSignals(True)
        cb.clear()
        have_piper = False
        for l, base, path in piper_voices():
            if l == lang:
                cb.addItem(f"{voice_label(base)} · Piper", ("piper", path))
                have_piper = True
        if espeak_exe():
            cb.addItem("espeak-ng · робот", ("espeak", None))
        cb.blockSignals(False)
        ok = cb.count() > 0
        self.speak_btn.setEnabled(ok and not self._tts_busy)
        if not ok:
            self.say_info.setText("Нет голосов: нажми «Скачать голос…» (нужен Piper) или поставь espeak-ng.")
        elif not have_piper:
            self.say_info.setText("Сейчас звучит espeak (робот). Для живого голоса нажми «Скачать голос…».")
        else:
            self.say_info.setText(self.SAY_HINT)

    def _select_voice(self, key: str):
        """key — путь к .onnx, его имя без расширения или 'espeak'."""
        for i in range(self.voice_combo.count()):
            kind, path = self.voice_combo.itemData(i)
            if (kind == "espeak" and key == "espeak") or (path and key in (path, os.path.basename(path)[:-5])):
                self.voice_combo.setCurrentIndex(i)
                return

    def _show_voice_menu(self):
        menu = QMenu(self)
        have = {b for _l, b, _p in piper_voices()}
        lang = self.lang_combo.currentData()
        for l, label, rel in PIPER_CATALOG:
            if l != lang:
                continue
            base = os.path.basename(rel)
            act = menu.addAction(("✓ " if base in have else "") + label)
            act.setEnabled(base not in have and not self._voice_dl_busy)
            act.triggered.connect(lambda _=False, rel=rel, label=label: self._download_voice(rel, label))
        if menu.isEmpty():
            menu.addAction("Для этого языка нет голосов в списке").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Добавить голос из файла…").triggered.connect(self._import_voice)
        menu.addAction("Открыть страницу голосов в браузере").triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://huggingface.co/" + PIPER_REPO.replace("resolve", "tree") + lang)))
        menu.addAction("Прокси для скачивания…" + (" ✓" if self._tts_proxy else "")).triggered.connect(self._set_proxy)
        menu.addAction("Открыть папку с голосами").triggered.connect(self._open_voices_dir)
        menu.exec(self.voice_dl_btn.mapToGlobal(QPoint(0, self.voice_dl_btn.height())))

    def _set_proxy(self):
        txt, ok = QInputDialog.getText(
            self, "Прокси для скачивания голосов",
            "HTTP-прокси, например http://127.0.0.1:7890 (пусто — без прокси):",
            QLineEdit.EchoMode.Normal, self._tts_proxy)
        if ok:
            self._tts_proxy = txt.strip()

    def _import_voice(self):
        """Ручной обход: скачай .onnx и .onnx.json любым способом и выбери их здесь."""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выбери файлы голоса (.onnx и .onnx.json)", os.path.expanduser("~"),
            "Голоса Piper (*.onnx *.onnx.json)")
        if not files:
            return
        os.makedirs(voices_dir(), exist_ok=True)
        base = None
        for fp in files:
            shutil.copy2(fp, os.path.join(voices_dir(), os.path.basename(fp)))
            if fp.endswith(".onnx"):
                base = os.path.basename(fp)[:-5]
        self._refresh_voices()
        if base and any(b == base for _l, b, _p in piper_voices()):
            self._select_voice(base)
            self.say_info.setText(f"Голос «{voice_label(base)}» добавлен.")
        else:
            self.say_info.setText("Нужны оба файла: .onnx и .onnx.json с одинаковым именем.")

    def _open_voices_dir(self):
        os.makedirs(voices_dir(), exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(voices_dir()))

    def _download_voice(self, rel, label):
        if self._voice_dl_busy:
            return
        self._voice_dl_busy = True
        self.voice_dl_btn.setEnabled(False)
        self.say_info.setText(f"Скачиваю «{label}»… 0 %")
        last = [-1]

        def prog(fr):
            pc = int(fr * 100)
            if pc != last[0]:
                last[0] = pc
                self.tts_status.emit(f"Скачиваю «{label}»… {pc} %")

        def work():
            try:
                download_piper_voice(rel, prog, self._tts_proxy)
                self.voice_downloaded.emit(os.path.basename(rel))
            except Exception as exc:
                self.tts_failed.emit(f"Не удалось скачать голос ({exc}). Попробуй «Прокси для скачивания…» или «Добавить голос из файла…»")
                self.voice_downloaded.emit("")
        threading.Thread(target=work, daemon=True).start()

    def _on_voice_downloaded(self, base):
        self._voice_dl_busy = False
        self.voice_dl_btn.setEnabled(True)
        if not base:
            return
        self._refresh_voices()
        self._select_voice(base)
        self.say_info.setText(f"Голос «{voice_label(base)}» готов. Жми «Сказать».")

    def _speak(self):
        text = self.say_edit.text().strip()
        if not text or self._tts_busy:
            return
        if not self.engine.running:
            self._msg("Сначала включите смену голоса", 4000)
            return
        data = self.voice_combo.currentData()
        if not data:
            return
        kind, model = data
        lang = self.lang_combo.currentData()
        speed = self.tts_speed.value() / 100.0
        sr = self.engine.sr
        self._tts_busy = True
        self.speak_btn.setEnabled(False)
        self.speak_btn.setText("Синтез…")

        def work():
            try:
                a, s = synth_piper(text, model, speed) if kind == "piper" else synth_speech(text, lang, speed)
                self.tts_done.emit(tts_finish(a, s, sr), text)
            except Exception as exc:
                self.tts_failed.emit(f"Ошибка озвучки: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def _tts_reset_btn(self):
        self._tts_busy = False
        self.speak_btn.setText("Сказать")
        self.speak_btn.setEnabled(self.voice_combo.count() > 0)

    def _on_tts_done(self, a, text):
        self._tts_reset_btn()
        if not self.engine.running:
            return
        self.board.play_array(a)
        short = text if len(text) <= 60 else text[:57] + "…"
        self.player.set_track("Озвучка: " + short, array_bars(a))
        self.say_info.setText(self.SAY_HINT)

    def _on_tts_failed(self, msg):
        self._tts_reset_btn()
        self.say_info.setText(msg)

    def _tick(self):
        lin_in, lin_out = self.engine.take_levels()
        self.meter_in.set_level(lin_in)
        self.meter_out.set_level(lin_out)
        if self.engine.running:
            self.spectrum.set_samples(self.engine.scope, self.engine.sr)
            self.power_btn.update()
        else:
            self.spectrum.set_samples(None, 48000)

        # Обновление состояния плеера
        pos, total, is_paused, busy = self.board.get_progress()
        self.player.update_state(pos, total, self.engine.sr, is_paused, busy)

        self._tick_n += 1
        if self._tick_n % 8 == 0 and self.engine.running:
            e = self.engine
            if not e.healthy and not self._health_warned:
                self._health_warned = True
                self.power_btn.setChecked(False)
                QMessageBox.warning(self, "Аудио остановилось", "Аудиопоток прервался.")
                return
            txt = f"Задержка ≈ {e.latency_ms:.0f} мс  •  Щелчков: {e.xruns}"
            if e.error:
                txt += f"  •  Ошибка DSP: {e.error}"
            self.stats_label.setText(txt)

    def _load_settings(self):
        s = self.settings
        geom = s.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        for combo, key in ((self.in_combo, "in_dev"), (self.out_combo, "out_dev")):
            k = combo.findText(s.value(key, "", type=str))
            if k >= 0:
                combo.setCurrentIndex(k)
        self.sr_combo.setCurrentText(s.value("sr", "48000", type=str))
        self.block_combo.setCurrentText(s.value("block", "512", type=str))
        self.vol_slider.setValue(s.value("volume", 100, type=int))

        try:
            self.sound_paths = [p for p in json.loads(s.value("sounds", "[]", type=str)) if os.path.isfile(p)]
        except Exception:
            self.sound_paths = []
        self._load_hotkeys()

        try:
            params = sanitize_params(json.loads(s.value("params", "", type=str) or "{}"))
        except Exception:
            params = dict(DEFAULTS)
        self._set_widgets(params)
        name = s.value("preset", "", type=str)
        allp = self._all_presets()
        if name in allp and allp[name] == params:
            self.current_preset = name
        elif params == PRESETS["Без изменений"]:
            self.current_preset = "Без изменений"
        else:
            self.current_preset = None
        self._rebuild_cards(appear=True)
        self._rebuild_sound_cards(appear=False)
        self._apply_params_to_chain()

        k = self.lang_combo.findData(s.value("tts_lang", "ru", type=str))
        if k >= 0:
            self.lang_combo.setCurrentIndex(k)
        self._refresh_voices()
        self._select_voice(s.value("tts_voice", "", type=str))
        self.tts_speed.setValue(s.value("tts_speed", 100, type=int))
        self._tts_proxy = s.value("tts_proxy", "", type=str)

        self.monitor_check.setChecked(s.value("monitor", False, type=bool))
        self.awake_check.setChecked(s.value("keep_awake", False, type=bool))
        self.sr_combo.currentIndexChanged.connect(self._apply_awake)
        if s.value("virtual", False, type=bool) and VirtualMic.available():
            self.vm_check.setChecked(True)

    def _save_settings(self):
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("in_dev", self.in_combo.currentText())
        s.setValue("out_dev", self.out_combo.currentText())
        s.setValue("sr", self.sr_combo.currentText())
        s.setValue("block", self.block_combo.currentText())
        s.setValue("params", json.dumps(self._params_from_widgets()))
        s.setValue("preset", self.current_preset or "")
        s.setValue("virtual", self.vm_check.isChecked())
        s.setValue("monitor", self.monitor_check.isChecked())
        s.setValue("keep_awake", self.awake_check.isChecked())
        s.setValue("volume", self.vol_slider.value())
        s.setValue("sounds", json.dumps(self.sound_paths))
        s.setValue("hotkeys", json.dumps(self.hotkeys))
        s.setValue("sound_keys", json.dumps(self.sound_keys))
        vd = self.voice_combo.currentData()
        s.setValue("tts_voice", (vd[1] or "espeak") if vd else "")
        s.setValue("tts_lang", self.lang_combo.currentData())
        s.setValue("tts_speed", self.tts_speed.value())
        s.setValue("tts_proxy", self._tts_proxy)
        s.setValue("ui_theme", UI.theme)
        s.setValue("ui_gradient", UI.accent_name if UI.is_gradient else "")
        s.setValue("ui_accent", UI.accent_name if not UI.is_gradient else "")
        s.setValue("ui_anim", UI.anim)
        s.setValue("ui_custom_solid", UI.custom_solid)
        s.setValue("ui_custom_g1", UI.custom_g1)
        s.setValue("ui_custom_g2", UI.custom_g2)
        s.setValue("ui_custom_mode", "solid" if UI.accent_name == CUSTOM_SOLID
                   else "grad" if UI.accent_name == CUSTOM_GRAD else "")

    def closeEvent(self, event):
        try:
            if self.rec_btn.isChecked():
                self.rec_btn.setChecked(False)
            self._save_settings()
        finally:
            self.board.stop()
            self.engine.stop()
            self._awake.stop()
            if self._vm_active:
                VirtualMic.destroy()
            super().closeEvent(event)


# ===========================================================================
#  ТОЧКА ВХОДА
# ===========================================================================

def _find_icon():
    cands = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cands += [
            os.path.join(exe_dir, "..", "..", "voicechanger.png"),
            os.path.join(exe_dir, "..", "..", "icon.png"),
            os.path.join(exe_dir, "icon.png"),
        ]
    here = os.path.dirname(os.path.abspath(__file__))
    cands += [os.path.join(here, "icon.png"), os.path.join(here, "AppDir", "icon.png")]
    for p in cands:
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    app.setApplicationName("VoiceChanger")
    app.setOrganizationName("VoiceChanger")
    app.setStyle("Fusion")
    UI.load(QSettings("VoiceChanger", "VoiceChanger"))
    apply_ui_theme(app)
    icon = _find_icon()
    if icon:
        app.setWindowIcon(QIcon(icon))

    if not SD_OK:
        QMessageBox.critical(None, "Ошибка", f"Не удалось загрузить sounddevice:\n{SD_ERR}")
        return 1

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
