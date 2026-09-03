# Perceived-distance routing — working notes

Working notes for the extension task: *how do cyclist heat exposure and the
effectiveness of tree planting change when cyclists route by perceived rather
than physical distance?*

---

## 1. Where the shortest-path assumption actually lives

Not in a graph library. Routing runs through **Valhalla**, a routing engine in
a local Docker container on `localhost:8002`, called with `costing="bicycle"`.

The chain in `notebooks/valhalla_routing_check.ipynb`:

```
verified_streets_neat_bidirectional.shp
  → cell 5: tag + export GeoJSON
  → ogr2osm            → verified_nyc_tagged.osm
  → osmium renumber/cat → verified_nyc_tagged.pbf
  → valhalla_build_tiles
  → cells 22–29: batch HTTP route calls over all station pairs
  → verified_station_pair_routes.gpkg
  → cell 31: re-snap station IDs
  → verified_station_pair_routes_fixed.gpkg   ← what downstream consumes
```

There is no edge-weight attribute to swap out. Perceived distance cannot be
implemented by editing one line.

## 2. The network carries no cycling information

`valhalla_routing_check.ipynb` cell 5 is the only place road semantics enter:

```python
gdf['highway'] = gdf['type'].replace(fix).fillna('residential')
gdf['bicycle'] = 'yes'
gdf['access']  = 'yes'
gdf['oneway']  = np.where(gdf['oneway_flag'] == 1, 'yes', 'no')
```

and it exports only `highway, bicycle, access, oneway, name, geometry`.

So: **no `cycleway` tag, no bike-lane class, no traffic volume, and
`bicycle='yes'` hardcoded on every segment.** Whatever `type` values the neat
shapefile carries are collapsed into coarse OSM highway classes, and anything
missing becomes `residential`.

This is the real blocker. Perceived-distance weights need a per-segment comfort
class, and the network has none. It has to come from an external join — NYC
Open Data's bike routes layer is the obvious candidate and is already cited in
the README as a data source, so someone has used it before.

## 3. Implementation options

| | approach | arbitrary per-segment factors? | keeps pipeline? |
|---|---|---|---|
| A | rebuild graph in networkx, weight = `length × factor` | yes | no — replaces Valhalla |
| B | re-tag network, tune Valhalla bicycle costing (`use_roads`, `avoid_bad_surfaces`, `bicycle_type`) | no, only global knobs | yes |
| C | Valhalla per-edge costing overrides | not really supported | yes |

**A is the recommendation.** It matches the literature formulation exactly, it
makes the robustness sweep trivial (re-run with a different factor dict), and
it drops the Docker/ogr2osm/osmium toolchain that is the single biggest
obstacle to anyone reproducing this work. The cost is that we are no longer
reproducing the published baseline bit-for-bit.

Mitigation: `scripts/perceived_distance_routing.py --weight physical` routes on
physical distance through the same graph. Comparing that against the published
Valhalla routes isolates "engine change" from "assumption change" — worth doing
before trusting any perceived-distance result.

## 4. What the new script does

`scripts/perceived_distance_routing.py` is a drop-in replacement producing the
same file downstream already reads:

- layer `routes_with_correct_ids`, columns `orig_start, orig_end, geometry`,
  EPSG:32118
- so `verified_hotspot_analysis.ipynb` only needs its `route_gpkg` path changed

Design notes:

- **seg_id must stay the positional row index.** `verified_hotspot_analysis`
  does `streets_base["seg_id"] = streets_base.index` on a plain `read_file`,
  and every per-hour UTCI GeoPackage is keyed on it. Any reorder or filter of
  the street frame before that assignment silently corrupts the exposure join.
- **One Dijkstra per origin, not per pair.** ~2k stations vs. ~500k realised
  pairs; single-source predecessor trees make the whole run tractable.
- **Midpoint probes for the lane join.** NYC lane geometries are digitised off
  the roadbed centreline, so line-to-line nearest joins grab parallel streets.

Status: unvalidated. No data and no `libs/` module in this repo, so it has not
been run end to end. Expect to fix column names on first contact.

## 5. Open questions / discrepancies found

- **`MIN_LEN` disagrees between scripts.** `verified_hotspot_analysis.ipynb`
  cell 15 sets `BUF, MIN_LEN, MIN_RATIO, HOT = 3, 10, 0.20, 32.0`, but
  `scripts/build_hotspots_1000trees.txt` sets `MIN_LEN = 5`. Same constant,
  different values, both feeding segment-usage counts. Which is the published
  one? This directly affects which segments enter the top-1000.
- **`libs/valhalla_routing_client.py` is not in this repo** — it is imported
  from a separate `biketrip-heat-exposure` folder. Needed to reproduce the
  baseline at all.
- **No data, no `environment.yml`** (added here), **no LICENSE** despite the
  README claiming MIT.
- **Results notebooks hand-copy numbers from screenshots** (`trees_analysis_results.ipynb`
  literally builds DataFrames from typed-in values "from the screenshots"), so
  "reproduce the baseline" has soft spots that aren't code.
- **Which paper are the multipliers from?** The factors in the script are
  placeholders. Need the actual categories and values before any result is
  reportable.
- **Are the multipliers directional?** An uphill or high-traffic segment may be
  asymmetric. The graph is directed and could carry per-direction factors, but
  the literature values are probably symmetric — worth confirming.

## 6. Next steps

1. Get `libs/`, the data, and the multiplier paper.
2. Run `--weight physical` and diff against the published routes. Quantify the
   engine change on its own.
3. Inspect the real `type` and NYC facility vocabularies; fix the class mapping.
4. Single OD pair sanity check: plot physical vs. perceived path, confirm they
   differ in the expected direction.
5. Full re-route, then rerun exposure and the top-1000 tree selection.
6. Sweep factors and report how much the priority set moves. That overlap
   fraction is the actual deliverable.
