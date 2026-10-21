"""Biten maçların sonucuna göre seçimleri kazandı/kaybetti olarak işaretleme.

Aynı seçim tanımı (spec) olasılık motorunda kullanılan maskelerle değerlendirilir; bu yüzden
gerçek skoru tek noktaya yığılmış bir "model" ile hesaplamak yeterlidir.
"""
from __future__ import annotations

import numpy as np

from .markets import HALF_MAX, GoalModel, selection_prob


class PointGoals(GoalModel):
    def __init__(self, h1: int, a1: int, h2: int, a2: int):  # noqa: D401 (GoalModel.__init__ çağrılmaz)
        n = max(HALF_MAX, h1, a1, h2, a2) + 1
        J = np.zeros((n, n, n, n))
        J[h1, a1, h2, a2] = 1.0
        self.J = J
        H1, A1, H2, A2 = np.indices(J.shape)
        self.v = {"h1": H1, "a1": A1, "h2": H2, "a2": A2, "H": H1 + H2, "A": A1 + A2}
        self.lh = self.la = 1.0


class PointCount:
    def __init__(self, h: int, a: int, h_ht: int | None = None, a_ht: int | None = None):
        self.h, self.a, self.h_ht, self.a_ht = h, a, h_ht, a_ht

    def _vals(self, per):
        if per == "FT":
            return self.h, self.a
        if per == "H1" and self.h_ht is not None:
            return self.h_ht, self.a_ht
        raise ValueError("devre verisi yok")

    def p_over(self, line, scope="total", per="FT"):
        h, a = self._vals(per)
        v = h + a if scope == "total" else (h if scope == "home" else a)
        return 1.0 if v > line else 0.0

    def p_1x2(self, sel, per="FT"):
        h, a = self._vals(per)
        return 1.0 if {"1": h > a, "X": h == a, "2": h < a}[sel] else 0.0


NEEDS_HT = {"htft", "half_more", "win_both"}


def evaluate(spec: dict, ft: tuple[int, int], ht: tuple[int, int] | None,
             corners: tuple[int, int] | None = None, cards: tuple[int, int] | None = None) -> bool | None:
    """True=kazandı, False=kaybetti, None=bu veriyle belirlenemez (elle işaretlenir)."""
    subj = spec.get("subject", "goals")
    per = spec.get("period", "FT")
    try:
        if subj in ("corners", "cards"):
            data = corners if subj == "corners" else cards
            if data is None or per != "FT":
                return None
            p = selection_prob(spec, None, PointCount(*data) if subj == "corners" else None,
                               PointCount(*data) if subj == "cards" else None)
            return None if p is None else p > 0.5
        if spec["kind"] == "first_goal" and not (ft == (0, 0) or spec["sel"] == "none"):
            return None
        needs_ht = per != "FT" or spec["kind"] in NEEDS_HT or any(
            p.get("period", "FT") != "FT" for p in spec.get("parts", []))
        if needs_ht and ht is None:
            return None
        h1, a1 = ht if ht else (0, 0)
        h2, a2 = ft[0] - h1, ft[1] - a1
        if h2 < 0 or a2 < 0:
            return None
        p = selection_prob(spec, PointGoals(h1, a1, h2, a2), None, None)
        return None if p is None else p > 0.5
    except (ValueError, KeyError):
        return None
