"""
Pallet Shipment Optimizer (v4)

A local Streamlit app for arranging rectangular boxes on one or more pallets.

New in v4 vs the original:
- Full 3D box rotation (6 orientations) with a per-box "this side up" lock.
- Minimum-support-area stacking rule to keep upper boxes from tipping/overhanging.
- Heavy-items-on-bottom biasing and center-of-gravity reporting.
- An orbitable Three.js 3D viewer (drag to rotate, scroll to zoom, hover for info).

Run:
    pip install -r requirements.txt
    streamlit run pallet_optimizer_app.py

The packer is a practical layer-based heuristic (3D bin packing is NP-hard),
not a proof of the globally optimal layout. Units are agnostic; stay consistent.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Dict, List
import csv
import io
import os

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle as MplRectangle
import pandas as pd
import streamlit as st

from engine import (
    BoxSpec,
    Placement,
    Settings,
    color_for_name,
    find_overlaps,
    min_support_seen,
    optimize_best_effort,
    optimize_minimum_height,
    optimize_minimum_pallets,
    optimize_pallets,
)
from viewer3d import build_viewer_html


# -----------------------------
# Box table helpers
# -----------------------------

BOX_COLUMNS = ["name", "length", "width", "height", "quantity", "box_weight", "pad", "allow_rotate", "this_side_up"]


def default_box_table() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"name": "Small", "length": 12.0, "width": 10.0, "height": 8.0,
             "quantity": 40, "box_weight": 8.0, "pad": 0.0, "allow_rotate": True, "this_side_up": False},
            {"name": "Medium", "length": 16.0, "width": 12.0, "height": 10.0,
             "quantity": 24, "box_weight": 15.0, "pad": 0.0, "allow_rotate": True, "this_side_up": False},
            {"name": "Large", "length": 20.0, "width": 16.0, "height": 12.0,
             "quantity": 10, "box_weight": 30.0, "pad": 0.0, "allow_rotate": True, "this_side_up": True},
        ],
        columns=BOX_COLUMNS,
    )


def normalize_box_table(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = out.drop(columns=[c for c in ["max_stack_layers"] if c in out.columns], errors="ignore")
    for col in BOX_COLUMNS:
        if col not in out.columns:
            if col == "allow_rotate":
                out[col] = True
            elif col == "this_side_up":
                out[col] = False
            elif col == "name":
                out[col] = ""
            else:
                out[col] = 0
    out = out[BOX_COLUMNS]
    return out


def _to_bool(val, default: bool) -> bool:
    if isinstance(val, str):
        return val.strip().lower() not in {"false", "f", "no", "n", "0", ""}
    try:
        if pd.isna(val):
            return default
    except (TypeError, ValueError):
        pass
    return bool(val)


def parse_pasted_table(raw_text: str) -> pd.DataFrame:
    # Drop blank lines and any stray indentation so ragged spacing parses cleanly.
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Paste at least one row of data first.")
    text = "\n".join(lines)

    # Pick a delimiter. Excel paste uses tabs; CSV uses commas/semicolons; a plain
    # typed table uses spaces -> treat any run of whitespace as the separator.
    if "\t" in text:
        sep, whitespace = "\t", False
    elif "," in text:
        sep, whitespace = ",", False
    elif ";" in text:
        sep, whitespace = ";", False
    else:
        sep, whitespace = r"\s+", True

    first_row = lines[0].split() if whitespace else next(csv.reader([lines[0]], delimiter=sep))
    first_row_normalized = [c.strip().lower() for c in first_row]
    has_header = any(
        c in BOX_COLUMNS or c in {"box weight", "weight", "rotate", "allow rotate", "this side up", "side up"}
        for c in first_row_normalized
    )

    df = pd.read_csv(io.StringIO(text), sep=sep, header=0 if has_header else None, engine="python")

    if not has_header:
        if df.shape[1] < 6:
            raise ValueError(
                "Pasted rows without headers need at least 6 columns: "
                "name, length, width, height, quantity, box_weight. "
                "(With space-separated paste, box names can't contain spaces.)"
            )
        df = df.iloc[:, : len(BOX_COLUMNS)]
        df.columns = BOX_COLUMNS[: df.shape[1]]

    rename_map = {}
    for c in df.columns:
        key = str(c).strip().lower().replace(" ", "_")
        if key in {"box", "box_name", "type", "box_type", "item", "sku"}:
            rename_map[c] = "name"
        elif key in {"l", "len"}:
            rename_map[c] = "length"
        elif key in {"w"}:
            rename_map[c] = "width"
        elif key in {"h"}:
            rename_map[c] = "height"
        elif key in {"qty", "count", "number"}:
            rename_map[c] = "quantity"
        elif key in {"weight", "unit_weight", "boxweight", "box_weight", "box_wt"}:
            rename_map[c] = "box_weight"
        elif key in {"rotate", "rotation", "allow_rotate", "allow_rotation", "can_rotate"}:
            rename_map[c] = "allow_rotate"
        elif key in {"this_side_up", "side_up", "upright", "keep_upright", "fragile"}:
            rename_map[c] = "this_side_up"
        elif key in {"pad", "bulge", "oversize", "tolerance", "padding", "swell"}:
            rename_map[c] = "pad"
        elif key in BOX_COLUMNS:
            rename_map[c] = key
    df = df.rename(columns=rename_map)
    return normalize_box_table(df)


def make_specs(df: pd.DataFrame, clearance: float = 0.0) -> List[BoxSpec]:
    """Build packing specs. Each dimension is inflated by ``clearance`` (global)
    plus the box's own ``pad`` (for overstuffed boxes) so the layout reserves the
    real occupied space. Weights and quantities are unchanged."""
    df = normalize_box_table(df)
    specs: List[BoxSpec] = []
    for _, row in df.iterrows():
        name = str(row.get("name", "")).strip()
        if not name or name.lower() == "nan":
            continue
        length = float(row["length"])
        width = float(row["width"])
        height = float(row["height"])
        quantity = int(row["quantity"])
        box_weight = float(row["box_weight"])
        pad = float(row.get("pad", 0.0) or 0.0)
        allow_rotate = _to_bool(row.get("allow_rotate", True), True)
        this_side_up = _to_bool(row.get("this_side_up", False), False)
        if min(length, width, height) <= 0:
            raise ValueError(f"Box '{name}' has a non-positive dimension.")
        if quantity < 0:
            raise ValueError(f"Box '{name}' has a negative quantity.")
        if box_weight < 0:
            raise ValueError(f"Box '{name}' has a negative weight.")
        if pad < 0:
            raise ValueError(f"Box '{name}' has a negative pad.")
        grow = clearance + pad
        specs.append(BoxSpec(
            name, length + grow, width + grow, height + grow,
            quantity, box_weight, allow_rotate, this_side_up,
        ))

    if len({b.name for b in specs}) != len(specs):
        raise ValueError("Each box type must have a unique name.")
    return specs


def placements_to_df(placements: List[Placement]) -> pd.DataFrame:
    if not placements:
        return pd.DataFrame(columns=list(asdict(Placement(0, 0, "", 0, 0, 0, 0, 0, 0, 0)).keys()))
    return pd.DataFrame([asdict(p) for p in placements])


def box_counts_summary(specs: List[BoxSpec], remaining: Dict[str, int]) -> pd.DataFrame:
    rows = []
    for b in specs:
        left = remaining.get(b.name, 0)
        packed = b.quantity - left
        rows.append(
            {
                "box_name": b.name,
                "requested": b.quantity,
                "packed": packed,
                "left_unpacked": left,
                "unit_weight": b.box_weight,
                "packed_weight": round(packed * b.box_weight, 3),
            }
        )
    return pd.DataFrame(rows)


# -----------------------------
# 2D matplotlib views
# -----------------------------

def draw_pallet_top_view(placements_df, settings, pallet_no, layer_no, unit_label):
    layer_df = placements_df[(placements_df["pallet"] == pallet_no) & (placements_df["layer"] == layer_no)]
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.add_patch(MplRectangle((0, 0), settings.pallet_length, settings.pallet_width,
                              fill=False, linewidth=2, label="Pallet"))
    ax.add_patch(MplRectangle((-settings.overhang, -settings.overhang),
                              settings.usable_length, settings.usable_width,
                              fill=False, linestyle="--", linewidth=1.5,
                              label="Allowed footprint incl. overhang"))
    for _, p in layer_df.iterrows():
        ax.add_patch(MplRectangle((p["x"], p["y"]), p["length"], p["width"],
                                  alpha=0.55, edgecolor="black",
                                  facecolor=color_for_name(p["box_name"]), linewidth=0.8))
        ax.text(p["x"] + p["length"] / 2, p["y"] + p["width"] / 2, str(p["box_name"]),
                ha="center", va="center", fontsize=8)
    ax.set_title(f"Pallet {pallet_no} — Layer {layer_no} top view")
    ax.set_xlabel(f"Length ({unit_label})")
    ax.set_ylabel(f"Width ({unit_label})")
    ax.set_aspect("equal", adjustable="box")
    margin = max(settings.overhang, 2)
    ax.set_xlim(-margin, settings.pallet_length + margin)
    ax.set_ylim(-margin, settings.pallet_width + margin)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right")
    return fig


def draw_pallet_side_view(placements_df, pallet_no, unit_label):
    pallet_df = placements_df[placements_df["pallet"] == pallet_no]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    if pallet_df.empty:
        ax.set_title(f"Pallet {pallet_no} side view")
        return fig
    for _, p in pallet_df.iterrows():
        ax.add_patch(MplRectangle((p["x"], p["z"]), p["length"], p["height"],
                                  alpha=0.45, edgecolor="black",
                                  facecolor=color_for_name(p["box_name"]), linewidth=0.5))
    ax.set_title(f"Pallet {pallet_no} — side/height view")
    ax.set_xlabel(f"Length ({unit_label})")
    ax.set_ylabel(f"Height ({unit_label})")
    ax.autoscale_view()
    ax.grid(True, alpha=0.25)
    return fig


# -----------------------------
# Results storage + rendering
# -----------------------------

def save_results_to_state(specs, settings, placements, pallet_summary, remaining,
                          search_mode, minimum_found, max_pallets_tried):
    st.session_state.results = {
        "specs": specs,
        "settings": settings,
        "placements": placements,
        "placements_df": placements_to_df(placements),
        "pallet_summary": pallet_summary,
        "remaining": remaining,
        "counts_df": box_counts_summary(specs, remaining),
        "search_mode": search_mode,
        "minimum_found": minimum_found,
        "max_pallets_tried": max_pallets_tried,
    }


def render_results(unit_label: str, weight_label: str) -> None:
    if "results" not in st.session_state:
        st.info("Set up your boxes and pallet, then click **Optimize pallet layout**.")
        return

    r = st.session_state.results
    specs: List[BoxSpec] = r["specs"]
    settings: Settings = r["settings"]
    placements: List[Placement] = r["placements"]
    placements_df: pd.DataFrame = r["placements_df"]
    pallet_summary: pd.DataFrame = r["pallet_summary"]
    remaining: Dict[str, int] = r["remaining"]
    counts_df: pd.DataFrame = r["counts_df"]

    total_requested = sum(b.quantity for b in specs)
    total_packed = int(len(placements_df))
    total_left = int(sum(remaining.values()))
    cargo_weight = float(placements_df["weight"].sum()) if not placements_df.empty else 0.0
    pallet_count_used = int(pallet_summary[pallet_summary["boxes_packed"] > 0].shape[0]) if not pallet_summary.empty else 0
    total_shipment_weight = cargo_weight + pallet_count_used * settings.pallet_weight

    st.subheader("Optimization results")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Boxes packed", f"{total_packed} / {total_requested}")
    c2.metric("Boxes left", f"{total_left}")
    if r["search_mode"] == "Minimize pallets used":
        c3.metric("Pallets used", f"{pallet_count_used}")
    else:
        c3.metric("Pallets used", f"{pallet_count_used} / {settings.pallet_count}")
    c4.metric("Total shipment weight", f"{total_shipment_weight:,.2f} {weight_label}")

    if total_left > 0:
        st.warning(
            f"{total_left} boxes were left unpacked. The limiting factor may be max height, "
            "footprint, overhang, weight capacity, support requirement, or the pallet search limit."
        )
    elif r["search_mode"] == "Minimize pallets used" and r["minimum_found"]:
        st.success(f"All boxes fit. Minimum pallet count found: {pallet_count_used}.")
    else:
        st.success("All boxes were packed within the current constraints.")

    # Stability + height readout.
    if placements:
        worst_support = min_support_seen(placements, settings)
        overlaps = find_overlaps(placements)
        tallest = float(pallet_summary["loaded_height"].max()) if not pallet_summary.empty else 0.0
        s1, s2, s3 = st.columns(3)
        s1.metric("Tallest pallet (billed height)", f"{tallest:.1f} {unit_label}")
        s2.metric("Min support area (stacked boxes)", f"{worst_support * 100:.0f}%")
        s3.metric("Overlapping boxes", f"{len(overlaps)}")

    tab_3d, tab_2d, tab_tables = st.tabs(["🧊 3D view", "📐 2D layers", "📋 Tables & export"])

    pallet_options = sorted(placements_df["pallet"].unique().tolist()) if not placements_df.empty else []

    with tab_3d:
        if not pallet_options:
            st.info("No boxes were placed.")
        else:
            sel = st.selectbox("Pallet", pallet_options, key="viz3d_pallet")
            pallet_placements = [p for p in placements if p.pallet == sel]
            html = build_viewer_html(
                pallet_placements,
                pallet_length=settings.pallet_length,
                pallet_width=settings.pallet_width,
                overhang=settings.overhang,
                unit_label=unit_label,
                height_px=640,
                max_height=settings.max_loaded_height,
            )
            # Rendered in a sandboxed iframe so the viewer's JavaScript (Three.js) runs.
            # st.iframe treats a non-URL/non-path string as raw HTML and embeds it.
            st.iframe(html, height=660)
            st.caption("Drag to orbit · right-drag to pan · scroll to zoom · hover a box for details. "
                       "Use the on-canvas slider to peel back layers.")

    with tab_2d:
        if not pallet_options:
            st.info("No boxes were placed.")
        else:
            vc = st.columns(2)
            sel_p = int(vc[0].selectbox("Pallet", pallet_options, key="viz2d_pallet"))
            layer_options = sorted(
                placements_df[placements_df["pallet"] == sel_p]["layer"].unique().tolist()
            )
            sel_l = int(vc[1].selectbox("Layer", layer_options, key=f"viz2d_layer_{sel_p}"))
            top_fig = draw_pallet_top_view(placements_df, settings, sel_p, sel_l, unit_label)
            st.pyplot(top_fig)
            plt.close(top_fig)
            side_fig = draw_pallet_side_view(placements_df, sel_p, unit_label)
            st.pyplot(side_fig)
            plt.close(side_fig)

    with tab_tables:
        st.markdown("**Pallet summary**")
        st.dataframe(pallet_summary, width="stretch")
        st.markdown("**Box count summary**")
        st.dataframe(counts_df, width="stretch")
        st.markdown("**Placement details**")
        st.dataframe(placements_df, width="stretch")
        if not placements_df.empty:
            st.download_button(
                "Download placement CSV",
                data=placements_df.to_csv(index=False).encode("utf-8"),
                file_name="pallet_placements.csv",
                mime="text/csv",
            )
            st.download_button(
                "Download pallet summary CSV",
                data=pallet_summary.to_csv(index=False).encode("utf-8"),
                file_name="pallet_summary.csv",
                mime="text/csv",
            )

    with st.expander("Assumptions and limitations"):
        st.markdown(
            """
