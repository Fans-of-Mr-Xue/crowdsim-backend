# Position-aware pedestrian routing

The backend routes pedestrians between exact offsets on SUMO edges.  It does
not treat an edge as a single point and does not use the edge's directed order
as a pedestrian walking direction.

## Model

- Every pedestrian edge has distinct start and end endpoint states.
- Traversing an edge is possible in both directions when pedestrians are
  allowed.
- Endpoint transitions come from SUMO pedestrian connections, including
  crossing and walking-area edges.
- A position at offset `x` on an edge of length `L` connects to its endpoints
  with distances `x` and `L - x`.
- Internal SUMO edges participate in distance and travel-time calculations but
  are omitted from the public route passed back to SUMO.

`PositionAwarePedestrianRouter.route()` finds the least-cost route between two
positions. `route_via_edges()` evaluates both orientations of every configured
access portal and traverses each selected portal exactly once.

## Scene configuration

Hotspots can declare generic access portals:

```json
"access_portals": [
  {
    "id": "west_gate",
    "edge": "178411801#0",
    "outside_side": "auto",
    "inside_side": "auto"
  }
]
```

Portal orientation is normally inferred by evaluating both directions.  The
legacy `entry_edges` field remains supported and is normalized to portals by
`HotspotCatalog`. The generic `approach_edges` field is also accepted while the
legacy `park_access_edges` name remains compatible. An open hotspot may omit
portals entirely.

## Dynamic costs and performance

The graph is built once when a network is loaded.  Shortest-path trees are
cached by endpoint, forbidden-edge set, and congestion snapshot. Exact person
positions add only partial-edge costs and therefore share the same trees.

All visitors in one route-choice interval share one congestion snapshot.
Profiles determine whether a time saving justifies changing route; they do not
build a separate path tree per person. Old dynamic trees are discarded when a
new snapshot becomes active, while free-flow trees remain cached.

Routing diagnostics are exposed through `RouteProvider.diagnostics`, including
graph size, tree builds, cache hits, query count, and cached-tree count.

## Current monument scene

The monument's M1 and M2 edges are configured as two access portals. Initial
approach, runtime replanning, and dispersal routes now all use exact source and
target positions. The generated route file is rebuilt from this same geometry,
so initial and runtime route costs no longer use different path models.
