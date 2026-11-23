# Search engine: third-party software

**Read on:** 2026-10-10, from the image `dbt-search-api:engine-rc7` on testbox (Python 3.12.14, Debian 13) and from
`Dockerfile.rtc`'s packages resolved inside that image. **Not legal advice.**

This lists the software the engine runs on, not the data (see `docs/legal/SEARCH-DATA-LICENSES.md`) and not the
models (see `serve/api/NOTICE` and `docs/legal/SEARCH-MODEL-LICENSES.md`). The engine's own code is AGPL-3.0
(`LICENSE`). The source tree contains none of the software below; it is fetched when an image or bundle is
built, so the obligations here fall on **whoever distributes a built image or installer**.

Licence and copyright come from each package's own metadata and licence files as installed. "Bundled" means
shared libraries shipped inside the wheel, which pip metadata does not describe. The library names were read from the
wheels; **the licences given for bundled libraries are each upstream project's known licence, written from
memory, not read from the files**, because most wheels ship no text for them.

## 1. search-api image (`serve/api/Dockerfile`)

Installed by name:

| Package | Version | Licence | Copyright |
|---|---|---|---|
| onnxruntime | 1.30.0 | MIT | Microsoft Corporation |
| openvino | 2026.4.0 | Apache-2.0 | Intel Corporation |
| tokenizers | 0.23.2 | Apache-2.0 | Hugging Face (the wheel carries no licence file) |
| tantivy (tantivy-py) | 0.26.2 | MIT | Quickwit, Inc. and contributors |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 2005-2025 NumPy Developers |
| psycopg, psycopg-binary | 3.3.6 | LGPL-3.0-only | The Psycopg Team |

Pulled in by those:

| Package | Version | Licence | Copyright |
|---|---|---|---|
| anyio | 4.15.1 | MIT | 2018 Alex Grönholm |
| certifi | 2026.7.22 | MPL-2.0 | Kenneth Reitz (CA bundle from Mozilla) |
| click | 8.5.0 | BSD-3-Clause | 2014 Pallets |
| filelock | 4.0.3 | MIT | 2025 Bernát Gábor and contributors |
| flatbuffers | 25.12.19 | Apache-2.0 | Google (no licence file in the wheel) |
| fsspec | 2026.9.0 | BSD-3-Clause | 2018 Martin Durant |
| h11 | 0.16.0 | MIT | 2016 Nathaniel J. Smith and other contributors |
| hf-xet | 1.6.0 | Apache-2.0 | Hugging Face |
| httpcore | 1.0.9 | BSD-3-Clause | 2020 Encode OSS Ltd |
| httpx | 0.28.1 | BSD-3-Clause | 2019 Encode OSS Ltd |
| huggingface_hub | 1.33.0 | Apache-2.0 | Hugging Face |
| idna | 3.20 | BSD-3-Clause | 2013-2026 Kim Davies and contributors |
| openvino-telemetry | 2025.2.0 | Apache-2.0 | Intel Corporation |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | Donald Stufft and individual contributors |
| pip | 25.0.1 | MIT | 2008-present The pip developers |
| protobuf | 7.36.2 | BSD-3-Clause | 2008 Google Inc. |
| PyYAML | 6.0.3 | MIT | 2017-2021 Ingy döt Net; 2006-2016 Kirill Simonov |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | 2013 noamraph and tqdm developers |
| typing_extensions | 4.16.0 | PSF-2.0 | Python Software Foundation |

Bundled shared libraries:

| Wheel | Libraries inside | Licence of those libraries |
|---|---|---|
| numpy | OpenBLAS (`libscipy_openblas64_`), `libgfortran`, `libquadmath` | OpenBLAS BSD-3-Clause; libgfortran GPL-3.0 with the GCC Runtime Library Exception; libquadmath LGPL-2.1-or-later. numpy ships all three texts (20 licence files) |
| onnxruntime | `libonnxruntime`, `libonnxruntime_providers_shared` | MIT; `ThirdPartyNotices.txt` in the wheel names the statically linked parts (protobuf, abseil, Eigen, zlib, pybind11 and others) |
| openvino | 17 `libopenvino*` libraries; oneTBB (`libtbb`, `libtbbbind`, `libtbbmalloc`, `libtbbmalloc_proxy`); `libhwloc` | OpenVINO Apache-2.0; oneTBB Apache-2.0; hwloc BSD-3-Clause. The wheel ships 4 licence files |
| psycopg-binary | `libpq`; OpenSSL (`libssl`, `libcrypto`); MIT Kerberos (`libkrb5`, `libgssapi_krb5`, `libk5crypto`, `libkrb5support`, `libcom_err`); OpenLDAP (`libldap`, `liblber`); Cyrus SASL; PCRE; `libselinux`; `libcrypt`; `libkeyutils` | PostgreSQL; Apache-2.0; MIT; OpenLDAP Public License; BSD-style (Cyrus); BSD-3-Clause (PCRE); public domain (libselinux); LGPL-2.1-or-later (libxcrypt, keyutils). **The wheel ships only the LGPL-3.0 text for psycopg itself**, none for these 15 libraries |

