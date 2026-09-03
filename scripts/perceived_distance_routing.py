#!/usr/bin/env python
"""
Perceived-distance bicycle routing for the NYC heat-exposure pipeline.

WHAT THIS IS
------------
A drop-in replacement for the Valhalla routing stage in
`notebooks/valhalla_routing_check.ipynb`. Instead of routing on physical
distance via a local Valhalla service, this builds the street network as a
directed graph and routes on PERCEIVED distance:

    perceived_length = physical_length * factor(segment)

where `factor` depends on the cycling comfort of the segment (separated lane,
painted lane, mixed traffic, etc.). This is the standard formulation in the
route-choice literature.

It writes the SAME output file the downstream notebooks already expect:

    verified_station_pair_routes_fixed.gpkg
        layer  = "routes_with_correct_ids"
        cols   = orig_start, orig_end, geometry
        crs    = EPSG:32118

so `verified_hotspot_analysis.ipynb` and `trees_analysis.ipynb` need no changes.
Run it once per factor set, write to a different output path each time, and the
whole downstream exposure/tree analysis can be re-run per scenario.

STATUS
------
UNVALIDATED. Written against the schema of the existing notebooks, but not yet
run end-to-end because this repo ships no data and no `libs/` module. Expect to
fix column names on first contact with the real shapefile -- see CHECK ME notes.

Usage
-----
    python perceived_distance_routing.py \
        --streets  verified_streets_neat_bidirectional.shp \
        --stations station_coords.csv \
        --pairs    station_pairs.csv \
        --bike-lanes nyc_bike_routes.geojson \
        --out      routes_perceived_baseline.gpkg

    # robustness sweep: same call, different factors
    python perceived_distance_routing.py ... \
        --factor protected=1.0 --factor painted=1.15 --factor mixed=1.6 \
        --out routes_perceived_low.gpkg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import LineString

# --------------------------------------------------------------------------
# Perceived-distance multipliers
# --------------------------------------------------------------------------
# PLACEHOLDER VALUES. Replace with the values from the paper Schlaepfer sent
# before reporting any result. They are deliberately kept in one dict so the
# robustness sweep is a one-line change.
#
# Interpretation: a cyclist treats 1 m on this segment as if it were
# FACTOR metres of effort/discomfort. Protected lane is the reference (1.0).
DEFAULT_FACTORS = {
    "protected": 1.00,   # physically separated cycle track
    "painted":   1.25,   # painted lane, no physical separation
    "sharrow":   1.50,   # shared-lane marking only
    "mixed":     2.00,   # no cycling infrastructure, mixed traffic
    "offstreet": 0.90,   # greenway / park path, more pleasant than a lane
}

# Fallback when a segment has no bike-lane match: assume the worst realistic
# case for the road class rather than silently treating it as protected.
DEFAULT_CLASS = "mixed"

# Road classes that should never be routed over by a bicycle at all.
EXCLUDED_HIGHWAY = {
    "motorway", "motorway_link", "trunk", "trunk_link",
    "proposed", "construction", "raceway",
}

# Coordinate rounding (metres) used to weld segment endpoints into shared
# graph nodes. The neat network is already topologically cleaned, so this
# should only absorb float noise. Raise it if the graph comes out disconnected.
NODE_PRECISION = 0.5

ANALYSIS_CRS = 32118  # NAD83 / New York Long Island (metres) -- matches pipeline


# --------------------------------------------------------------------------
# Network construction
# --------------------------------------------------------------------------
def load_streets(path: Path) -> gpd.GeoDataFrame:
    """Load the neat street network and attach the seg_id used downstream.

    seg_id MUST be the positional row index: `verified_hotspot_analysis.ipynb`
    does `streets_base["seg_id"] = streets_base.index` after a plain read_file,
    and every per-hour UTCI GeoPackage is keyed on it. Do not sort or filter
    this frame before assigning seg_id or the exposure join breaks silently.
    """
    gdf = gpd.read_file(path)
    gdf = gdf.reset_index(drop=True)
    gdf["seg_id"] = gdf.index
    gdf = gdf.to_crs(ANALYSIS_CRS)
    return gdf


def classify_segments(
    streets: gpd.GeoDataFrame,
    bike_lanes: gpd.GeoDataFrame | None,
    match_distance: float = 12.0,
) -> gpd.GeoDataFrame:
    """Assign each street segment a cycling-comfort class.

    The neat network carries NO bike infrastructure attributes -- the Valhalla
    tagging cell exports only highway/bicycle/access/oneway/name/geometry, and
    hardcodes `bicycle='yes'` everywhere. So the class has to come from an
    external join, currently the NYC Open Data bike routes layer.

    CHECK ME: the NYC layer's facility field has been named `ft_facilit` /
    `facilitycl` / `allclasses` in different vintages. Inspect before trusting.
    """
    streets = streets.copy()
    streets["bike_class"] = DEFAULT_CLASS

    if bike_lanes is None:
        print("  ! no bike-lane layer supplied -- every segment defaults to "
              f"'{DEFAULT_CLASS}'. Perceived distance will be a constant "
              "rescaling of physical distance and routes will NOT change.")
        return streets

    lanes = bike_lanes.to_crs(ANALYSIS_CRS).copy()

    facility_col = _guess_facility_column(lanes)
    if facility_col is None:
        print("  ! could not find a facility-class column in the bike-lane "
              f"layer; columns were {list(lanes.columns)}")
        return streets
    print(f"  using bike-lane facility column: {facility_col!r}")

    lanes["bike_class"] = lanes[facility_col].map(_nyc_facility_to_class)
    lanes = lanes.dropna(subset=["bike_class"])[["bike_class", "geometry"]]

    # Nearest join on segment midpoints. A midpoint is a better probe than the
    # full line: NYC lane geometries are digitised slightly off the roadbed
    # centreline, so line-to-line nearest joins pick up the parallel street.
    probes = streets[["seg_id", "geometry"]].copy()
    probes["geometry"] = probes.geometry.interpolate(0.5, normalized=True)

    joined = gpd.sjoin_nearest(
        probes, lanes, how="left",
        max_distance=match_distance, distance_col="_d",
    )
    # sjoin_nearest can emit ties -- keep the closest single match per segment.
    joined = (joined.sort_values(["seg_id", "_d"])
                    .drop_duplicates("seg_id")
                    .set_index("seg_id")["bike_class"])

    matched = joined.dropna()
    streets["bike_class"] = (
        streets["seg_id"].map(matched).fillna(DEFAULT_CLASS)
    )

    counts = streets["bike_class"].value_counts()
    print("  segment classes:")
    for cls, n in counts.items():
        print(f"    {cls:<10} {n:>7,}  ({n / len(streets):.1%})")
    return streets


def _guess_facility_column(lanes: gpd.GeoDataFrame) -> str | None:
    for cand in ("ft_facilit", "facilitycl", "allclasses", "facility",
                 "tf_facilit", "bikeclass", "class"):
        for col in lanes.columns:
            if col.lower() == cand:
                return col
    return None


def _nyc_facility_to_class(value) -> str | None:
    """Map an NYC bike-route facility string onto a comfort class.

    CHECK ME: NYC encodes facilities as free text ('Protected Path',
    'Standard', 'Sharrows', 'Greenway', ...) and the vocabulary has drifted
    across releases. Print the unique values on the real file and extend this.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v:
        return None
    if "greenway" in v or "off-street" in v or "off street" in v:
        return "offstreet"
    if "protected" in v or "curbside" in v or "cycle track" in v:
        return "protected"
    if "sharrow" in v or "shared" in v or "signed" in v or "route" in v:
        return "sharrow"
    if "standard" in v or "lane" in v or "buffer" in v:
        return "painted"
    return None


