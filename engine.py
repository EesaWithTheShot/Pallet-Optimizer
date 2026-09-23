"""
Pallet packing engine (v5).

Pure-Python (+ numpy), no Streamlit dependency, so it can be unit tested alone.

Packing approach: a continuous **column / height-map (skyline) packer**.
Instead of forcing every box into rigid global layers of one height, each (x, y)
spot on the pallet grows to its own height independently. A box is placed at the
lowest empty corner where it rests on the surface below, so different regions of
the pallet build up to different heights — mixed box heights are exploited, and
voids are filled at their own levels. We pack each pallet many times with
different box orderings (multi-start) and keep the densest result.

Key features:
- Full 3D box rotation (up to 6 face orientations), with a per-box
  ``this_side_up`` flag that keeps a box upright.
- Minimum-support-area rule: a stacked box must rest on at least a configurable
  fraction of its base (it may span several boxes below) so it can't tip.
- Heavy-on-bottom biasing (prefers lower center of gravity among equally dense
  packings).
- Multi-start search across sort strategies + randomized restarts.

General 3D pallet packing is NP-hard; this is a strong practical heuristic, not a
proof of the global optimum. Units are agnostic (inches/lb or cm/kg, consistent).
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, asdict, replace
from itertools import permutations
from typing import Dict, List, Optional, Tuple
import hashlib
import random
import time

import numpy as np
import pandas as pd

EPS = 1e-9


# -----------------------------
# Colors (shared by 2D and 3D views)
# -----------------------------

PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
    "#86bcb6", "#d37295", "#fabfd2", "#b6992d", "#499894",
    "#79706e", "#d7b5a6", "#a0cbe8", "#f1ce63", "#8cd17d",
]


def color_for_name(name: str) -> str:
    """Deterministic, stable color for a box type name."""
    idx = int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16) % len(PALETTE)
    return PALETTE[idx]


# -----------------------------
# Data models
# -----------------------------

@dataclass(frozen=True)
class BoxSpec:
    name: str
    length: float
    width: float
    height: float
    quantity: int
    box_weight: float
    allow_rotate: bool = True
    this_side_up: bool = False

    @property
    def base_area(self) -> float:
        return self.length * self.width

    @property
    def volume(self) -> float:
        return self.length * self.width * self.height


@dataclass(frozen=True)
class Settings:
    pallet_length: float
    pallet_width: float
    max_loaded_height: float
    pallet_count: int
    overhang: float
    pallet_weight: float
    max_total_pallet_weight: float
    enforce_support: bool = True
    min_support_fraction: float = 0.80
    heavy_low: bool = True
    random_restarts: int = 8
    tidy_pass: bool = True
    n_jobs: int = 1  # CPU workers for the deeper/parallel search

    @property
    def usable_length(self) -> float:
        return self.pallet_length + 2 * self.overhang

    @property
    def usable_width(self) -> float:
        return self.pallet_width + 2 * self.overhang

    @property
    def max_cargo_weight(self) -> float:
        return max(0.0, self.max_total_pallet_weight - self.pallet_weight)


@dataclass
class Placement:
    pallet: int
    layer: int          # stacking level (1 = on the deck), used by the views
    box_name: str
    x: float
    y: float
    z: float
    length: float
    width: float
    height: float
    weight: float
    color: str = "#888888"


# -----------------------------
# Geometry helpers
# -----------------------------

def rect_intersection_area(
    ax: float, ay: float, al: float, aw: float,
    bx: float, by: float, bl: float, bw: float,
) -> float:
    ix = max(0.0, min(ax + al, bx + bl) - max(ax, bx))
    iy = max(0.0, min(ay + aw, by + bw) - max(ay, by))
    return ix * iy


def orientations_3d(box: BoxSpec) -> List[Tuple[float, float, float]]:
    """Distinct (length, width, height) orientations allowed for a box.

    - ``allow_rotate`` False -> single orientation as entered.
    - ``this_side_up`` True  -> 90 deg base rotation only (height fixed).
    - otherwise              -> up to 6 unique face orientations.
    """
    l, w, h = box.length, box.width, box.height
    if not box.allow_rotate:
        return [(l, w, h)]
    if box.this_side_up:
        return list({(l, w, h), (w, l, h)})
    return list({perm for perm in permutations((l, w, h))})


def sort_specs(
    specs: List[BoxSpec],
    remaining: Dict[str, int],
    strategy: str,
    rng: Optional[random.Random] = None,
) -> List[BoxSpec]:
    if strategy == "base_area_desc":
        key = lambda b: (-b.base_area, -b.height, b.name)
    elif strategy == "height_desc":
        key = lambda b: (-b.height, -b.base_area, b.name)
    elif strategy == "long_side_desc":
        key = lambda b: (-max(b.length, b.width), -b.base_area, b.name)
    elif strategy == "quantity_desc":
        key = lambda b: (-remaining.get(b.name, 0), -b.base_area, b.name)
    elif strategy == "weight_desc":
        key = lambda b: (-b.box_weight, -b.base_area, b.name)
    elif strategy == "volume_desc":
        key = lambda b: (-b.volume, -b.base_area, b.name)
    elif strategy == "footprint_weight_desc":
        key = lambda b: (-b.base_area * max(b.box_weight, 1e-6), -b.base_area, b.name)
    else:
        key = lambda b: (-b.base_area, -b.height, b.name)
    return sorted(specs, key=key)


# -----------------------------
# Column / height-map packer
# -----------------------------

# A placed box on a pallet, in internal (overhang-inclusive) coordinates.
PlacedTuple = Tuple[BoxSpec, float, float, float, float, float, float]  # box, x, y, z, l, w, h


def _expand_order(
    specs: List[BoxSpec],
    remaining: Dict[str, int],
    strategy: str,
    rng: Optional[random.Random],
) -> List[BoxSpec]:
    """Flatten remaining inventory into a per-instance attempt order."""
    types = [b for b in specs if remaining.get(b.name, 0) > 0]
    if strategy == "random" and rng is not None:
        seq: List[BoxSpec] = []
        for b in types:
            seq.extend([b] * remaining[b.name])
        rng.shuffle(seq)
        return seq
    ordered = sort_specs(types, remaining, strategy, rng)
    seq = []
    for b in ordered:
        seq.extend([b] * remaining[b.name])
    return seq


def _pack_once(
    specs: List[BoxSpec],
    remaining: Dict[str, int],
    settings: Settings,
    seq: List[BoxSpec],
) -> List[PlacedTuple]:
    """Greedily pack one pallet from ``remaining`` following attempt order ``seq``.

    Each box is placed at the lowest, most bottom-left corner where it fits and is
    sufficiently supported. Candidate corners are evaluated against all placed
    boxes at once with numpy broadcasting. Returns placed boxes (internal coords).
    """
    UL = settings.usable_length
    UW = settings.usable_width
    cap = settings.max_cargo_weight
    maxh = settings.max_loaded_height
    frac = settings.min_support_fraction if settings.enforce_support else 0.0

    placed: List[PlacedTuple] = []
    # numpy mirror of placed boxes: x, y, length, width, top(z+h)
    bx = by = bl = bw = btop = np.empty(0)

    def snapshot():
        nonlocal bx, by, bl, bw, btop
        if placed:
            arr = np.array([(p[1], p[2], p[4], p[5], p[3] + p[6]) for p in placed], dtype=float)
            bx, by, bl, bw, btop = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
        else:
            bx = by = bl = bw = btop = np.empty(0)

    cand_set = {(0.0, 0.0)}
    cand_x: List[float] = [0.0]
    cand_y: List[float] = [0.0]
    weight = 0.0
    rem = dict(remaining)
    # Identical box types fail identically while the surface is unchanged.
    failed_types: set = set()

    for box in seq:
        if rem[box.name] <= 0 or box.name in failed_types:
            continue
        if weight + box.box_weight > cap + EPS:
            continue

        cxs = np.array(cand_x)
        cys = np.array(cand_y)
        n = len(placed)
        best: Optional[Tuple[Tuple[float, float, float], float, float, float, float, float, float]] = None

        for (l, w, h) in orientations_3d(box):
            if l > UL + EPS or w > UW + EPS or h > maxh + EPS:
                continue
            area = l * w
            fits = (cxs + l <= UL + EPS) & (cys + w <= UW + EPS)
            if not fits.any():
                continue
            cx_v = cxs[fits]
            cy_v = cys[fits]

            if n == 0:
                z_v = np.zeros(cx_v.shape[0])
            else:
                # intersection area between each candidate footprint (rows) and
                # each placed box (cols): shape (C, N)
                iw = np.clip(np.minimum(cx_v[:, None] + l, bx + bl) - np.maximum(cx_v[:, None], bx), 0.0, None)
                ih = np.clip(np.minimum(cy_v[:, None] + w, by + bw) - np.maximum(cy_v[:, None], by), 0.0, None)
                ia = iw * ih
                hit = ia > 0
                z_v = np.where(hit, btop, 0.0).max(axis=1)  # rest on highest surface below
                # support fraction for stacked candidates
                sup = hit & (np.abs(btop - z_v[:, None]) <= 1e-6)
                sa = (ia * sup).sum(axis=1)
                support_ok = (z_v <= EPS) | (sa / area + EPS >= frac)
                ok = support_ok & (z_v + h <= maxh + EPS)
                if not ok.any():
                    continue
                cx_v, cy_v, z_v = cx_v[ok], cy_v[ok], z_v[ok]

            if cx_v.shape[0] == 0:
                continue
            # deepest-bottom-left: minimize z, then y, then x
            order = np.lexsort((cx_v, cy_v, z_v))
            i = order[0]
            score = (float(z_v[i]), float(cy_v[i]), float(cx_v[i]))
            if best is None or score < best[0]:
                best = (score, float(cx_v[i]), float(cy_v[i]), float(z_v[i]), l, w, h)

        if best is None:
            failed_types.add(box.name)
            continue

        _, cx, cy, z, l, w, h = best
        placed.append((box, cx, cy, z, l, w, h))
        snapshot()
        for c in ((cx, cy), (cx + l, cy), (cx, cy + w), (cx + l, cy + w)):
            key = (round(c[0], 6), round(c[1], 6))
            if key not in cand_set:
                cand_set.add(key)
                cand_x.append(c[0])
                cand_y.append(c[1])
        weight += box.box_weight
        rem[box.name] -= 1
        failed_types.clear()  # surface changed; previously-stuck types may fit now

    return placed


def _packing_stats(placed: List[PlacedTuple]) -> Tuple[float, float, float]:
    """Return (packed_volume, cargo_weight, cog_height) for a packing."""
    vol = 0.0
    wt = 0.0
    moment = 0.0
    for (box, _x, _y, z, l, w, h) in placed:
        vol += l * w * h
        wt += box.box_weight
        moment += box.box_weight * (z + h / 2)
    cog = moment / wt if wt > 0 else 0.0
    return vol, wt, cog


# -----------------------------
# Column / layer-replication packer (the "human" method)
# -----------------------------
# Tile a clean base layer of one box type, then stack each footprint into a
# full-height column. Leftover base area is tiled with the next box type. Columns
# of identical boxes are inherently fully supported and use the full height, which
# is where most of the density gain over greedy placement comes from.

# A free base rectangle is a plain tuple (x, y, length, width).

def _prune_free(rects):
    rs = [r for r in rects if r[2] > 1e-8 and r[3] > 1e-8]
    keep = []
    for i, r in enumerate(rs):
        dominated = False
        for j, o in enumerate(rs):
            if i == j:
                continue
            if (o[0] <= r[0] + EPS and o[1] <= r[1] + EPS
                    and o[0] + o[2] >= r[0] + r[2] - EPS and o[1] + o[3] >= r[1] + r[3] - EPS):
                area_o, area_r = o[2] * o[3], r[2] * r[3]
                if area_o > area_r + 1e-9 or (abs(area_o - area_r) <= 1e-9 and j < i):
                    dominated = True
                    break
        if not dominated:
            keep.append(r)
    return keep


def _split_free(free, px, py, pl, pw):
    out = []
    for (x, y, l, w) in free:
        if px >= x + l - EPS or px + pl <= x + EPS or py >= y + w - EPS or py + pw <= y + EPS:
            out.append((x, y, l, w))
            continue
        if px > x + EPS:
            out.append((x, y, px - x, w))
        if px + pl < x + l - EPS:
            out.append((px + pl, y, (x + l) - (px + pl), w))
        if py > y + EPS:
            out.append((x, y, l, py - y))
        if py + pw < y + w - EPS:
            out.append((x, py + pw, l, (y + w) - (py + pw)))
    return _prune_free(out)


def _grid_strip(x0, y0, sw, sh, a, b):
    """Best uniform grid (either orientation) filling an sw x sh strip."""
    best = []
    oris = [(a, b)] if abs(a - b) < 1e-9 else [(a, b), (b, a)]
    for (da, db) in oris:
        nx, ny = int((sw + EPS) // da), int((sh + EPS) // db)
        if nx * ny > len(best):
            best = [(x0 + i * da, y0 + j * db, da, db) for i in range(nx) for j in range(ny)]
    return best


def _greedy_rect(x0, y0, L, W, a, b):
    """Greedy MaxRects tiling within one rectangle (fallback / odd shapes)."""
    positions = []
    frees = [(x0, y0, L, W)]
    oris = [(a, b)] if abs(a - b) < 1e-9 else [(a, b), (b, a)]
    while True:
        best = None
        for (dl, dw) in oris:
            for (fx, fy, fl, fw) in frees:
                if dl <= fl + EPS and dw <= fw + EPS:
                    sc = (min(fl - dl, fw - dw), fl * fw - dl * dw, fy, fx)
                    if best is None or sc < best[0]:
                        best = (sc, fx, fy, dl, dw)
        if best is None:
            break
        _, fx, fy, dl, dw = best
        positions.append((fx, fy, dl, dw))
        frees = _split_free(frees, fx, fy, dl, dw)
    return positions


def _best_rect_tiling(rect, a, b):
    """Most boxes of footprint a x b (rotatable) fitting in one rectangle, using
    clean grid 'blocks' (a big grid plus strip fills) rather than greedy rotation,
    which is how a person tiles a pallet base. Falls back to greedy for odd cases."""
    x0, y0, L, W = rect
    best = []
    prim_oris = [(a, b)] if abs(a - b) < 1e-9 else [(a, b), (b, a)]
    for (pa, pb) in prim_oris:
        nx, ny = int((L + EPS) // pa), int((W + EPS) // pb)
        if nx == 0 or ny == 0:
            pos = []
        else:
            pos = [(x0 + i * pa, y0 + j * pb, pa, pb) for i in range(nx) for j in range(ny)]
            used_l, used_w = nx * pa, ny * pb
            pos += _grid_strip(x0 + used_l, y0, L - used_l, W, a, b)          # right strip (full height)
            pos += _grid_strip(x0, y0 + used_w, used_l, W - used_w, a, b)     # top strip (above grid)
        if len(pos) > len(best):
            best = pos
    greedy = _greedy_rect(x0, y0, L, W, a, b)
    if len(greedy) > len(best):
        best = greedy
    return best


def _tile_footprint(free, l, w):
    """Tile a footprint (l x w, rotatable) into the free rectangles using clean
    block tilings. Returns (positions, remaining_free)."""
    positions = []
    new_free = []
    for rect in free:
        pos = _best_rect_tiling(rect, l, w)
        positions += pos
        sub = [rect]
        for (x, y, dl, dw) in pos:
            nxt = []
            for r in sub:
                nxt += _split_free([r], x, y, dl, dw)
            sub = nxt
        new_free += sub
    return positions, _prune_free(new_free)


def _best_column_orientation(box: BoxSpec, settings: Settings):
    """Pick the orientation that maximizes columns x layers (rough volume)."""
    UL, UW, maxh = settings.usable_length, settings.usable_width, settings.max_loaded_height
    best = None
    best_val = -1.0
    for (l, w, h) in orientations_3d(box):
        if h > maxh + EPS or min(l, w) > max(UL, UW) + EPS:
            continue
        layers = int((maxh + EPS) // h)
        if layers < 1:
            continue
        grid = max(int((UL + EPS) // l) * int((UW + EPS) // w),
                   int((UL + EPS) // w) * int((UW + EPS) // l))
        val = grid * layers
        if val > best_val:
            best_val = val
            best = (l, w, h)
    return best


def _pack_columns(specs: List[BoxSpec], remaining: Dict[str, int], settings: Settings,
                  type_order: List[BoxSpec]) -> List[PlacedTuple]:
    UL, UW = settings.usable_length, settings.usable_width
    maxh, cap = settings.max_loaded_height, settings.max_cargo_weight
    rem = dict(remaining)
    placed: List[PlacedTuple] = []
    weight = 0.0
    free = [(0.0, 0.0, UL, UW)]

    for box in type_order:
        if rem[box.name] <= 0 or not free:
            continue
        chosen = _best_column_orientation(box, settings)
        if not chosen:
            continue
        l, w, h = chosen
        layers = int((maxh + EPS) // h)
        positions, free = _tile_footprint(free, l, w)
        for (x, y, dl, dw) in positions:
            if rem[box.name] <= 0:
                break
            colcnt = min(layers, rem[box.name])
            while colcnt > 0 and weight + colcnt * box.box_weight > cap + EPS:
                colcnt -= 1
            if colcnt <= 0:
                continue
            for k in range(colcnt):
                placed.append((box, x, y, k * h, dl, dw, h))
            rem[box.name] -= colcnt
            weight += colcnt * box.box_weight
    return placed


def _column_variants(specs: List[BoxSpec], remaining: Dict[str, int],
                     settings: Settings) -> List[List[PlacedTuple]]:
    active = [b for b in specs if remaining.get(b.name, 0) > 0]
    if not active:
        return []
    orders = [
        sorted(active, key=lambda b: -(remaining[b.name] * b.volume)),  # dominant by total volume
        sorted(active, key=lambda b: -b.base_area),                     # biggest footprint first
        sorted(active, key=lambda b: -b.volume),                        # biggest box first
    ]
    results = []
    seen = set()
    for order in orders:
        key = tuple(b.name for b in order)
        if key in seen:
            continue
        seen.add(key)
        results.append(_pack_columns(specs, remaining, settings, order))
    return results


_STRATEGIES = [
    "footprint_weight_desc",
    "base_area_desc",
    "volume_desc",
    "height_desc",
    "long_side_desc",
]


def _build_attempts(settings: Settings, pallet_index: int, seed_offset: int) -> List[Tuple[str, Optional[int]]]:
    """Attempt list: the deterministic strategies plus N seeded random shuffles."""
    attempts: List[Tuple[str, Optional[int]]] = [(s, None) for s in _STRATEGIES]
    base = 7000 + pallet_index * 100003 + seed_offset
    for i in range(max(0, settings.random_restarts)):
        attempts.append(("random", base + i))
    return attempts


def _score_placed(placed: List[PlacedTuple], settings: Settings) -> Tuple[float, float, int]:
    pallet_base = settings.usable_length * settings.usable_width
    vol, _wt, cog = _packing_stats(placed)
    if settings.heavy_low:
        # Group near-equal fills (to 0.1% of pallet footprint), then prefer a
        # lower center of gravity, then more boxes.
        return (round(vol / max(pallet_base, 1e-9), 3), -cog, len(placed))
    return (vol, float(len(placed)), 0)


def _pack_batch(args):
    """Evaluate a batch of attempts and return (best_score, best_placed).

    Module-level and picklable so it can run inside a ProcessPoolExecutor worker."""
    specs, remaining, settings, attempts = args
    best: List[PlacedTuple] = []
    best_score: Tuple[float, float, int] = (-1.0, -1.0, -1)
    for strategy, seed in attempts:
        rng = random.Random(seed) if seed is not None else None
        seq = _expand_order(specs, remaining, strategy, rng)
        placed = _pack_once(specs, remaining, settings, seq)
        if not placed:
            continue
        score = _score_placed(placed, settings)
        if score > best_score:
            best_score = score
            best = placed
    return best_score, best


def pack_pallet_best(
    specs: List[BoxSpec],
    remaining: Dict[str, int],
    settings: Settings,
    pallet_index: int,
    executor: Optional[ProcessPoolExecutor] = None,
    seed_offset: int = 0,
) -> List[PlacedTuple]:
    """Multi-start: pack the pallet many ways and keep the densest result.

    When an ``executor`` is supplied the attempts are split across CPU workers."""
    attempts = _build_attempts(settings, pallet_index, seed_offset)

    best: List[PlacedTuple] = []
    best_score: Tuple[float, float, int] = (-1.0, -1.0, -1)

    used_parallel = False
    if executor is not None and len(attempts) > 1:
        nj = max(2, settings.n_jobs)
        chunks = [attempts[i::nj] for i in range(nj)]
        chunks = [c for c in chunks if c]
        try:
            for sc, pl in executor.map(_pack_batch, [(specs, remaining, settings, c) for c in chunks]):
                if pl and sc > best_score:
                    best_score, best = sc, pl
            used_parallel = True
        except Exception:
            used_parallel = False  # fall back to serial below

    if not used_parallel:
        best_score, best = _pack_batch((specs, remaining, settings, attempts))

    # Column-replication candidates (human style: tile the base, stack each
    # footprint into a full-height column). Cheap and deterministic; kept only if
    # it packs more than the greedy skyline result, so it can't regress.
    for cand in _column_variants(specs, remaining, settings):
        if not cand:
            continue
        sc = _score_placed(cand, settings)
        if sc > best_score:
            best_score, best = sc, cand

    if not best:
        return best

    # Tidy pass: rearrange the SAME boxes into a more pyramidal / lower-CoG shape,
    # but only if it fits all of them at no greater height. Never regresses density.
    # Skip when the dense layout is already a clean pyramid (no big-on-small).
    if settings.tidy_pass and len(best) > 1 and _shape_score(best)[0] > 0.0:
        height_cap = max((z + h for (_b, _x, _y, z, _l, _w, h) in best), default=0.0)
        counts: Dict[str, int] = {}
        spec_by_name: Dict[str, BoxSpec] = {}
        for (box, *_rest) in best:
            counts[box.name] = counts.get(box.name, 0) + 1
            spec_by_name[box.name] = box
        tidy = _pack_tidy(
            list(spec_by_name.values()), counts, settings, height_cap, len(best)
        )
        if tidy is not None and _shape_score(tidy) < _shape_score(best):
            return tidy

    return best


def _inversion_area(placed: List[PlacedTuple]) -> float:
    """Penalty for 'big box on small box': sum of how much each stacked box's
    footprint exceeds the largest footprint directly supporting it. Lower = more
    pyramidal (wide bases, tapering up)."""
    if len(placed) < 2:
        return 0.0
    arr = [(x, y, z, l, w, h) for (_b, x, y, z, l, w, h) in placed]
    penalty = 0.0
    for i, (x, y, z, l, w, h) in enumerate(arr):
        if z <= 1e-6:
            continue
        my_area = l * w
        best_support = 0.0
        for j, (x2, y2, z2, l2, w2, h2) in enumerate(arr):
            if i == j or abs((z2 + h2) - z) > 1e-6:
                continue
            if rect_intersection_area(x, y, l, w, x2, y2, l2, w2) > 1e-9:
                best_support = max(best_support, l2 * w2)
        if best_support > 0.0 and my_area > best_support:
            penalty += my_area - best_support
    return penalty


def _shape_score(placed: List[PlacedTuple]) -> Tuple[float, float]:
    """Tidiness of an arrangement (lower is tidier): fewest 'big-on-small'
    inversions (the pyramid/no-big-box-on-small-box property), then lowest center
    of gravity. Used only to compare arrangements of the SAME set of boxes, so it
    never trades away packing efficiency."""
    _vol, _wt, cog = _packing_stats(placed)
    return (round(_inversion_area(placed), 4), round(cog, 4))


def _pack_tidy(
    specs_present: List[BoxSpec],
    counts: Dict[str, int],
    settings: Settings,
    height_cap: float,
    target_count: int,
) -> Optional[List[PlacedTuple]]:
    """Re-pack an already-decided set of boxes into the tidiest (most pyramidal,
    lowest-CoG) arrangement that still fits all of them within ``height_cap``.

    Tries several pyramid-biased orderings plus randomized restarts and keeps the
    best-shaped layout that places every box at no greater height. Returns None if
    none fit all the boxes within the cap (caller then keeps the dense layout)."""
    capped = replace(settings, max_loaded_height=height_cap)
    # Largest / heaviest first -> they settle on the deck, smaller boxes ride on top.
    orders: List[Tuple[str, Optional[random.Random]]] = [
        ("footprint_weight_desc", None),
        ("weight_desc", None),
        ("base_area_desc", None),
        ("long_side_desc", None),
        ("height_desc", None),
    ]
    for i in range(max(8, settings.random_restarts)):
        orders.append(("random", random.Random(31_000 + i)))

    best: Optional[List[PlacedTuple]] = None
    best_shape: Optional[Tuple[float, float]] = None
    for strat, rng in orders:
        seq = _expand_order(specs_present, counts, strat, rng)
        placed = _pack_once(specs_present, counts, capped, seq)
        if len(placed) != target_count:
            continue
        shape = _shape_score(placed)
        if best is None or shape < best_shape:
            best_shape = shape
            best = placed
            if shape[0] == 0.0:  # already a clean pyramid; can't beat zero inversions
                break
    return best


def _finalize(placed: List[PlacedTuple], settings: Settings, pallet_index: int) -> List[Placement]:
    """Convert internal placed tuples to Placements with view-friendly levels."""
    zs = sorted({round(z, 6) for (_b, _x, _y, z, _l, _w, _h) in placed})
    level_of = {z: i + 1 for i, z in enumerate(zs)}
    out: List[Placement] = []
    for (box, cx, cy, z, l, w, h) in placed:
        out.append(
            Placement(
                pallet=pallet_index,
                layer=level_of[round(z, 6)],
                box_name=box.name,
                x=cx - settings.overhang,
                y=cy - settings.overhang,
                z=z,
                length=l,
                width=w,
                height=h,
                weight=box.box_weight,
                color=color_for_name(box.name),
            )
        )
    return out


def optimize_pallets(
    specs: List[BoxSpec],
    settings: Settings,
    executor: Optional[ProcessPoolExecutor] = None,
    seed_offset: int = 0,
) -> Tuple[List[Placement], pd.DataFrame, Dict[str, int]]:
    """Fill pallets one at a time, packing each as densely as possible.

    If ``settings.n_jobs > 1`` and no ``executor`` is supplied, a temporary CPU
    worker pool is created for this call. ``seed_offset`` varies the random
    attempts between repeated solves (used by the best-effort search)."""
    remaining: Dict[str, int] = {b.name: int(b.quantity) for b in specs}
    all_placements: List[Placement] = []
    pallet_rows = []

    own_executor: Optional[ProcessPoolExecutor] = None
    if executor is None and settings.n_jobs > 1:
        try:
            own_executor = executor = ProcessPoolExecutor(max_workers=settings.n_jobs)
        except Exception:
            executor = None

    try:
        for pallet_index in range(1, settings.pallet_count + 1):
            if sum(remaining.values()) <= 0:
                break

            placed = pack_pallet_best(specs, remaining, settings, pallet_index,
                                      executor=executor, seed_offset=seed_offset)
            if not placed:
                # Nothing left fits on an empty pallet (e.g. too large / too heavy).
                break

            placements = _finalize(placed, settings, pallet_index)
            for p in placements:
                remaining[p.box_name] -= 1
            all_placements.extend(placements)

            cargo_weight = sum(p.weight for p in placements)
            cog_height = (
                sum(p.weight * (p.z + p.height / 2) for p in placements) / cargo_weight
                if cargo_weight > 0
                else 0.0
            )
            pallet_volume = settings.usable_length * settings.usable_width * max(
                (p.z + p.height for p in placements), default=1e-9
            )
            used_volume = sum(p.length * p.width * p.height for p in placements)
            pallet_rows.append(
                {
                    "pallet": pallet_index,
                    "boxes_packed": len(placements),
                    "cargo_weight": round(cargo_weight, 3),
                    "pallet_weight": settings.pallet_weight,
                    "total_weight": round(cargo_weight + settings.pallet_weight, 3),
                    "loaded_height": round(max((p.z + p.height for p in placements), default=0), 3),
                    "cube_utilization": round(used_volume / pallet_volume, 3) if pallet_volume > 0 else 0.0,
                    "cog_height": round(cog_height, 3),
                    "levels_used": max((p.layer for p in placements), default=0),
                    "footprint_length_allowed": settings.usable_length,
                    "footprint_width_allowed": settings.usable_width,
                }
            )
    finally:
        if own_executor is not None:
            own_executor.shutdown()

    pallet_summary = pd.DataFrame(pallet_rows)
    return all_placements, pallet_summary, remaining


def optimize_minimum_pallets(
    specs: List[BoxSpec],
    settings: Settings,
    max_pallets_to_try: int,
) -> Tuple[List[Placement], pd.DataFrame, Dict[str, int], int, bool]:
    """Densely fill pallets in sequence up to the limit.

    Because each pallet is packed as tightly as possible before opening the next,
    the number of pallets used is naturally minimized.
    """
    trial_settings = replace(settings, pallet_count=max_pallets_to_try)
    placements, summary, remaining = optimize_pallets(specs, trial_settings)
    used = int((summary["boxes_packed"] > 0).sum()) if not summary.empty else 0
    everything_fits = sum(remaining.values()) == 0
    return placements, summary, remaining, used, everything_fits


def _peak_height(summary: pd.DataFrame) -> float:
    if summary.empty:
        return 0.0
    return float(summary["loaded_height"].max())


def optimize_minimum_height(
    specs: List[BoxSpec],
    settings: Settings,
    max_pallets_to_try: int,
    height_resolution: float = 0.5,
) -> Tuple[List[Placement], pd.DataFrame, Dict[str, int], int, bool, float]:
    """Use the fewest pallets, then make those pallets as SHORT as possible.

    Pallets are typically billed by the inch of height, so once the load fits in
    the minimum number of pallets ``K``, we binary-search the smallest loaded
    height at which everything still fits in ``K`` pallets (a flatter, cheaper
    load). Returns the usual tuple plus the achieved peak height.
    """
    # 1) fewest pallets at the full allowed height.
    placements, summary, remaining, K, fit = optimize_minimum_pallets(specs, settings, max_pallets_to_try)
    if not fit or K <= 0:
        return placements, summary, remaining, K, fit, _peak_height(summary)

    best = (placements, summary, remaining, _peak_height(summary))

    # 2) binary-search the lowest height that still fits everything in K pallets.
    # A single box can never be shorter than its smallest dimension.
    lo = max((min(b.length, b.width, b.height) for b in specs), default=0.0)
    hi = _peak_height(summary)  # no point searching above what we already achieved
    search_restarts = min(settings.random_restarts, 4)

    for _ in range(24):
        if hi - lo <= height_resolution:
            break
        mid = (lo + hi) / 2.0
        trial = replace(settings, max_loaded_height=mid, pallet_count=K,
                        random_restarts=search_restarts, tidy_pass=False)
        pl, sm, rem = optimize_pallets(specs, trial)
        if sum(rem.values()) == 0 and not sm.empty:
            hi = mid
            best = (pl, sm, rem, _peak_height(sm))
        else:
            lo = mid

    # 3) polish: re-pack at the found height with the full restart budget.
    polish = replace(settings, max_loaded_height=max(hi, lo), pallet_count=K)
    pl, sm, rem = optimize_pallets(specs, polish)
    if sum(rem.values()) == 0 and not sm.empty and _peak_height(sm) <= best[3] + 1e-6:
        best = (pl, sm, rem, _peak_height(sm))

    placements, summary, remaining, peak = best
    used = int((summary["boxes_packed"] > 0).sum()) if not summary.empty else 0
    return placements, summary, remaining, used, True, peak


def _solution_quality(summary: pd.DataFrame, placements: List[Placement], remaining: Dict[str, int]):
    """Higher is better: fit everything, then fewest pallets, then most packed
    volume, then lowest total (billed) height."""
    fits = 1 if sum(remaining.values()) == 0 else 0
    used = int((summary["boxes_packed"] > 0).sum()) if not summary.empty else 0
    total_vol = sum(p.length * p.width * p.height for p in placements)
    total_h = float(summary["loaded_height"].sum()) if not summary.empty else 0.0
    return (fits, -used, round(total_vol, 3), -round(total_h, 3))


def optimize_best_effort(
    specs: List[BoxSpec],
    settings: Settings,
    total_seconds: float,
    progress=None,
) -> Tuple[List[Placement], pd.DataFrame, Dict[str, int], int, bool, int]:
    """Deep search: re-solve the whole problem repeatedly with a growing attempt
    budget until ``total_seconds`` elapses, keeping the best solution found.

    Parallelizes attempts across ``settings.n_jobs`` CPU workers (falls back to a
    single core if a worker pool can't be created). Returns the usual tuple plus
    the number of solver rounds completed."""
    deadline = time.time() + max(0.0, total_seconds)

    executor: Optional[ProcessPoolExecutor] = None
    if settings.n_jobs > 1:
        try:
            executor = ProcessPoolExecutor(max_workers=settings.n_jobs)
        except Exception:
            executor = None
    jobs = settings.n_jobs if executor is not None else 1

    best = None
    best_key = None
    rounds = 0
    restart = max(settings.random_restarts, 8)
    try:
        while True:
            trial = replace(settings, random_restarts=restart, n_jobs=jobs)
            pl, summ, rem = optimize_pallets(specs, trial, executor=executor, seed_offset=rounds * 1009 + 1)
            key = _solution_quality(summ, pl, rem)
            if best is None or key > best_key:
                best_key = key
                best = (pl, summ, rem)
            rounds += 1
            if progress is not None:
                try:
                    progress(rounds, time.time() - (deadline - total_seconds), best_key)
                except Exception:
                    pass
            if time.time() >= deadline:
                break
            restart = min(restart * 2, 4000)
    finally:
        if executor is not None:
            executor.shutdown()

    placements, summary, remaining = best
    used = int((summary["boxes_packed"] > 0).sum()) if not summary.empty else 0
    fits = sum(remaining.values()) == 0
    return placements, summary, remaining, used, fits, rounds


# -----------------------------
# Validation helpers (used by tests and the UI)
# -----------------------------

def boxes_overlap_3d(a: Placement, b: Placement, eps: float = 1e-6) -> bool:
    return (
        a.x < b.x + b.length - eps and b.x < a.x + a.length - eps
        and a.y < b.y + b.width - eps and b.y < a.y + a.width - eps
        and a.z < b.z + b.height - eps and b.z < a.z + a.height - eps
    )


def find_overlaps(placements: List[Placement]) -> List[Tuple[int, int]]:
    """Index pairs of boxes that overlap in 3D within the same pallet."""
    overlaps: List[Tuple[int, int]] = []
    by_pallet: Dict[int, List[int]] = {}
    for i, p in enumerate(placements):
        by_pallet.setdefault(p.pallet, []).append(i)
    for idxs in by_pallet.values():
        for ii in range(len(idxs)):
            for jj in range(ii + 1, len(idxs)):
                if boxes_overlap_3d(placements[idxs[ii]], placements[idxs[jj]]):
                    overlaps.append((idxs[ii], idxs[jj]))
    return overlaps


def min_support_seen(placements: List[Placement], settings: Settings) -> float:
    """Smallest support fraction across every off-the-deck box (1.0 if none)."""
    worst = 1.0
    by_pallet: Dict[int, List[Placement]] = {}
    for p in placements:
        by_pallet.setdefault(p.pallet, []).append(p)
    for pallet_ps in by_pallet.values():
        for p in pallet_ps:
            if p.z <= 1e-6:
                continue
            area = p.length * p.width
            supported = sum(
                rect_intersection_area(p.x, p.y, p.length, p.width, s.x, s.y, s.length, s.width)
                for s in pallet_ps
                if abs((s.z + s.height) - p.z) <= 1e-6
            )
            worst = min(worst, supported / area if area > 0 else 1.0)
    return worst


def placements_to_records(placements: List[Placement]) -> List[dict]:
    return [asdict(p) for p in placements]
