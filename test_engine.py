"""
Sanity tests for the packing engine. Run with:  python test_engine.py
(no pytest required). Exits non-zero if anything fails.
"""

from __future__ import annotations

from engine import (
    BoxSpec,
    Settings,
    find_overlaps,
    min_support_seen,
    optimize_best_effort,
    optimize_minimum_height,
    optimize_minimum_pallets,
    optimize_pallets,
    orientations_3d,
)


def base_settings(**kw) -> Settings:
    defaults = dict(
        pallet_length=48.0,
        pallet_width=40.0,
        max_loaded_height=60.0,
        pallet_count=10,
        overhang=0.0,
        pallet_weight=40.0,
        max_total_pallet_weight=2000.0,
        enforce_support=True,
        min_support_fraction=0.80,
        heavy_low=True,
        random_restarts=4,
    )
    defaults.update(kw)
    return Settings(**defaults)


def test_orientations():
    free = BoxSpec("f", 3, 2, 1, 1, 1, allow_rotate=True, this_side_up=False)
    assert len(orientations_3d(free)) == 6, "free box should have 6 orientations"

    upright = BoxSpec("u", 3, 2, 1, 1, 1, allow_rotate=True, this_side_up=True)
    oris = orientations_3d(upright)
    assert all(h == 1 for _, _, h in oris), "upright box must keep its height"
    assert len(oris) == 2, "upright box should have 2 base rotations"

    fixed = BoxSpec("x", 3, 2, 1, 1, 1, allow_rotate=False)
    assert orientations_3d(fixed) == [(3, 2, 1)], "non-rotatable box has 1 orientation"
    print("ok  orientations")


def test_no_overlap_and_fits():
    specs = [
        BoxSpec("Small", 12, 10, 8, 40, 8),
        BoxSpec("Medium", 16, 12, 10, 24, 15),
        BoxSpec("Large", 20, 16, 12, 10, 30, this_side_up=True),
    ]
    placements, summary, remaining = optimize_pallets(specs, base_settings())
    assert sum(remaining.values()) == 0, f"expected all boxes packed, {sum(remaining.values())} left"
    overlaps = find_overlaps(placements)
    assert not overlaps, f"found {len(overlaps)} overlapping box pairs"
    print(f"ok  no-overlap & fits  (pallets used: {int((summary['boxes_packed'] > 0).sum())})")


def test_support_fraction_respected():
    specs = [
        BoxSpec("A", 12, 10, 8, 30, 8),
        BoxSpec("B", 16, 12, 10, 20, 15),
    ]
    settings = base_settings(min_support_fraction=0.80)
    placements, _, _ = optimize_pallets(specs, settings)
    worst = min_support_seen(placements, settings)
    assert worst >= 0.80 - 1e-6, f"min support {worst:.2f} below required 0.80"
    print(f"ok  support fraction  (worst seen: {worst * 100:.0f}%)")


def test_this_side_up_height():
    specs = [BoxSpec("Tall", 10, 10, 20, 12, 10, this_side_up=True)]
    settings = base_settings(max_loaded_height=60.0)
    placements, _, remaining = optimize_pallets(specs, settings)
    assert sum(remaining.values()) == 0
    assert all(abs(p.height - 20) < 1e-9 for p in placements), "upright box must keep height 20"
    print("ok  this-side-up keeps height")


def test_weight_cap():
    specs = [BoxSpec("Heavy", 12, 10, 8, 100, 50)]
    # cargo cap = 500 - 40 = 460 -> at 50 each, max 9 boxes per pallet.
    settings = base_settings(max_total_pallet_weight=500.0, pallet_count=1)
    placements, summary, _ = optimize_pallets(specs, settings)
    cargo = sum(p.weight for p in placements)
    assert cargo <= 460 + 1e-6, f"cargo {cargo} exceeds cap 460"
    print(f"ok  weight cap  (cargo packed: {cargo})")


def test_mixed_height_columns():
    # Boxes of different heights must stack into multiple levels that fill the
    # vertical space, rather than being forced into a single uniform layer.
    specs = [
        BoxSpec("Flat", 16, 12, 4, 40, 5),
        BoxSpec("Tall", 16, 12, 20, 8, 12),
    ]
    settings = base_settings(max_loaded_height=24)
    placements, summary, remaining = optimize_pallets(specs, settings)
    assert sum(remaining.values()) == 0, "expected everything to fit"

    p1 = [p for p in placements if p.pallet == 1]
    distinct_levels = {round(p.z, 3) for p in p1}
    assert len(distinct_levels) >= 3, f"expected multi-level stacking, got z-levels {sorted(distinct_levels)}"

    loaded_h = max(p.z + p.height for p in p1)
    assert loaded_h >= 0.9 * settings.max_loaded_height, (
        f"vertical space underused: loaded height {loaded_h} of {settings.max_loaded_height}"
    )
    print(f"ok  mixed-height columns  (levels: {len(distinct_levels)}, loaded height: {loaded_h:.0f})")