- Boxes are rectangular and placed axis-aligned.
- `allow_rotate` permits rotation; `this_side_up` keeps a box upright (90° base rotation only).
  With rotation allowed and not upright-locked, all 6 face orientations are considered.
- The optimizer uses **column/skyline packing**: each spot on the pallet builds up to its own
  height, so different regions can be different heights and mixed box heights are used to fill
  voids — no wasted air above short boxes.
- Each pallet is packed many ways (multiple orderings + randomized restarts) and the **densest
  result is kept**, then the next pallet is filled. `cube_utilization` shows how full each pallet is.
- **Minimum support area**: each stacked box must rest on at least the chosen fraction of its
  base (it may span several boxes below), so boxes don't float or hang off edges.
- **Heavy items on bottom**: among equally dense packings, the one with the lowest center of
  gravity is chosen. `cog_height` in the pallet summary is that height — lower is more stable.
- **Tidy stacks**: after packing, the same boxes on each pallet are rearranged into a more
  pyramidal, lower-CoG shape (big/heavy low, tapering up). It is only kept if it fits every box at
  no greater height, so it never reduces packing efficiency — worst case it reverts to the original.
- **Box clearance / pad**: dimensions are inflated by the global clearance plus each box's `pad`
  before packing, so overstuffed/bulging boxes reserve the space they really take. Displayed box
  sizes include that padding.
