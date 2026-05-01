# Mobility-driven heat exposure concentrates on a small fraction of urban streets

This repository contains analysis code accompanying the manuscript:

**Cabrera, A., Ziegler, D., & Schläpfer, M.** (2026). *Mobility-driven heat exposure concentrates on a small fraction of urban streets.*

## Overview

This work couples high-resolution urban microclimate modeling (WRF–BEP–SOLWEIG) with city-scale bicycle mobility data from the Citi Bike system to quantify mobility-weighted heat exposure across the New York City street network during the June 2024 heatwave. We identify a network-level concentration mechanism whereby a small fraction of street segments accounts for a disproportionate share of total heat-exposed travel, and evaluate the cooling efficiency of targeted tree-planting interventions.

## Repository status

This repository is being populated with the analysis code over the coming days. For early access, questions, or replication requests, please contact the corresponding author at m.schlaepfer@columbia.edu.

## Data sources

- Citi Bike trip data: https://citibikenyc.com/system-data
- NYC Open Data (building footprints, bike lanes): https://opendata.cityofnewyork.us
- ESA WorldCover 2021 land cover: https://esa-worldcover.org/
- American Community Survey 5-year estimates: https://www.census.gov/programs-surveys/acs/
- NCEP GFS reanalysis: https://www.nco.ncep.noaa.gov/pmb/products/gfs/

## Requirements

Python 3.10+. Key packages: `osmnx`, `geopandas`, `xarray`, `numpy`, `scipy`, `matplotlib`. A full `environment.yml` will be added shortly.

## Citation

If you use this code, please cite the paper:
*[Citation details to be added upon publication.]*

## License

MIT (see LICENSE file).

## Contact

Markus Schläpfer — m.schlaepfer@columbia.edu  
Department of Civil Engineering and Engineering Mechanics  
Columbia University