def build_graph(streets: gpd.GeoDataFrame, factors: dict) -> nx.DiGraph:
    """Build a directed graph weighted by perceived distance."""
    unknown = set(streets["bike_class"]) - set(factors)
    if unknown:
        raise SystemExit(f"no multiplier defined for class(es): {sorted(unknown)}")

    highway_col = "highway" if "highway" in streets.columns else "type"
    oneway_col = _guess_oneway_column(streets)
    if oneway_col is None:
        print("  ! no oneway column found -- treating every segment as "
              "bidirectional. This inflates connectivity; confirm against the "
              "shapefile before reporting results.")

    G = nx.DiGraph()
    skipped = 0

    for row in streets.itertuples(index=False):
        geom = row.geometry
        if geom is None or geom.is_empty:
            skipped += 1
            continue

        hw = getattr(row, highway_col, None)
        if isinstance(hw, str) and hw.strip().lower() in EXCLUDED_HIGHWAY:
            skipped += 1
            continue

        coords = list(geom.coords)
        if len(coords) < 2:
            skipped += 1
            continue

        u = _node_key(coords[0])
        v = _node_key(coords[-1])
        if u == v:
            skipped += 1  # self-loop, contributes nothing to a shortest path
            continue

        physical = geom.length
        factor = factors[row.bike_class]
        perceived = physical * factor

        attrs = dict(
            seg_id=row.seg_id,
            physical=physical,
            perceived=perceived,
            bike_class=row.bike_class,
            geometry=geom,
            reversed=False,
        )
        _add_edge(G, u, v, attrs)

        if oneway_col is None or not _is_oneway(getattr(row, oneway_col, None)):
            back = dict(attrs)
            back["reversed"] = True
            _add_edge(G, v, u, back)

    print(f"  graph: {G.number_of_nodes():,} nodes, "
          f"{G.number_of_edges():,} directed edges "
          f"({skipped:,} segments skipped)")

    if G.number_of_nodes():
        largest = max(nx.weakly_connected_components(G), key=len)
        frac = len(largest) / G.number_of_nodes()
        print(f"  largest weakly-connected component: {frac:.1%} of nodes")
        if frac < 0.90:
            print("  ! network is fragmented. Raise --node-precision or check "
                  "the neatnet output before routing.")
    return G


