from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd
import requests
from bs4 import BeautifulSoup
from supabase import create_client

from app import (
    ANWB_BASE_URL,
    _category_codes_to_url,
    _normalize_category_url,
    _normalize_product_id,
    _similarity,
)


def get_supabase_from_env():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def fetch_anwb_category_rankings_raw(category_url: str) -> List[Dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(category_url, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    seen_ids: set[str] = set()
    seen_norm_names: set[str] = set()
    products: List[Dict] = []

    for a in soup.select('a[href*="/webwinkel/p/"][id]'):
        product_id = _normalize_product_id(a.get("id"))

        container = a
        for _ in range(6):
            if container is None:
                break
            heading = container.find(["h1", "h2", "h3", "h4", "h5", "h6"])
            if heading and heading.get_text(strip=True):
                name = heading.get_text(" ", strip=True)
                break
            container = container.parent
        else:
            name = a.get("aria-label") or a.get_text(" ", strip=True)

        name = (name or "").strip()
        norm = name.lower()

        if product_id:
            if product_id in seen_ids:
                continue
            seen_ids.add(product_id)
        else:
            if not norm or norm in seen_norm_names:
                continue
            seen_norm_names.add(norm)

        if not norm:
            continue

        products.append({"product_id": product_id, "productnaam": name})

    rankings = [
        {"positie": idx + 1, "productnaam": p["productnaam"], "product_id": p["product_id"]}
        for idx, p in enumerate(products)
    ]
    return rankings


def match_products_to_anwb_cron(
    my_names: List[str],
    my_ids: List[str] | None,
    anwb_rankings: List[Dict],
    threshold: float = 0.80,
) -> pd.DataFrame:
    anwb_id_to_pos: Dict[str, int] = {}
    for r in anwb_rankings:
        pid = _normalize_product_id(r.get("product_id"))
        if pid:
            anwb_id_to_pos[pid] = int(r["positie"])

    anwb_names = [r["productnaam"] for r in anwb_rankings]
    anwb_norm_to_pos: Dict[str, int] = {
        str(r["productnaam"]).strip().lower(): int(r["positie"]) for r in anwb_rankings
    }

    rows: List[Dict] = []
    anwb_norms = [(str(n).strip().lower(), n) for n in anwb_names]

    if my_ids is None:
        my_ids = [""] * len(my_names)

    for raw, raw_id in zip(my_names, my_ids, strict=False):
        my_raw = "" if raw is None else str(raw)
        my_norm = my_raw.strip().lower()
        my_id = _normalize_product_id(raw_id)

        found = False
        pos: int | None = None

        if my_id and my_id in anwb_id_to_pos:
            found = True
            pos = anwb_id_to_pos[my_id]
        elif my_norm and my_norm in anwb_norm_to_pos:
            found = True
            pos = anwb_norm_to_pos[my_norm]
        elif my_norm:
            best_score = 0.0
            best_norm = ""
            for anwb_norm, _anwb_raw in anwb_norms:
                score = _similarity(my_norm, anwb_norm)
                if score > best_score:
                    best_score = score
                    best_norm = anwb_norm
            if best_score >= threshold and best_norm in anwb_norm_to_pos:
                found = True
                pos = anwb_norm_to_pos[best_norm]

        rows.append(
            {
                "product_name": my_raw,
                "found": found,
                "position": pos,
            }
        )

    return pd.DataFrame(rows)


def run_daily_scan() -> None:
    client = get_supabase_from_env()

    # 1) Haal producten op uit Supabase
    resp = (
        client.table("products")
        .select("*")
        .eq("marketplace", "ANWB")
        .execute()
    )
    products = pd.DataFrame(resp.data or [])
    if products.empty:
        print("Geen producten gevonden in 'products' tabel.")
        return

    # 2) Bouw per categorie-url de ranking op en match per product
    url_series = products.get("url")
    cat_codes_series = products.get("category_path_codes")

    def get_category_url(idx: int) -> str:
        if url_series is not None and not pd.isna(url_series.iloc[idx]):
            return _normalize_category_url(url_series.iloc[idx])
        if cat_codes_series is not None and not pd.isna(cat_codes_series.iloc[idx]):
            return _category_codes_to_url(cat_codes_series.iloc[idx])
        return ""

    unique_cat_urls: Dict[str, str] = {}
    for idx in range(len(products)):
        url = get_category_url(idx)
        if not url:
            continue
        unique_cat_urls[url] = url

    rankings_by_url: Dict[str, List[Dict]] = {}
    for url in unique_cat_urls.values():
        try:
            rankings_by_url[url] = fetch_anwb_category_rankings_raw(url)
        except Exception as exc:
            print(f"Kon categorie niet ophalen/parsen: {url} ({exc})")
            rankings_by_url[url] = []

    # 3) Matchen
    out_rows: List[Dict] = []
    article_series = products.get("article", pd.Series([""] * len(products)))
    product_id_series = products.get("product_id", pd.Series([""] * len(products)))
    category_series = products.get("category", pd.Series([""] * len(products)))

    for idx in range(len(products)):
        prod_name = article_series.iloc[idx]
        prod_id = product_id_series.iloc[idx]
        prod_cat = category_series.iloc[idx]
        url = get_category_url(idx)
        anwb_rankings = rankings_by_url.get(url, [])

        if not anwb_rankings:
            out_rows.append(
                {
                    "run_timestamp": datetime.now(timezone.utc).isoformat(),
                    "marketplace": "ANWB",
                    "category": str(prod_cat or ""),
                    "product_name": str(prod_name or ""),
                    "found": False,
                    "position": None,
                }
            )
            continue

        matched_df = match_products_to_anwb_cron(
            [str(prod_name)],
            [str(prod_id)],
            anwb_rankings,
            threshold=0.80,
        )
        row = matched_df.iloc[0]
        out_rows.append(
            {
                "run_timestamp": datetime.now(timezone.utc).isoformat(),
                "marketplace": "ANWB",
                "category": str(prod_cat or ""),
                "product_name": str(prod_name or ""),
                "found": bool(row["found"]),
                "position": int(row["position"]) if pd.notna(row["position"]) else None,
            }
        )

    if not out_rows:
        print("Geen resultaten om op te slaan.")
        return

    # 4) Oude records van vandaag verwijderen (voor een schone run) en nieuwe invoegen
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).replace(hour=23, minute=59, second=59)

    client.table("rankings").delete().eq("marketplace", "ANWB").gte(
        "run_timestamp", start.isoformat()
    ).lte("run_timestamp", end.isoformat()).execute()

    client.table("rankings").insert(out_rows).execute()
    print(f"{len(out_rows)} ranking-records opgeslagen in Supabase.")


if __name__ == "__main__":
    run_daily_scan()