From the base image and the Dockerfile:

| Component | Version | Licence |
|---|---|---|
| Python | 3.12.14 | PSF-2.0 |
| Debian 13 "trixie" base (`python:3.12-slim`) | — | per package, in `/usr/share/doc/*/copyright` in the image |
| ocl-icd-libopencl1 | 2.3.3 | BSD-2-Clause |
| Intel compute runtime (`intel-opencl-icd` 26.35.39758.10, `-legacy1` 24.35.30872.36) | — | MIT, Intel Corporation |
| Intel Graphics Compiler (`intel-igc-core`/`-opencl` 2.41.5 and 1.0.17537.24) | — | MIT, Intel Corporation (bundles LLVM, Apache-2.0 WITH LLVM-exception) |
| libigdgmm12 | 22.10.0 | MIT, Intel Corporation |

## 2. WebRTC peer image (`serve/api/Dockerfile.rtc`)

Everything in §1, plus:

| Package | Version | Licence | Copyright |
|---|---|---|---|
| aiortc | 1.15.0 | BSD-3-Clause | Jeremy Lainé |
| websockets | 17.1 | BSD-3-Clause | Aymeric Augustin and contributors |
| qrcode | 8.2 | BSD (metadata says only "BSD") | 2011 Lincoln Loop; 2009 Kazuhiko Arase |
| aioice | 0.10.2 | BSD-3-Clause | 2018-2019 Jeremy Lainé |
| cffi | 2.1.1 | MIT-0 | Armin Rigo, Maciej Fijalkowski |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | The Python Cryptographic Authority and individual contributors |
| dnspython | 2.9.0 | ISC | Dnspython Contributors; Nominum, Inc. |
| google-crc32c | 1.9.0 | Apache-2.0 | Google LLC (bundles `libcrc32c`, BSD-3-Clause) |
| ifaddr | 0.2.0 | MIT | 2014 Stefan C. Mueller |
| pycparser | 3.11 | BSD-3-Clause | Eli Bendersky |
| pyee | 14.0.0 | MIT | 2021 Josh Holbrook |
| pylibsrtp | 1.0.0 | BSD-3-Clause | Jeremy Lainé (links libsrtp, BSD-3-Clause, Cisco Systems) |
| pyOpenSSL | 26.4.0 | Apache-2.0 | The pyOpenSSL developers |

**PyAV (`av`) is deliberately not installed.** aiortc lists it as a requirement, for audio and video;
`rtc_peer.py` opens data channels only. PyAV 17.1.0's wheel is 33 shared libraries, about 100 MB: FFmpeg
configured with `--enable-version3 --enable-libx264 --enable-libx265` and **without `--enable-gpl`**, so the
binaries call themselves "LGPL version 3 or later" while linking x264 and x265, which are GPL-2.0-or-later;
plus GnuTLS, nettle, GMP, LAME and others under LGPL, and no licence text for any of them. Handing that out
would mean the GPL and LGPL texts, a source offer, and an answer to what licence that FFmpeg build is really
under.

Since 2026-10-10 `Dockerfile.rtc` installs aiortc with `--no-deps`, names its other requirements, and puts
`av_absent.py` in as `av`: the dozen names aiortc imports, each of which raises if used. The image holds no
FFmpeg and no codec. `rtc_selfcheck.py` runs in the build and fails it unless two peers open a data channel
and pass a message each way. Measured on testbox: the rtc layer adds 40 MB to the search-api image, and a search
for `libav*` and `libx26*` in the built image finds 0 files.

## 3. Not in any image, used at build time

- `tools/tantivy-structure` (Rust): its crates are pinned in `Cargo.lock`; not enumerated here.
- Importer, refresh, news and lookup-builder images (`importer/requirements.txt` and four more Dockerfiles):
  they build the data and are not handed to users. Not enumerated here.

## 4. Not checked

- **Rust crates compiled into the `tantivy` and `tokenizers` wheels.** Each is one static library built from
  dozens of crates (mostly MIT or Apache-2.0); neither wheel lists them. tantivy's wheel carries one licence
  file, tokenizers' none.
- **The Debian packages of the base image**, one by one.
- **Any Windows or macOS bundle.** None is built yet; a frozen bundle redistributes everything in §1 and §2
  directly, and the wheels for those systems bundle different libraries.
- **No rtc image was on testbox before 2026-10-10**: the versions in §2 are the ones `Dockerfile.rtc` pins, resolved
  and built there that day (as a test image, since removed). Their own dependencies (cffi, dnspython, ifaddr,
  pycparser) are not pinned and may resolve differently on another day.

## 5. What a distributed image still lacks

