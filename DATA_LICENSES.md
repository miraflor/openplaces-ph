# Data licensing and attribution

OpenPlaces PH separates the license of its **software** from the licenses of the **data it reads and produces**.

The source code in this repository is licensed under the [MIT License](LICENSE). That MIT license does **not** relicense upstream geospatial data and should not be interpreted as a blanket license for generated OpenPlaces PH datasets.

This document summarizes the upstream regimes used by the pipeline as of August 2026. It is operational documentation, not legal advice. Before publishing a derived database, verify the licenses that apply to the exact source releases used.

## Foursquare OS Places

Foursquare OS Places is provided under the **Apache License 2.0**.

The OpenPlaces PH normalized observation layer records:

```text
upstream_license = Apache-2.0
```

for direct Foursquare observations.

Official documentation:

- https://docs.foursquare.com/data-products/docs/fsq-places-open-source
- https://docs.foursquare.com/data-products/docs/access-fsq-os-places

Foursquare's published notice states:

```text
Copyright 2024 Foursquare Labs, Inc. All rights reserved.
```

Retain applicable attribution and notice requirements when redistributing Foursquare-derived data.

## OpenStreetMap

OpenStreetMap data is licensed under the **Open Data Commons Open Database License (ODbL) 1.0** by the OpenStreetMap Foundation.

The OpenPlaces PH normalized observation layer records:

```text
upstream_license = ODbL-1.0
```

for OpenStreetMap observations.

Official licensing page:

- https://www.openstreetmap.org/copyright

OpenStreetMap requires attribution to OpenStreetMap and its contributors. The ODbL also contains share-alike requirements that can apply when a derivative database is publicly distributed.

Because OpenPlaces PH combines and reconciles OSM observations with other databases, **do not assume that a published canonical dataset can simply be labeled MIT, Apache-2.0, or CC0**. The licensing of a distributed derived database should be evaluated for the concrete release architecture.

## Overture Maps Places

Overture Places is a **multi-license dataset**. The license depends on the upstream provider recorded in Overture provenance.

Official documentation:

- https://docs.overturemaps.org/guides/places/
- https://docs.overturemaps.org/attribution/

As documented by Overture in 2026, current Places providers include:

| Overture Places provider | License |
|---|---|
| Meta | CDLA-Permissive-2.0 |
| Microsoft | CDLA-Permissive-2.0 |
| PinMeTo | CDLA-Permissive-2.0 |
| Krick | CDLA-Permissive-2.0 |
| RenderSEO | CDLA-Permissive-2.0 |
| DAC | CDLA-Permissive-2.0 |
| BrightQuery | CDLA-Permissive-2.0 |
| Foursquare | Apache-2.0 |
| AllThePlaces | CC0-1.0 |

OpenPlaces PH preserves the raw Overture `sources` provenance string and derives an `upstream_license` value conservatively. Only the `dataset` values of that provenance are compared with the provider names; record ids and timestamps are ignored, because a provider pattern such as `dac` can otherwise match characters inside a record id. The value is re-derived at every finalization, so a corrected rule applies without downloading Overture again:

```text
Foursquare provenance                 -> Apache-2.0
AllThePlaces provenance               -> CC0-1.0
known CDLA-provider provenance        -> CDLA-Permissive-2.0
multiple detected license families    -> MIXED/REVIEW
unknown future provider               -> UNKNOWN
```

`MIXED/REVIEW` and `UNKNOWN` are deliberate safeguards. They prevent a future Overture provider from being silently assigned the wrong license merely because the pipeline has not yet been updated.

## OpenPlaces PH output fields

The normalized observation table includes:

```text
source
source_id
provenance
upstream_license
```

The canonical output exposes the upstream license alongside each represented source:

```text
fsq_license
overture_license
osm_license
```

These fields are provenance metadata. They are not themselves a legal conclusion about the license of the canonical database as a whole.

## Publishing OpenPlaces PH data

Before a public data release:

1. record the exact Foursquare, Overture, and OSM source snapshots used (`summary.json` → `sources`);
2. retain source IDs and provenance fields;
3. retain required copyright/license notices;
4. provide OpenStreetMap attribution where OSM data are used;
5. preserve Overture provider-level licensing information;
6. include Foursquare's applicable Apache-2.0 notice; and
7. explicitly state the license chosen for the released derived database only after checking that it is compatible with all applicable upstream obligations.

The repository's MIT license applies to the **software**, not automatically to `canonical_pois.parquet`, `observations.parquet`, or other generated data products.