- **Minimize stack height**: optional. After finding the fewest pallets, it searches for the lowest
  height that still fits everything in that many pallets — flatter load, fewer billed inches.
  (Tilting boxes does *not* reduce billed height: a box is shortest lying flat, which rotation
  already does, and tilting only makes its vertical extent larger.)
- General pallet optimization is NP-hard; this is a strong heuristic, not a proven optimum.
            """
        )


# -----------------------------
# Main app
# -----------------------------

def streamlit_app() -> None:
    st.set_page_config(page_title="Pallet Shipment Optimizer", layout="wide")
    st.title("📦 Pallet Shipment Optimizer")
    st.caption(
        "Arrange boxes across pallets with full 3D rotation, support-aware stable stacking, "
        "minimum-pallet search, and an orbitable 3D view."
    )

    if "box_table" not in st.session_state:
        st.session_state.box_table = default_box_table()

    with st.sidebar:
        st.header("Pallet settings")
        unit_label = st.text_input("Dimension unit label", value="in")
        weight_label = st.text_input("Weight unit label", value="lb")
        pallet_length = st.number_input("Pallet length", min_value=0.01, value=48.0, step=1.0)
        pallet_width = st.number_input("Pallet width", min_value=0.01, value=40.0, step=1.0)

        search_mode = st.radio(
            "Optimization goal",
            options=["Minimize pallets used", "Use a fixed number of pallets"],
            index=0,
            help="Minimum mode starts at 1 pallet and increases until every box fits.",
        )
        minimize_height = False
        if search_mode == "Minimize pallets used":
            pallet_count = st.number_input("Maximum pallets to try", min_value=1, value=20, step=1)
            minimize_height = st.checkbox(
                "Then minimize stack height (reduce billed inches)", value=False,
                help="After finding the fewest pallets, searches for the lowest height that still "
                     "fits everything in that many pallets. Flatter load, lower billed height. Slower.",
            )
        else:
            pallet_count = st.number_input("Number of pallets available", min_value=1, value=1, step=1)

        max_loaded_height = st.number_input("Max loaded height (above deck)", min_value=0.01, value=60.0, step=1.0)
        overhang = st.number_input("Allowed overhang on each side", min_value=0.0, value=0.0, step=0.25)
        clearance = st.number_input(
            "Box clearance / padding (added to each box dimension)", min_value=0.0, value=0.0, step=0.25,
            help="Global safety gap added to every box's L/W/H so the plan reserves real occupied "
                 "space. Use the per-box 'pad' column for boxes that get stuffed fuller than others.",
        )

        st.header("Weight settings")
        pallet_weight = st.number_input("Empty pallet weight", min_value=0.0, value=40.0, step=1.0)
        max_total_pallet_weight = st.number_input(
            "Max total pallet weight incl. pallet", min_value=0.01, value=2000.0, step=25.0
        )

        st.header("Stacking & stability")
        enforce_support = st.checkbox(
            "Enforce support for stacked boxes", value=True,
            help="Upper boxes must rest on the boxes below by at least the support fraction.",
        )
        min_support_pct = st.slider(
            "Minimum support area %", min_value=10, max_value=100, value=80, step=5,
            help="How much of a box's base must sit on boxes below. Higher = more stable, may use more pallets.",
        )
        heavy_low = st.checkbox(
            "Heavy items on bottom", value=True,
            help="Among equally dense packings, prefers the one with a lower center of gravity.",
        )
        tidy_pass = st.checkbox(
            "Tidy stacks (pyramid / lower CoG)", value=True,
            help="After packing, rearranges the same boxes on each pallet into a more pyramidal, "
                 "lower-center-of-gravity shape. Only applied if it fits everything at no greater "
                 "height, so it never costs efficiency.",
        )

        st.header("Packing behavior")
        st.caption(
            "Boxes build in independent columns, so different parts of the pallet can "
            "rise to different heights (mixed heights are used automatically)."
        )
        cpu_cores = os.cpu_count() or 1
        effort = st.selectbox(
            "Optimization effort",
            ["Fast (seconds)", "Thorough (more attempts)", f"Maximum (deep, uses {cpu_cores} CPU cores)"],
            index=0,
            help="Fast: a quick heuristic. Thorough: many more attempts across all CPU cores. "
                 "Maximum: keeps searching for a set time budget and parallelizes across all cores "
                 "to squeeze out every bit of space.",
        )
        if effort.startswith("Maximum"):
            deep_minutes = st.number_input(
                "Time budget (minutes)", min_value=0.5, value=2.0, step=0.5,
                help="The deep search runs for about this long, then returns the best layout found.",
            )
            random_restarts, n_jobs, deep = 24, cpu_cores, True
        elif effort.startswith("Thorough"):
            deep_minutes = 0.0
            random_restarts, n_jobs, deep = 64, cpu_cores, False
        else:
            deep_minutes = 0.0
            random_restarts, n_jobs, deep = 12, 1, False

    settings = Settings(
        pallet_length=pallet_length,
        pallet_width=pallet_width,
        max_loaded_height=max_loaded_height,
        pallet_count=int(pallet_count),
        overhang=overhang,
        pallet_weight=pallet_weight,
        max_total_pallet_weight=max_total_pallet_weight,
        enforce_support=enforce_support,
        min_support_fraction=min_support_pct / 100.0,
        heavy_low=heavy_low,
        random_restarts=random_restarts,
        tidy_pass=tidy_pass,
        n_jobs=n_jobs,
    )

    st.subheader("Box types")
    st.write(
        "Edit the table directly, or paste rows from Excel/Google Sheets. Required columns: "
        "`name`, `length`, `width`, `height`, `quantity`, `box_weight`. "
        "Optional: `pad` (extra size for overstuffed boxes), `allow_rotate`, `this_side_up`."
    )

    with st.expander("Paste from Excel / CSV"):
        st.caption(
            "Values can be separated by spaces, tabs (Excel), or commas. Paste with or "
            "without headers. Without headers, use this order: "
            "name, length, width, height, quantity, box_weight, pad, allow_rotate, this_side_up. "
            "(With spaces, box names can't contain spaces.)"
        )
        pasted = st.text_area(
            "Paste table here", height=160,
            placeholder="name\tlength\twidth\theight\tquantity\tbox_weight\n"
                        "Small\t12\t10\t8\t40\t8",
            key="paste_box_table_text",
        )
        if st.columns([1, 4])[0].button("Load pasted table"):
            try:
                st.session_state.box_table = parse_pasted_table(pasted)
                st.session_state.pop("box_editor", None)
                st.success("Pasted table loaded. Review it below, then optimize.")
            except Exception as exc:
                st.error(str(exc))

    run = False
    apply_table = False
    with st.form("box_table_form", clear_on_submit=False):
        edited_df = st.data_editor(
            normalize_box_table(st.session_state.box_table),
            num_rows="dynamic",
            width="stretch",
            key="box_editor",
            column_config={
                "name": st.column_config.TextColumn("name", required=True),
                "length": st.column_config.NumberColumn("length", min_value=0.01, format="%.3f"),
                "width": st.column_config.NumberColumn("width", min_value=0.01, format="%.3f"),
                "height": st.column_config.NumberColumn("height", min_value=0.01, format="%.3f"),
                "quantity": st.column_config.NumberColumn("quantity", min_value=0, step=1),
                "box_weight": st.column_config.NumberColumn("box_weight", min_value=0.0, format="%.3f"),
                "pad": st.column_config.NumberColumn(
                    "pad", min_value=0.0, format="%.2f",
                    help="Extra size added to each dimension for this box (e.g. when stuffed full).",
                ),
                "allow_rotate": st.column_config.CheckboxColumn("allow_rotate"),
                "this_side_up": st.column_config.CheckboxColumn(
                    "this_side_up", help="Keep upright: only 90° base rotation, never tipped onto a side."
                ),
            },
        )
        button_cols = st.columns([1, 1, 4])
        apply_table = button_cols[0].form_submit_button("Apply table changes")
        run = button_cols[1].form_submit_button("Optimize pallet layout", type="primary")

    if apply_table or run:
        st.session_state.box_table = normalize_box_table(edited_df)
        if apply_table and not run:
            st.success("Table changes applied.")

    if run:
        try:
            specs = make_specs(st.session_state.box_table, clearance=clearance)
            if not specs:
                st.error("Add at least one valid box type.")
                return
            if deep:
                spin = f"Deep optimization running for ~{deep_minutes:g} min across {n_jobs} cores…"
            elif minimize_height:
                spin = "Optimizing (then minimizing height)…"
            else:
                spin = "Optimizing pallet layout…"
            status = st.empty()

            def _progress(rounds, elapsed, key):
                status.info(f"Deep search… round {rounds} · {elapsed:.0f}s elapsed · "
                            f"best so far: {-key[1]} pallets, {'all fit' if key[0] else 'partial'}")

            with st.spinner(spin):
                if search_mode == "Minimize pallets used":
                    if minimize_height:
                        placements, pallet_summary, remaining, pallets_tried, minimum_found, _peak = (
                            optimize_minimum_height(specs, settings, int(pallet_count))
                        )
                    elif deep:
                        placements, pallet_summary, remaining, pallets_tried, minimum_found, _rounds = (
                            optimize_best_effort(specs, settings, deep_minutes * 60.0, progress=_progress)
                        )
                    else:
                        placements, pallet_summary, remaining, pallets_tried, minimum_found = (
                            optimize_minimum_pallets(specs, settings, int(pallet_count))
                        )
                    result_settings = replace(settings, pallet_count=max(pallets_tried, 1))
                    save_results_to_state(specs, result_settings, placements, pallet_summary,
                                          remaining, search_mode, minimum_found, int(pallet_count))
                else:
                    if deep:
                        placements, pallet_summary, remaining, _used, _fit, _rounds = (
                            optimize_best_effort(specs, settings, deep_minutes * 60.0, progress=_progress)
                        )
                    else:
                        placements, pallet_summary, remaining = optimize_pallets(specs, settings)
                    save_results_to_state(specs, settings, placements, pallet_summary, remaining,
                                          search_mode, sum(remaining.values()) == 0, int(pallet_count))
            status.empty()
        except Exception as exc:
            st.exception(exc)

    render_results(unit_label, weight_label)


if __name__ == "__main__":
    streamlit_app()
