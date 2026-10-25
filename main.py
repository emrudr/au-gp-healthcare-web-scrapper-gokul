import os
import sys
import json
import time
import argparse
import asyncio
from typing import List, Dict, Any, Set
import pandas as pd

from src.scraper import HotDocScraper
from src.cleaner import clean_dataset, export_data, export_csv_quick


async def run_pipeline(
    targets_file: str,
    hub_filter: str = None,
    limit: int = None,
    hub_concurrency: int = 6,
    worker_concurrency: int = 20,
    force: bool = False,
    fresh: bool = False,
):
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "australian_gp_directory.csv")

    # Load targets
    if not os.path.exists(targets_file):
        print(f"Error: Targets file not found at {targets_file}")
        sys.exit(1)

    with open(targets_file, "r", encoding="utf-8") as f:
        targets: List[Dict[str, str]] = json.load(f)

    # Optional filter for testing a specific hub
    if hub_filter:
        targets = [
            t for t in targets
            if hub_filter.lower() in t.get("url_slug", "").lower()
            or hub_filter.lower() in t.get("reference_label", "").lower()
        ]
        if not targets:
            print(f"No hub matching '{hub_filter}' found in {targets_file}")
            sys.exit(1)

    if limit and limit > 0:
        targets = targets[:limit]

    # Load existing records so we never lose prior progress unless fresh is requested
    all_records: List[Dict[str, Any]] = []
    completed_hubs: Set[str] = set()

    if os.path.exists(csv_path) and not fresh:
        try:
            existing_df = pd.read_csv(csv_path)
            if not existing_df.empty:
                all_records = existing_df.fillna("").to_dict(orient="records")
                completed_hubs = set(existing_df["search_hub_reference"].dropna().unique())
        except Exception:
            pass

    start_time = time.time()
    save_lock = asyncio.Lock()

    async with HotDocScraper(concurrency=worker_concurrency, headless=True) as scraper:
        # Pre-seed cache from existing records to eliminate duplicate clinic requests
        if all_records:
            scraper.seed_cache(all_records)

        hub_sem = asyncio.Semaphore(hub_concurrency)

        async def process_hub(target: Dict[str, str]):
            ref_label = target.get("reference_label", target.get("url_slug", ""))
            if ref_label in completed_hubs and not force:
                print(f"[SKIP] {ref_label} (already in directory)", flush=True)
                return

            async with hub_sem:
                print(f"[START] {ref_label}", flush=True)
                hub_records = await scraper.scrape_hub(target)

                async with save_lock:
                    all_records.extend(hub_records)
                    completed_hubs.add(ref_label)
                    # Non-blocking quick interim CSV save in worker thread
                    df_interim, _, _, _ = clean_dataset(all_records)
                    await asyncio.to_thread(export_csv_quick, df_interim, output_dir=output_dir)

                print(f"[DONE] {ref_label} (found {len(hub_records)} practitioners)", flush=True)

        hub_tasks = [process_hub(t) for t in targets]
        await asyncio.gather(*hub_tasks)

    # Final clean, sort, and complete export (both XLSX and CSV)
    df_cleaned, raw_count, dup_count, clean_count = clean_dataset(all_records)
    export_info = await asyncio.to_thread(export_data, df_cleaned, output_dir=output_dir)

    total_time = time.time() - start_time
    minutes = int(total_time // 60)
    seconds = total_time % 60

    print("\n" + "=" * 60)
    print("SCRAPING SUMMARY")
    print("=" * 60)
    print(f"Total raw records extracted:  {raw_count}")
    print(f"Duplicates removed:           {dup_count}")
    print(f"Final unique doctor count:    {clean_count}")
    print(f"Total time taken:             {minutes}m {seconds:.1f}s ({total_time:.1f} seconds)")
    print(f"Excel file saved to:          {export_info['excel_path']}")
    print(f"CSV file saved to:            {export_info['csv_path']}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="HotDoc Australian GP Directory Scraper (Optimized)")
    parser.add_argument(
        "--hub",
        type=str,
        default=None,
        help="Optional hub url_slug or name to scrape (e.g. 'robina-QLD-4226')"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of hubs to scrape"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Number of concurrent hubs to process in parallel (default: 6)"
    )
    parser.add_argument(
        "--profile-concurrency",
        type=int,
        default=20,
        help="Number of concurrent clinic profile pages to fetch (default: 20)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-scrape hubs even if they already exist in the output directory"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Start completely fresh, ignoring existing output CSV"
    )
    parser.add_argument(
        "--targets",
        type=str,
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "targets.json"),
        help="Path to targets.json"
    )
    args = parser.parse_args()

    asyncio.run(
        run_pipeline(
            args.targets,
            hub_filter=args.hub,
            limit=args.limit,
            hub_concurrency=args.workers,
            worker_concurrency=args.profile_concurrency,
            force=args.force,
            fresh=args.fresh,
        )
    )


if __name__ == "__main__":
    main()