def test_tidy_pass_no_regression():
    # The tidy pass must keep every box, never make a pallet taller, and not
    # overlap; on a mixed load it should lower (or equal) the center of gravity.
    specs = [
        BoxSpec("Big", 22, 18, 14, 12, 40),
        BoxSpec("Mid", 16, 12, 10, 20, 18),
        BoxSpec("Small", 10, 9, 8, 30, 7),
    ]
    plain = base_settings(tidy_pass=False)
    tidy = base_settings(tidy_pass=True)

    pl_plain, sum_plain, rem_plain = optimize_pallets(specs, plain)
    pl_tidy, sum_tidy, rem_tidy = optimize_pallets(specs, tidy)

    assert sum(rem_tidy.values()) == sum(rem_plain.values()), "tidy changed how many boxes fit"
    assert len(pl_tidy) == len(pl_plain), "tidy changed packed box count"
    assert int((sum_tidy["boxes_packed"] > 0).sum()) == int((sum_plain["boxes_packed"] > 0).sum()), \
        "tidy changed pallet count"
    assert not find_overlaps(pl_tidy)
    # no pallet got taller
    for pallet in sorted({p.pallet for p in pl_tidy}):
        h_tidy = max(p.z + p.height for p in pl_tidy if p.pallet == pallet)
        h_plain = max((p.z + p.height for p in pl_plain if p.pallet == pallet), default=h_tidy)
        assert h_tidy <= h_plain + 1e-6, f"pallet {pallet} got taller: {h_tidy} > {h_plain}"
    cog_tidy = float(sum_tidy["cog_height"].mean())
    cog_plain = float(sum_plain["cog_height"].mean())
    assert cog_tidy <= cog_plain + 1e-6, f"tidy raised CoG: {cog_tidy} > {cog_plain}"
    print(f"ok  tidy pass no regression  (CoG {cog_plain:.1f} -> {cog_tidy:.1f}, no taller, same fit)")


def test_column_packing():
    # A pallet of one box type should pack as clean full-height columns and reach
    # (here) the theoretical maximum of 4 per layer x 6 layers = 24, fully supported.
    specs = [BoxSpec("RR2", 22, 19, 12, 200, 35)]
    settings = base_settings(max_loaded_height=72, max_total_pallet_weight=100000, pallet_count=1)
    placements, summary, remaining = optimize_pallets(specs, settings)
    p1 = [p for p in placements if p.pallet == 1]
    assert len(p1) == 24, f"expected 24 RR2 on the pallet, got {len(p1)}"
    assert not find_overlaps(p1)
    assert min_support_seen(p1, settings) >= 0.999, "columns must be fully supported"
    print(f"ok  column packing  ({len(p1)} boxes/pallet, cube {summary['cube_utilization'].iloc[0]:.2f})")


def test_minimize_height():
    # Minimizing height should use the same pallet count but a peak no taller
    # (usually shorter) than the plain densest packing.
    specs = [
        BoxSpec("A", 16, 12, 6, 36, 8),
        BoxSpec("B", 12, 10, 6, 36, 6),
    ]
    settings = base_settings(max_loaded_height=72)
    _pl, base_summary, _rem, base_K, base_fit = optimize_minimum_pallets(specs, settings, 20)
    base_peak = float(base_summary["loaded_height"].max())

    pl, summary, remaining, used, fit, peak = optimize_minimum_height(specs, settings, 20)
    assert fit and sum(remaining.values()) == 0, "everything should still fit"
    assert used == base_K, f"pallet count changed: {used} vs {base_K}"
    assert peak <= base_peak + 1e-6, f"min-height peak {peak} taller than baseline {base_peak}"
    assert not find_overlaps(pl)
    print(f"ok  minimize height  (peak {base_peak:.0f} -> {peak:.0f}, pallets={used})")


def test_best_effort():
    # Deep search (single-core in the test) must return a valid solution no worse
    # than the fast pass: everything fits, no overlaps, support respected.
    specs = [
        BoxSpec("Small", 12, 10, 8, 40, 8),
        BoxSpec("Medium", 16, 12, 10, 24, 15),
        BoxSpec("Large", 20, 16, 12, 10, 30, this_side_up=True),
    ]
    settings = base_settings(random_restarts=8, n_jobs=1)
    _fp, fast_summary, _fr, fast_used, _ff = optimize_minimum_pallets(specs, settings, 20)
    pl, summary, remaining, used, fits, rounds = optimize_best_effort(specs, settings, 3.0)
    assert fits and sum(remaining.values()) == 0
    assert used <= fast_used, f"deep used more pallets ({used}) than fast ({fast_used})"
    assert not find_overlaps(pl)
    assert min_support_seen(pl, settings) >= 0.80 - 1e-6
    assert rounds >= 1
    print(f"ok  best-effort search  (rounds={rounds}, pallets {fast_used} -> {used})")


def test_minimum_pallet_search():
    specs = [BoxSpec("Small", 12, 10, 8, 40, 8)]
    placements, summary, remaining, tried, found = optimize_minimum_pallets(specs, base_settings(), 20)
    assert found and sum(remaining.values()) == 0
    used = int((summary["boxes_packed"] > 0).sum())
    assert used == tried, "minimum search should return the pallet count it used"
    print(f"ok  minimum-pallet search  (used {used} pallet(s))")


def main():
    test_orientations()
    test_no_overlap_and_fits()
    test_support_fraction_respected()
    test_this_side_up_height()
    test_weight_cap()
    test_mixed_height_columns()
    test_column_packing()
    test_tidy_pass_no_regression()
    test_minimize_height()
    test_best_effort()
    test_minimum_pallet_search()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