def _add_edge(G: nx.DiGraph, u, v, attrs: dict) -> None:
    """Add an edge, keeping the cheaper one if a parallel edge already exists."""
    existing = G.get_edge_data(u, v)
    if existing is not None and existing["perceived"] <= attrs["perceived"]:
        return
    G.add_edge(u, v, **attrs)


def _node_key(xy) -> tuple:
    return (round(xy[0] / NODE_PRECISION), round(xy[1] / NODE_PRECISION))


def _node_point(key) -> tuple:
    return (key[0] * NODE_PRECISION, key[1] * NODE_PRECISION)


def _guess_oneway_column(streets: gpd.GeoDataFrame) -> str | None:
    """Find the oneway column, tolerating ESRI shapefile name truncation.

    Shapefiles cap field names at 10 characters, so `oneway_flag` is written to
    disk as `oneway_fla`. `valhalla_routing_check.ipynb` cell 5 tests
    `if 'oneway_flag' in gdf.columns` -- which is FALSE for a shapefile round
    trip, so it silently falls through to `oneway = 'no'` and makes the whole
    network bidirectional. Worth checking whether the published run hit this.
    """
    for cand in ("oneway", "oneway_flag", "oneway_fla"):
        for col in streets.columns:
            if col.lower() == cand:
                return col
    # last resort: any column that starts with "oneway"
    for col in streets.columns:
        if col.lower().startswith("oneway"):
            return col
    return None


def _is_oneway(value) -> bool:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"yes", "true", "1", "-1"}
    return bool(value)


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------
def snap_stations(G: nx.DiGraph, stations: pd.DataFrame) -> dict:
    """Map each station_id to its nearest graph node.

    `stations` must have columns station_id, lat, lon (WGS84) -- the same
    median-per-station coordinates the Valhalla notebook builds in cell 22.
    """
    pts = gpd.GeoDataFrame(
        stations.copy(),
        geometry=gpd.points_from_xy(stations.lon, stations.lat, crs=4326),
    ).to_crs(ANALYSIS_CRS)

    node_keys = list(G.nodes)
    node_xy = np.array([_node_point(k) for k in node_keys])

    station_xy = np.column_stack([pts.geometry.x, pts.geometry.y])

    try:
        from scipy.spatial import cKDTree
        dist, idx = cKDTree(node_xy).query(station_xy, k=1)
    except ImportError:
        print("  ! scipy unavailable, falling back to brute-force snapping")
        dist, idx = _brute_force_nearest(station_xy, node_xy)

    mapping = {}
    far = 0
    for sid, i, d in zip(pts["station_id"], idx, dist):
        if d > 100.0:  # Valhalla used a 50 m snap radius; be a little looser
            far += 1
        mapping[str(sid)] = node_keys[i]

    print(f"  snapped {len(mapping):,} stations "
          f"(median {np.median(dist):.1f} m, {far:,} beyond 100 m)")
    return mapping


def _brute_force_nearest(a: np.ndarray, b: np.ndarray):
    idx = np.empty(len(a), dtype=int)
    dist = np.empty(len(a))
    for i, p in enumerate(a):
        d = np.hypot(b[:, 0] - p[0], b[:, 1] - p[1])
        j = int(d.argmin())
        idx[i], dist[i] = j, d[j]
    return dist, idx


def route_pairs(G: nx.DiGraph, pairs: pd.DataFrame, snapped: dict,
                weight: str = "perceived") -> gpd.GeoDataFrame:
    """Route every station pair, grouping by origin to reuse one Dijkstra tree.

    Citi Bike has ~2k stations but ~500k realised pairs, so a per-pair Dijkstra
    is enormously wasteful. One single-source pass per origin covers every
    destination from that origin at roughly the cost of one pair.
    """
    records = []
    no_path = 0
    no_node = 0

    grouped = pairs.groupby("start_station_id")
    total = len(grouped)

    for n, (start_id, group) in enumerate(grouped, 1):
        if n % 100 == 0 or n == total:
            print(f"    origin {n:,}/{total:,}", flush=True)

        source = snapped.get(str(start_id))
        if source is None:
            no_node += len(group)
            continue

        # Single-source Dijkstra: predecessor tree + costs for the whole graph.
        pred, _cost = nx.dijkstra_predecessor_and_distance(
            G, source, weight=weight
        )

        for end_id in group["end_station_id"]:
            target = snapped.get(str(end_id))
            if target is None:
                no_node += 1
                continue
            if target == source:
                continue
            if target not in pred:
                no_path += 1
                continue

            geom = _rebuild_geometry(G, pred, source, target)
            if geom is None:
                no_path += 1
                continue

            records.append({
                "orig_start": str(start_id),
                "orig_end": str(end_id),
                "geometry": geom,
            })

    print(f"  routed {len(records):,} pairs "
          f"({no_path:,} unreachable, {no_node:,} unsnapped)")

    return gpd.GeoDataFrame(records, geometry="geometry", crs=ANALYSIS_CRS)