1. The licence texts. `/app/licenses/` holds Apache-2.0 and the bge-small MIT text only. Needed in addition:
   MIT, BSD-2-Clause, BSD-3-Clause, LGPL-3.0, LGPL-2.1, GPL-3.0 with the GCC exception, MPL-2.0, PSF-2.0, ISC,
   PostgreSQL, OpenLDAP. Most are already in the wheels' `dist-info` directories inside the image; the
   psycopg-binary libraries are the ones with nothing.
2. `serve/api/NOTICE` names eight components. It does not name numpy, the psycopg-binary libraries or any
   package of the rtc image.
3. The LGPL libraries (psycopg and the ones bundled with it) are met by shipping them as separate, replaceable shared
   libraries, which a Docker image and a pip install both do. A frozen single-file installer would not.

## Live data: airport status

Not software, listed here because the collector (`news/wxall.py`) fetches it on every install. Both arrive as whole
files on a schedule; a search reads the local copy and never causes a request.

| Data | Source | Licence | Credit shown |
|---|---|---|---|
| Airport delays, ground stops and closures (US) | FAA National Airspace System status, `nasstatus.faa.gov/api/airport-status-information` | US government work, public domain in the United States (17 U.S.C. 105) | "FAA National Airspace System status", linked, on the card |
| Tide harmonic constants, datums and station offsets (US), shipped as `serve/api/tides.json.gz`; the highs and lows are calculated by this software | NOAA Center for Operational Oceanographic Products and Services (CO-OPS), `api.tidesandcurrents.noaa.gov` | US government work, public domain in the United States (17 U.S.C. 105) | "NOAA station <name>" and "Calculated here from NOAA constants; not for navigation" on the box |
| Cities outside the United States with their position, population and time zone, for sun and moon answers ("sunset in london"), shipped as `serve/api/world_cities.tsv.gz`. Changed from the original: only cities of 15,000 people or more outside the US, nine of the columns, positions rounded | GeoNames `cities15000`, `countryInfo`, `admin1CodesASCII` (download.geonames.org/export/dump) | Creative Commons Attribution 4.0 (creativecommons.org/licenses/by/4.0) | "City data from GeoNames (geonames.org), CC BY 4.0, filtered" in this file and on the licences page |
| Equinox and solstice times 2025-2045 (a table in `serve/api/almanac.py`); sun and moon times are calculated by this software | U.S. Naval Observatory, Astronomical Applications, `aa.usno.navy.mil` | US government work, public domain in the United States (17 U.S.C. 105) | "Times from the U.S. Naval Observatory" on the box |
| Solar and lunar eclipses 2024-2050: date, time of greatest eclipse (TD, with its delta T), type, magnitude, durations, where the greatest eclipse is, and the regions and path countries NASA names, shipped as `serve/api/almanac_sky.json.gz` (built by `tools/build_almanac_sky.py`) | NASA GSFC, Five Millennium Catalogs of Solar and Lunar Eclipses and the decade pages, Fred Espenak and Jean Meeus, `eclipse.gsfc.nasa.gov` | NASA states on each catalog page: "Permission is freely granted to reproduce this data when accompanied by an acknowledgment" | "Eclipse Predictions by Fred Espenak (NASA's GSFC)", in the note under every eclipse card and on the licences page |
| Meteor showers: names, activity dates, date of the maximum, solar longitude, radiant, speed and ZHR from Table 5 (Working List of Visual Meteor Showers) of the 2026 and 2027 calendars, shipped in the same file. Facts only: none of the IMO's text is copied | International Meteor Organization, Meteor Shower Calendar, `imo.net/files/meteor-shower` | The calendar states no licence. Dates, rates and positions are facts and not copyrightable, and the calendar's prose and layout are not used; all of Table 5 (every row) ships, so the owner may want to ask the IMO | "International Meteor Organization", in the note under every shower card and on the licences page |
| Last spring and first fall freeze dates (10, 50 and 90 percent, 32 F and 28 F) and season length for 6,535 US stations, shipped as `serve/api/frost_normals.tsv.gz` (built by `tools/build_frost_normals.py`) | NOAA NCEI, 1991-2020 U.S. Climate Normals, annual/seasonal by-station, `ncei.noaa.gov/data/normals-annualseasonal/1991-2020` | US government work, public domain in the United States (17 U.S.C. 105) | "NOAA station <name>" and "1991-2020 U.S. Climate Normals" on the box |
| Airport names, codes, towns and positions | OurAirports `airports.csv` (ourairports.com/data) | Public domain (the project's own dedication) | none required |
| On-time record by flight number (monthly, about two months late) | Bureau of Transportation Statistics, Marketing Carrier On-Time Performance, `transtats.bts.gov/PREZIP` | US government work, public domain in the United States (17 U.S.C. 105) | "Bureau of Transportation Statistics", linked, on the card |

