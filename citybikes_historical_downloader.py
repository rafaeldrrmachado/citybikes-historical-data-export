#!/usr/bin/env python3
"""Interactive downloader for CityBikes historical Parquet data.

Features
--------
- Search and select one or more bike-share networks by name, ID, city, or country.
- Select an inclusive YYYY-MM date interval.
- Discover files from the CityBikes yearly JSON indexes.
- Resume-safe downloads: existing files with the expected size are skipped.
- Optional merge into one Parquet file per network or one file for all networks.
- Optional SHA-256-free integrity checks using HTTP size and Parquet readability.

Examples
--------
    python citybikes_historical_downloader.py
    python citybikes_historical_downloader.py --insecure
    python citybikes_historical_downloader.py --output D:\\Data\\CityBikes

Dependencies
------------
    pip install requests pyarrow InquirerPy
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests

try:
    from InquirerPy import inquirer
except ImportError:
    inquirer = None
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = None
    pq = None

API_NETWORKS_URL = "https://api.citybik.es/v2/networks"
HISTORICAL_BASE_URL = "https://data.citybik.es/dumps/by-network"
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    city: str = ""
    country: str = ""

    @property
    def label(self) -> str:
        location = ", ".join(part for part in (self.city, self.country) if part)
        return f"{self.name} [{self.id}]" + (f" - {location}" if location else "")


@dataclass(frozen=True)
class RemoteFile:
    network_id: str
    year: int
    month: int
    name: str
    size: int | None
    mtime: str | None

    @property
    def url(self) -> str:
        return f"{HISTORICAL_BASE_URL}/{self.year}/{quote(self.name)}"


def make_session(insecure: bool) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "HEAD")),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "CityBikes-Historical-Downloader/1.0"})
    session.verify = not insecure
    return session


def get_json(session: requests.Session, url: str):
    try:
        response = session.get(url, timeout=(20, 90))
        response.raise_for_status()
        return response.json()
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            "SSL certificate validation failed. On a trusted network, run the script "
            "with --insecure, or install your organisation's root certificate."
        ) from exc
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError(f"Expected JSON from {url}, but received another format.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Request failed for {url}: {exc}") from exc


def load_networks(session: requests.Session) -> list[Network]:
    payload = get_json(session, API_NETWORKS_URL)
    results: list[Network] = []
    for item in payload.get("networks", []):
        location = item.get("location") or {}
        network_id = str(item.get("id", "")).strip()
        if not network_id:
            continue
        results.append(
            Network(
                id=network_id,
                name=str(item.get("name") or network_id),
                city=str(location.get("city") or ""),
                country=str(location.get("country") or ""),
            )
        )
    return sorted(results, key=lambda n: (n.name.casefold(), n.id.casefold()))


def search_networks(networks: list[Network], query: str) -> list[Network]:
    terms = query.casefold().split()
    if not terms:
        return networks

    def searchable(n: Network) -> str:
        return " ".join((n.id, n.name, n.city, n.country)).casefold()

    return [n for n in networks if all(term in searchable(n) for term in terms)]


def parse_selection(raw: str, displayed: list[Network]) -> list[Network]:
    chosen: list[Network] = []
    seen: set[str] = set()
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= len(displayed):
            network = displayed[int(token) - 1]
        else:
            exact = [n for n in displayed if n.id.casefold() == token.casefold()]
            if not exact:
                print(f"  Ignored unknown selection: {token}")
                continue
            network = exact[0]
        if network.id not in seen:
            chosen.append(network)
            seen.add(network.id)
    return chosen

def get_available_months(
    session: requests.Session,
    selected: list[Network],
) -> list[str]:
    """Return all available YYYY-MM values for the selected networks."""

    wanted_ids = {n.id.casefold() for n in selected}
    months = set()

    current_year = datetime.now().year

    for year in range(2015, current_year + 1):
        try:
            entries = get_json(
                session,
                f"{HISTORICAL_BASE_URL}/{year}/"
            )
        except Exception:
            continue

        if not isinstance(entries, list):
            continue

        for entry in entries:

            if entry.get("type") != "file":
                continue

            name = str(entry.get("name", ""))

            for network_id in wanted_ids:

                suffix = f"-{network_id}-stats.parquet"

                if not name.casefold().endswith(suffix):
                    continue

                prefix = name[:6]

                if len(prefix) == 6 and prefix.isdigit():

                    months.add(
                        f"{prefix[:4]}-{prefix[4:]}"
                    )

                break

    return sorted(months)

def choose_networks(networks: list[Network]) -> list[Network]:
    """Searchable multi-select list controlled with the keyboard."""
    if inquirer is None:
        raise RuntimeError(
            "The interactive selector requires InquirerPy. Install it with: "
            "python -m pip install InquirerPy"
        )

    choices = [
        {
            "name": network.label,
            "value": network.id,
        }
        for network in networks
    ]

    print("\nNETWORK SELECTION")
    print("Start typing to filter the list by network name, ID, city, or country.")
    print("Controls:")
    print("  Up/Down or j/k : move")
    print("  TAB          : select or deselect")
    print("  Enter          : confirm selection and continue")
    print("  Ctrl+C         : cancel\n")

    selected_ids = inquirer.fuzzy(
    message="Choose one or more networks:",
    choices=choices,
    multiselect=True,
    match_exact=True,
    validate=lambda result: len(result) > 0,
    invalid_message="Select at least one network before pressing Enter.",
    ).execute()

    selected_set = set(selected_ids)
    return [network for network in networks if network.id in selected_set]


def parse_month(raw: str) -> tuple[int, int]:
    match = MONTH_RE.fullmatch(raw.strip())
    if not match:
        raise ValueError("Use YYYY-MM, for example 2025-01.")
    return int(match.group(1)), int(match.group(2))


def month_number(year: int, month: int) -> int:
    return year * 12 + month


def choose_interval(
    available_months: list[str],
) -> tuple[tuple[int, int], tuple[int, int]]:

    if not available_months:
        raise RuntimeError(
            "No historical data was found for the selected networks."
        )

    first_month = available_months[0]
    last_month = available_months[-1]

    print("\nAVAILABLE DATA")
    print(f"  From : {first_month}")
    print(f"  To   : {last_month}")
    print(f"  Total months available: {len(available_months)}")

    while True:

        try:

            raw_start = input(
                f"\nStart month [{first_month}]: "
            ).strip()

            raw_end = input(
                f"End month   [{last_month}]: "
            ).strip()

            start = parse_month(
                raw_start or first_month
            )

            end = parse_month(
                raw_end or last_month
            )

            if month_number(*start) > month_number(*end):
                raise ValueError(
                    "The start month must not be after the end month."
                )

            return start, end

        except ValueError as exc:
            print(f"Invalid interval: {exc}")

def included_years(start: tuple[int, int], end: tuple[int, int]) -> range:
    return range(start[0], end[0] + 1)


def discover_files(
    session: requests.Session,
    selected: list[Network],
    start: tuple[int, int],
    end: tuple[int, int],
) -> list[RemoteFile]:
    wanted_ids = {n.id.casefold(): n.id for n in selected}
    found: list[RemoteFile] = []

    for year in included_years(start, end):
        index_url = f"{HISTORICAL_BASE_URL}/{year}/"
        print(f"Reading index for {year}...")
        try:
            entries = get_json(session, index_url)
        except RuntimeError as exc:
            print(f"  Warning: {exc}")
            continue
        if not isinstance(entries, list):
            print(f"  Warning: unexpected index format for {year}; skipped.")
            continue

        for entry in entries:
            if entry.get("type") != "file":
                continue
            name = str(entry.get("name") or "")
            for network_key, original_id in wanted_ids.items():
                suffix = f"-{network_key}-stats.parquet"
                if not name.casefold().endswith(suffix):
                    continue
                prefix = name[:6]
                if len(prefix) != 6 or not prefix.isdigit():
                    continue
                file_year, file_month = int(prefix[:4]), int(prefix[4:6])
                current = month_number(file_year, file_month)
                if month_number(*start) <= current <= month_number(*end):
                    size_value = entry.get("size")
                    found.append(
                        RemoteFile(
                            network_id=original_id,
                            year=file_year,
                            month=file_month,
                            name=name,
                            size=int(size_value) if size_value is not None else None,
                            mtime=entry.get("mtime"),
                        )
                    )
                break

    return sorted(found, key=lambda f: (f.network_id.casefold(), f.year, f.month, f.name))


def human_size(size: int | None) -> str:
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def download_file(
    session: requests.Session,
    remote: RemoteFile,
    target: Path,
    overwrite: bool,
) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        if remote.size is None or target.stat().st_size == remote.size:
            return "skipped"
        print(f"  Existing file has the wrong size; downloading again: {target.name}")

    temp = target.with_suffix(target.suffix + ".part")
    temp.unlink(missing_ok=True)
    try:
        with session.get(remote.url, stream=True, timeout=(20, 300)) as response:
            response.raise_for_status()
            expected = remote.size
            header_size = response.headers.get("Content-Length")
            if expected is None and header_size and header_size.isdigit():
                expected = int(header_size)

            downloaded = 0
            with temp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if expected:
                        percent = downloaded / expected * 100
                        print(
                            f"\r  {target.name}: {human_size(downloaded)} / "
                            f"{human_size(expected)} ({percent:5.1f}%)",
                            end="",
                            flush=True,
                        )
            if expected:
                print()
            if expected is not None and downloaded != expected:
                raise IOError(
                    f"Size mismatch for {target.name}: expected {expected}, got {downloaded}."
                )
        temp.replace(target)
        return "downloaded"
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def ensure_pyarrow() -> None:
    if pa is None or pq is None:
        raise RuntimeError(
            "Merging requires pyarrow. Install it with: pip install pyarrow"
        )


def merge_parquet_files(files: list[Path], output: Path) -> None:
    """Stream row groups into one file while unifying compatible schemas."""
    ensure_pyarrow()
    if not files:
        raise RuntimeError("No Parquet files were available to merge.")

    schemas = [pq.ParquetFile(path).schema_arrow for path in files]
    try:
        unified_schema = pa.unify_schemas(schemas)
    except Exception as exc:
        raise RuntimeError(
            "The selected files have incompatible Parquet schemas and could not be merged. "
            "The monthly files remain available separately."
        ) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".part")
    temp.unlink(missing_ok=True)
    writer = None
    try:
        writer = pq.ParquetWriter(temp, unified_schema, compression="snappy")
        for path in files:
            parquet_file = pq.ParquetFile(path)
            for batch in parquet_file.iter_batches(batch_size=100_000):
                table = pa.Table.from_batches([batch])
                arrays = []
                for field in unified_schema:
                    if field.name in table.column_names:
                        column = table[field.name]
                        if not column.type.equals(field.type):
                            column = column.cast(field.type, safe=False)
                        arrays.append(column)
                    else:
                        arrays.append(pa.nulls(table.num_rows, type=field.type))
                aligned = pa.Table.from_arrays(arrays, schema=unified_schema)
                writer.write_table(aligned)
        writer.close()
        writer = None
        temp.replace(output)
    except Exception:
        if writer is not None:
            writer.close()
        temp.unlink(missing_ok=True)
        raise


def yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    raw = input(prompt + suffix).strip().casefold()
    if not raw:
        return default
    return raw in {"y", "yes"}


def save_manifest(
    output_dir: Path,
    selected: list[Network],
    start: tuple[int, int],
    end: tuple[int, int],
    files: list[RemoteFile],
) -> None:
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "source": HISTORICAL_BASE_URL,
        "interval": {"start": f"{start[0]:04d}-{start[1]:02d}", "end": f"{end[0]:04d}-{end[1]:02d}"},
        "networks": [n.__dict__ for n in selected],
        "files": [
            {
                "network_id": f.network_id,
                "year": f.year,
                "month": f.month,
                "name": f.name,
                "size": f.size,
                "mtime": f.mtime,
                "url": f.url,
            }
            for f in files
        ],
    }
    with (output_dir / "download_manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download CityBikes historical Parquet data interactively.")
    parser.add_argument("--output", type=Path, default=Path("citybikes_data"), help="Output directory")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--overwrite", action="store_true", help="Redownload files that already exist")
    args = parser.parse_args()

    print("CityBikes Historical Data Downloader")
    print("=====================================")
    if args.insecure:
        print("WARNING: TLS certificate verification is disabled for this run.")

    session = make_session(args.insecure)
    try:
        print("Loading the CityBikes network catalogue...")
        networks = load_networks(session)
        print(f"Loaded {len(networks)} networks.")

        selected = choose_networks(networks)

        print("\nChecking available historical data...")

        available_months = get_available_months(
            session,
            selected
        )

        start, end = choose_interval(
            available_months
        )
        
        output_dir = args.output.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        print("\nDISCOVERY")
        remote_files = discover_files(session, selected, start, end)
        if not remote_files:
            print("No matching historical files were found for that selection and interval.")
            return 1

        by_network: dict[str, list[RemoteFile]] = {n.id: [] for n in selected}
        for remote in remote_files:
            by_network.setdefault(remote.network_id, []).append(remote)

        print("\nFiles found:")
        for network in selected:
            items = by_network.get(network.id, [])
            total_size = sum(item.size or 0 for item in items)
            print(f"  {network.label}: {len(items)} file(s), {human_size(total_size)}")

        if not yes_no("\nDownload these files?", default=True):
            print("Cancelled before download.")
            return 0

        print("\nDOWNLOAD")
        downloaded_paths: dict[str, list[Path]] = {n.id: [] for n in selected}
        failures: list[str] = []
        downloaded_count = 0
        skipped_count = 0

        for remote in remote_files:
            target = output_dir / remote.network_id / remote.name
            try:
                status = download_file(session, remote, target, args.overwrite)
                downloaded_paths.setdefault(remote.network_id, []).append(target)
                if status == "downloaded":
                    downloaded_count += 1
                else:
                    skipped_count += 1
                    print(f"  Skipped existing file: {target.name}")
            except Exception as exc:
                failures.append(f"{remote.name}: {exc}")
                print(f"  ERROR: {remote.name}: {exc}")

        save_manifest(output_dir, selected, start, end, remote_files)

        merge_mode = "none"
        if any(downloaded_paths.values()) and yes_no("\nMerge downloaded monthly files?", default=False):
            print("  1. One merged file per network")
            print("  2. One merged file containing all selected networks")
            raw_mode = input("Choose 1 or 2 [1]: ").strip() or "1"
            merge_mode = "all" if raw_mode == "2" else "per-network"

        if merge_mode == "per-network":
            print("\nMERGE")
            start_text = f"{start[0]:04d}{start[1]:02d}"
            end_text = f"{end[0]:04d}{end[1]:02d}"
            for network in selected:
                paths = [p for p in downloaded_paths.get(network.id, []) if p.exists()]
                if not paths:
                    continue
                output = output_dir / f"{network.id}_{start_text}_{end_text}.parquet"
                try:
                    print(f"  Merging {len(paths)} file(s) into {output.name}...")
                    merge_parquet_files(paths, output)
                    print(f"  Created {output}")
                except Exception as exc:
                    print(f"  Merge failed for {network.id}: {exc}")

        elif merge_mode == "all":
            print("\nMERGE")
            all_paths = [p for paths in downloaded_paths.values() for p in paths if p.exists()]
            start_text = f"{start[0]:04d}{start[1]:02d}"
            end_text = f"{end[0]:04d}{end[1]:02d}"
            output = output_dir / f"citybikes_selected_{start_text}_{end_text}.parquet"
            try:
                print(f"  Merging {len(all_paths)} file(s) into {output.name}...")
                merge_parquet_files(all_paths, output)
                print(f"  Created {output}")
            except Exception as exc:
                print(f"  Merge failed: {exc}")

        print("\nSUMMARY")
        print(f"  Downloaded: {downloaded_count}")
        print(f"  Already present: {skipped_count}")
        print(f"  Failed: {len(failures)}")
        print(f"  Output folder: {output_dir}")
        print(f"  Manifest: {output_dir / 'download_manifest.json'}")
        if failures:
            print("\nFailures:")
            for failure in failures:
                print(f"  - {failure}")
            return 2
        return 0

    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