def _rebuild_geometry(G, pred, source, target) -> LineString | None:
    """Walk the predecessor tree back from target and stitch edge geometries."""
    node_path = [target]
    cur = target
    while cur != source:
        parents = pred.get(cur)
        if not parents:
            return None
        cur = parents[0]
        node_path.append(cur)
    node_path.reverse()

    if len(node_path) < 2:
        return None

    coords: list = []
    for u, v in zip(node_path[:-1], node_path[1:]):
        edge = G.get_edge_data(u, v)
        if edge is None:
            return None
        seg = list(edge["geometry"].coords)
        if edge["reversed"]:
            seg.reverse()
        if coords and coords[-1] == seg[0]:
            seg = seg[1:]
        coords.extend(seg)

    if len(coords) < 2:
        return None
    return LineString(coords)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_factor_overrides(values: list[str] | None) -> dict:
    factors = dict(DEFAULT_FACTORS)
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"--factor expects name=value, got {item!r}")
        name, raw = item.split("=", 1)
        name = name.strip()
        if name not in factors:
            raise SystemExit(
                f"unknown class {name!r}; known: {sorted(factors)}"
            )
        factors[name] = float(raw)
    return factors


def main(argv=None) -> int:
    global NODE_PRECISION

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--streets", required=True, type=Path,
                   help="verified_streets_neat_bidirectional.shp")
    p.add_argument("--stations", required=True, type=Path,
                   help="CSV with station_id, lat, lon (WGS84)")
    p.add_argument("--pairs", required=True, type=Path,
                   help="CSV with start_station_id, end_station_id")
    p.add_argument("--bike-lanes", type=Path, default=None,
                   help="NYC Open Data bike routes layer (geojson/shp)")
    p.add_argument("--out", required=True, type=Path,
                   help="output GeoPackage path")
    p.add_argument("--layer", default="routes_with_correct_ids",
                   help="output layer name (downstream default)")
    p.add_argument("--factor", action="append", metavar="CLASS=VALUE",
                   help="override a perceived-distance multiplier; repeatable")
    p.add_argument("--weight", default="perceived",
                   choices=["perceived", "physical"],
                   help="'physical' reproduces the shortest-path baseline")
    p.add_argument("--node-precision", type=float, default=NODE_PRECISION,
                   help="endpoint welding tolerance in metres")
    args = p.parse_args(argv)

    NODE_PRECISION = args.node_precision

    factors = parse_factor_overrides(args.factor)
    print("perceived-distance multipliers:")
    for k, v in sorted(factors.items()):
        print(f"  {k:<10} {v}")
    if args.weight == "physical":
        print("  (routing on PHYSICAL distance -- multipliers ignored)")

    print("\nloading streets ...")
    streets = load_streets(args.streets)
    print(f"  {len(streets):,} segments")

    print("\nclassifying segments ...")
    lanes = gpd.read_file(args.bike_lanes) if args.bike_lanes else None
    streets = classify_segments(streets, lanes)

    print("\nbuilding graph ...")
    G = build_graph(streets, factors)

    print("\nsnapping stations ...")
    stations = pd.read_csv(args.stations)
    missing = {"station_id", "lat", "lon"} - set(stations.columns)
    if missing:
        raise SystemExit(f"stations CSV missing columns: {sorted(missing)}")
    snapped = snap_stations(G, stations)

    print("\nrouting ...")
    pairs = pd.read_csv(args.pairs, dtype=str).dropna()
    pairs = pairs.drop_duplicates(["start_station_id", "end_station_id"])
    print(f"  {len(pairs):,} unique station pairs")
    routes = route_pairs(G, pairs, snapped, weight=args.weight)

    if routes.empty:
        raise SystemExit("no routes produced -- nothing written")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    routes.to_file(args.out, layer=args.layer, driver="GPKG")
    print(f"\nwrote {len(routes):,} routes to {args.out} "
          f"(layer {args.layer!r}, EPSG:{ANALYSIS_CRS})")
    print("\nDownstream: point `route_gpkg` in verified_hotspot_analysis.ipynb "
          "at this file. Schema matches the Valhalla output.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
