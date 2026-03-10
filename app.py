from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from pathlib import Path

import altair as alt
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from supabase import Client, create_client


DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

ANWB_BASE_URL = "https://www.anwb.nl"


def get_latest_excel_file() -> Path | None:
    """Zoek het meest recent opgeslagen Excel-bestand in de data-map."""
    excel_files = list(DATA_DIR.glob("*.xlsx"))
    if not excel_files:
        return None
    return max(excel_files, key=lambda p: p.stat().st_mtime)


def save_uploaded_file(uploaded_file) -> Path:
    """Sla het geüploade bestand op in de data-map en retourneer het pad."""
    safe_name = os.path.basename(uploaded_file.name)
    target_path = DATA_DIR / safe_name
    with open(target_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return target_path


def load_excel(path: Path) -> pd.DataFrame:
    """Laad een Excel-bestand in een DataFrame."""
    return pd.read_excel(path, engine="openpyxl")


@st.cache_resource
def get_supabase() -> Client | None:
    """
    Maak een Supabase-client op basis van Streamlit secrets.
    Verwacht in .streamlit/secrets.toml:

    [supabase]
    url = "https://...supabase.co"
    key = "YOUR_KEY_HERE"
    """
    try:
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["key"]
    except Exception:
        return None
    if not url or not key:
        return None
    return create_client(url, key)


def _normalize_name(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_product_id(value: object) -> str:
    """
    Normaliseer product id's (Excel kan ints/floats/strings bevatten).
    We willen uiteindelijk een string met digits (bijv. "163997").
    """
    if value is None:
        return ""

    # Pandas kan NaN/NA geven
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    s = str(value).strip()
    if not s:
        return ""

    # Veelvoorkomend: 163997.0 uit Excel
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".", 1)[0]

    # Houd alleen digits over (voor de zekerheid)
    digits = re.sub(r"\D+", "", s)
    return digits


def _category_codes_to_url(value: object) -> str:
    """
    Zet de Excel-waarde uit kolom 'Categorie pad codes' om naar een ANWB categorie-URL.
    Verwacht bijv. (varianten):
    - '374/reisartikelen/reis-elektronica/powerbanks'
    - '/webwinkel/c/374/reisartikelen/reis-elektronica/powerbanks'
    - 'https://www.anwb.nl/webwinkel/c/374/reisartikelen/reis-elektronica/powerbanks'
    - '374'
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    s = str(value).strip()
    if not s:
        return ""

    # Strip domein indien meegegeven
    s = re.sub(r"^https?://(www\.)?anwb\.nl", "", s, flags=re.IGNORECASE).strip()

    # Houd alleen het pad over
    s = s.split("?", 1)[0].strip()

    if s.startswith("/webwinkel/c/"):
        path = s
    elif s.startswith("webwinkel/c/"):
        path = "/" + s
    elif s.startswith("c/"):
        path = "/webwinkel/" + s
    elif re.fullmatch(r"\d+", s):
        path = f"/webwinkel/c/{s}"
    else:
        # Veelvoorkomend: '374/...'
        if re.match(r"^\d+/", s):
            path = "/webwinkel/c/" + s
        else:
            # Onbekend formaat
            return ""

    return f"{ANWB_BASE_URL}{path}?sortering=populair"


def _normalize_category_url(value: object) -> str:
    """
    Normaliseer een (categorie) URL of pad naar een volledige ANWB URL met sortering=populair.
    Ondersteunt:
    - volledige URL: https://www.anwb.nl/webwinkel/c/374/...
    - pad: /webwinkel/c/374/...
    - zonder leading slash: webwinkel/c/374/...
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    s = str(value).strip()
    if not s:
        return ""

    # Als het eigenlijk categorie-codes zijn, hergebruik de parser
    if "webwinkel" not in s.lower() and re.match(r"^\d+(/|$)", s.strip()):
        return _category_codes_to_url(s)

    # Strip domein indien meegegeven
    s = re.sub(r"^https?://(www\.)?anwb\.nl", "", s, flags=re.IGNORECASE).strip()
    s = s.split("?", 1)[0].strip()

    if s.startswith("/"):
        path = s
    else:
        path = "/" + s

    if not path.startswith("/webwinkel/"):
        # We verwachten categoriepagina's onder /webwinkel/...
        return ""

    return f"{ANWB_BASE_URL}{path}?sortering=populair"


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@st.cache_data(ttl=60 * 60, show_spinner=False)
def fetch_anwb_category_rankings(category_url: str) -> list[dict]:
    """
    Haal productnamen op van een ANWB categoriepagina (gesorteerd op populair) en onthoud de positie (1..n).
    Retourneert een lijst dicts: {"positie": int, "productnaam": str, "product_id": str}
    """
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
    products: list[dict] = []

    # Op ANWB product-kaarten zit vaak: <a ... id="163997" aria-label="...">
    for a in soup.select('a[href*="/webwinkel/p/"][id]'):
        product_id = _normalize_product_id(a.get("id"))

        container = a
        for _ in range(6):
            if container is None:
                break
            # In een productkaart staat de titel vaak als heading in dezelfde container.
            heading = container.find(["h1", "h2", "h3", "h4", "h5", "h6"])
            if heading and heading.get_text(strip=True):
                name = heading.get_text(" ", strip=True)
                break
            container = container.parent
        else:
            name = a.get("aria-label") or a.get_text(" ", strip=True)

        name = (name or "").strip()
        norm = _normalize_name(name)

        # Uniek maken: liefst op product_id, anders op genormaliseerde naam
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


def match_products_to_anwb(
    my_names: list[str],
    my_ids: list[str] | None,
    anwb_rankings: list[dict],
    threshold: float = 0.80,
) -> pd.DataFrame:
    anwb_id_to_pos: dict[str, int] = {}
    for r in anwb_rankings:
        pid = _normalize_product_id(r.get("product_id"))
        if pid:
            anwb_id_to_pos[pid] = int(r["positie"])

    anwb_names = [r["productnaam"] for r in anwb_rankings]
    anwb_norm_to_pos: dict[str, int] = {
        _normalize_name(r["productnaam"]): int(r["positie"]) for r in anwb_rankings
    }

    rows: list[dict] = []
    anwb_norms = [(_normalize_name(n), n) for n in anwb_names]

    if my_ids is None:
        my_ids = [""] * len(my_names)

    for raw, raw_id in zip(my_names, my_ids, strict=False):
        my_raw = "" if raw is None else str(raw)
        my_norm = _normalize_name(my_raw)
        my_id = _normalize_product_id(raw_id)

        found = False
        pos: int | None = None

        # 1) Eerst exact matchen op product-id (meest betrouwbaar)
        if my_id and my_id in anwb_id_to_pos:
            found = True
            pos = anwb_id_to_pos[my_id]
        # 2) Exacte naam-match (na normalisatie)
        elif my_norm and my_norm in anwb_norm_to_pos:
            found = True
            pos = anwb_norm_to_pos[my_norm]
        # 3) Fuzzy naam-match
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
                "Mijn Productnaam": my_raw,
                "Gevonden op ANWB (Ja/Nee)": "Ja" if found else "Nee",
                "Positie op ANWB": pos,
            }
        )

    return pd.DataFrame(rows)


def _style_position_column(df: pd.DataFrame):
    def color_for_pos(pos: object) -> str:
        if pos is None:
            return "background-color: #f8d7da; color: #000000; font-weight: 600; border: 1px solid #e0b4b4;"  # rood
        try:
            if pd.isna(pos):
                    return "background-color: #f8d7da; color: #000000; font-weight: 600; border: 1px solid #e0b4b4;"
        except Exception:
            pass
        try:
            p = int(pos)
        except Exception:
            return "background-color: #f8d7da; color: #000000; font-weight: 600; border: 1px solid #e0b4b4;"

        if 1 <= p <= 3:
            return "background-color: #c8e6c9; color: #000000; font-weight: 700; border: 1px solid #97c79b;"  # groen
        if 4 <= p <= 10:
            return "background-color: #ffe5b4; color: #000000; font-weight: 600; border: 1px solid #f0b96a;"  # oranje
        return "background-color: #f8d7da; color: #000000; font-weight: 600; border: 1px solid #e0b4b4;"  # rood

    styler = df.style.apply(
        lambda row: [""] * (df.columns.get_loc("Positie op ANWB"))
        + [color_for_pos(row["Positie op ANWB"])]
        + [""] * (len(df.columns) - df.columns.get_loc("Positie op ANWB") - 1),
        axis=1,
    )
    return styler


def _append_anwb_history(found_total: int, avg_rank: float | None, top5_count: int) -> None:
    """Legacy-functie (geen CSV meer nodig); behouden voor compatibiliteit."""
    return None


def _append_anwb_product_history(results_df: pd.DataFrame) -> None:
    """
    Schrijf per product de huidige positie weg naar Supabase tabel 'rankings'.

    Verwacht dat de tabel (Postgres) ongeveer zo is gedefinieerd:

        create table public.rankings (
          id uuid primary key default gen_random_uuid(),
          run_timestamp timestamptz not null,
          marketplace text not null,
          category text,
          product_name text,
          found boolean,
          position integer
        );
    """
    if results_df.empty:
        return

    client = get_supabase()
    if client is None:
        st.warning("Supabase-configuratie niet gevonden; historie wordt niet opgeslagen.")
        return

    now = pd.Timestamp.now(tz="UTC").isoformat()
    payload: list[dict] = []

    for _, row in results_df.iterrows():
        try:
            pos_val = row.get("Positie op ANWB")
        except Exception:
            pos_val = None
        position: int | None
        if pd.isna(pos_val):
            position = None
        else:
            try:
                position = int(pos_val)
            except Exception:
                position = None

        payload.append(
            {
                "run_timestamp": now,
                "marketplace": "ANWB",
                "category": str(row.get("Categorie", "")),
                "product_name": str(row.get("Mijn Productnaam", "")),
                "found": str(row.get("Gevonden op ANWB (Ja/Nee)", "")).strip().lower()
                == "ja",
                "position": position,
            }
        )

    try:
        client.table("rankings").insert(payload).execute()
    except Exception as exc:
        st.error(f"Kon rankings niet naar Supabase schrijven: {exc}")


def _render_anwb_ranking(df: pd.DataFrame) -> None:
    # Vereiste kolommen volgens jouw bestand (case-insensitive match, maar exacte namen zijn: Artikel, Product id, Categorie pad codes, Categorie)
    required_name_col = "artikel"
    required_category_col = "categorie pad codes"
    required_group_col = "categorie"
    optional_url_col = "url"
    id_col_candidates = ["product id", "product_id", "productid", "id"]

    df_cols_lower = {str(c).strip().lower(): c for c in df.columns}
    if required_name_col not in df_cols_lower:
        st.error(
            "Ik kan de kolom met productnamen niet vinden. "
            "Verwacht een kolom met naam `Artikel` in je Excel."
        )
        st.stop()

    product_col = df_cols_lower[required_name_col]

    url_col = df_cols_lower.get(optional_url_col)
    category_col = df_cols_lower.get(required_category_col)

    if url_col is None and category_col is None:
        st.error(
            "Ik kan geen kolom vinden om de ANWB categorie-URL uit te halen. "
            "Voeg een kolom `URL` toe (aanbevolen) of gebruik `Categorie pad codes`."
        )
        st.stop()

    # Groeperen van resultaten gebeurt altijd op de Excel-kolom 'Categorie'
    if required_group_col not in df_cols_lower:
        st.error(
            "Ik kan de kolom voor categorie-inzicht niet vinden. "
            "Verwacht een kolom met naam `Categorie` in je Excel."
        )
        st.stop()

    group_col = df_cols_lower[required_group_col]

    product_id_col = None
    for cand in id_col_candidates:
        if cand in df_cols_lower:
            product_id_col = df_cols_lower[cand]
            break

    if product_id_col is None:
        st.warning(
            "Geen product-id kolom gevonden (bijv. `product id`). "
            "Ik match nu alleen op (fuzzy) productnaam."
        )

    threshold = st.slider(
        "Match flexibiliteit (fuzzy threshold)",
        min_value=0.60,
        max_value=0.95,
        value=0.80,
        step=0.01,
        help="Hoger = strenger matchen, lager = flexibeler matchen.",
    )

    if st.button("Check ANWB Ranking (ANWB)", type="primary"):
        # 1) Kijk eerst of er al data voor vandaag in Supabase staat. Zo ja, hergebruik die.
        client = get_supabase()
        results_df: pd.DataFrame | None = None
        scanned_total: int | None = None

        if client is not None:
            try:
                now = pd.Timestamp.now(tz="UTC")
                start = now.normalize().isoformat()
                end = (now.normalize() + pd.Timedelta(days=1)).isoformat()
                resp_existing = (
                    client.table("rankings")
                    .select("*")
                    .eq("marketplace", "ANWB")
                    .gte("run_timestamp", start)
                    .lt("run_timestamp", end)
                    .execute()
                )
                existing = resp_existing.data or []
                if existing:
                    hist_df = pd.DataFrame(existing)
                    # Bouw een results_df die hetzelfde formaat heeft als bij live-scrapen
                    results_df = pd.DataFrame(
                        {
                            "Categorie": hist_df.get("category", ""),
                            "Mijn Productnaam": hist_df.get("product_name", ""),
                            "Gevonden op ANWB (Ja/Nee)": hist_df.get("found", False).map(
                                {True: "Ja", False: "Nee"}
                            ),
                            "Positie op ANWB": hist_df.get("position"),
                        }
                    )
                    scanned_total = None
                    st.info(
                        "Er zijn al ANWB-rankings voor vandaag in Supabase gevonden; "
                        "die data wordt nu opnieuw gebruikt (er wordt niet opnieuw gescrapet)."
                    )
            except Exception as exc:
                st.warning(f"Kon bestaande data voor vandaag niet controleren: {exc}")

        # 2) Als er geen data voor vandaag is, scrapen we zoals voorheen en schrijven we naar Supabase.
        if results_df is None:
            # Bouw per categorie-url de ranking op en match per rij in die categorie.
            my_names_series = df[product_col].astype(str)
            my_ids_series = df[product_id_col] if product_id_col is not None else None
            my_group_series = (
                df[group_col].astype(str) if group_col is not None else pd.Series([""] * len(df))
            )
            my_urls_series = df[url_col] if url_col is not None else None
            my_cats_series = df[category_col] if category_col is not None else None

            unique_cat_urls: dict[str, str] = {}
            if my_urls_series is not None:
                for raw_url in my_urls_series.tolist():
                    url = _normalize_category_url(raw_url)
                    if not url:
                        continue
                    unique_cat_urls[url] = url
            else:
                for raw_cat in (my_cats_series.tolist() if my_cats_series is not None else []):
                    url = _category_codes_to_url(raw_cat)
                    if not url:
                        continue
                    unique_cat_urls[url] = url

            if not unique_cat_urls:
                st.error(
                    "Ik kan geen geldige ANWB categorie-URL maken uit je Excel. "
                    "Aanpak: voeg een kolom `URL` toe met de juiste ANWB categoriepagina's (bijv. "
                    "`https://www.anwb.nl/webwinkel/c/374/reisartikelen/reis-elektronica/powerbanks`)."
                )
                return

            rankings_by_url: dict[str, list[dict]] = {}
            with st.spinner("ANWB categoriepagina('s) scannen..."):
                for url in unique_cat_urls.values():
                    try:
                        rankings_by_url[url] = fetch_anwb_category_rankings(url)
                    except Exception as exc:
                        rankings_by_url[url] = []
                        st.warning(f"Kon categorie niet ophalen/parsen: {url} ({exc})")

            # Match per rij tegen de ranking van zijn/haar categorie
            out_rows: list[dict] = []
            for idx in range(len(df)):
                row_name = my_names_series.iloc[idx]
                row_id = my_ids_series.iloc[idx] if my_ids_series is not None else None
                row_group = my_group_series.iloc[idx] if idx < len(my_group_series) else ""
                if my_urls_series is not None:
                    row_url = _normalize_category_url(my_urls_series.iloc[idx])
                else:
                    row_cat = my_cats_series.iloc[idx] if my_cats_series is not None else None
                    row_url = _category_codes_to_url(row_cat)
                anwb_rankings = rankings_by_url.get(row_url, [])

                if not anwb_rankings:
                    out_rows.append(
                        {
                            "Categorie": str(row_group),
                            "Mijn Productnaam": str(row_name),
                            "Gevonden op ANWB (Ja/Nee)": "Nee",
                            "Positie op ANWB": None,
                        }
                    )
                    continue

                matched_df = match_products_to_anwb(
                    [str(row_name)],
                    [row_id] if row_id is not None else None,
                    anwb_rankings,
                    threshold=threshold,
                )
                row_out = matched_df.iloc[0].to_dict()
                row_out["Categorie"] = str(row_group)
                out_rows.append(row_out)

            results_df = pd.DataFrame(out_rows)
            scanned_total = sum(len(v) for v in rankings_by_url.values())
            st.success(
                f"Klaar. {len(rankings_by_url)} categorie(ën) gescand, {scanned_total} ANWB producten totaal."
            )

        if results_df.empty:
            st.info("Geen resultaten om te tonen.")
            return

        # Groepeer per categorie en toon per categorie een expander
        # Maak Positie op ANWB numeriek (nullable) zodat sorteren logisch werkt
        results_df["Positie op ANWB"] = pd.to_numeric(results_df["Positie op ANWB"], errors="coerce").astype("Int64")

        # Overzichtsmetrics (bovenaan)
        found_mask = results_df["Gevonden op ANWB (Ja/Nee)"] == "Ja"
        found_total = int(found_mask.sum())
        positions_found = results_df.loc[found_mask, "Positie op ANWB"].dropna()
        avg_rank = float(positions_found.mean()) if len(positions_found) else None
        top5_count = int((positions_found <= 5).sum()) if len(positions_found) else 0

        # Sla metrics op voor historische lijn-grafieken
        _append_anwb_history(found_total, avg_rank, top5_count)
        _append_anwb_product_history(results_df)

        c1, c2, c3 = st.columns(3)
        c1.metric("Totaal gevonden op ANWB", f"{found_total}")
        c2.metric("Gemiddelde ranking (gevonden)", "-" if avg_rank is None else f"{avg_rank:.2f}")
        c3.metric("Aantal producten in Top 5", f"{top5_count}")

        results_df = results_df.sort_values(
            by=["Categorie", "Positie op ANWB", "Mijn Productnaam"],
            na_position="last",
        )
        categories_raw = results_df["Categorie"].fillna("").astype(str).unique().tolist()
        category_labels = [c if c.strip() else "(Geen categorie)" for c in categories_raw]

        st.markdown("#### Categorie-inzicht")
        # Navigatiebalk met één tab per categorie (links naar rechts)
        cat_tabs = st.tabs(category_labels)

        for cat_value, label, tab in zip(categories_raw, category_labels, cat_tabs):
            with tab:
                subset = results_df[results_df["Categorie"].astype(str) == str(cat_value)]
                if subset.empty:
                    st.info("Geen producten gevonden voor deze categorie.")
                    continue

                total_cat = len(subset)
                found_cat = int((subset["Gevonden op ANWB (Ja/Nee)"] == "Ja").sum())
                top5_cat = int(
                    subset.loc[subset["Gevonden op ANWB (Ja/Nee)"] == "Ja", "Positie op ANWB"]
                    .dropna()
                    .le(5)
                    .sum()
                )

                st.write(
                    f"In de categorie **{label}** staan **{top5_cat} van de {total_cat} producten** in de top 5 "
                    f"(waarvan {found_cat} producten op de ANWB-site gevonden zijn)."
                )

                view = subset[
                    ["Mijn Productnaam", "Gevonden op ANWB (Ja/Nee)", "Positie op ANWB"]
                ].copy()
                view = view.sort_values(
                    by=["Positie op ANWB", "Mijn Productnaam"], na_position="last"
                )

                st.dataframe(
                    _style_position_column(view),
                    use_container_width=True,
                    column_config={
                        "Positie op ANWB": st.column_config.NumberColumn(
                            "Positie op ANWB",
                            help="Klik op de kolomkop om te sorteren (laagste = beste ranking).",
                            format="%d",
                        )
                    },
                )

                # Lijndiagram: historie per product in deze categorie (uit Supabase)
                client = get_supabase()
                if client is not None:
                    try:
                        resp = (
                            client.table("rankings")
                            .select("*")
                            .eq("marketplace", "ANWB")
                            .eq("category", str(cat_value))
                            .order("run_timestamp")
                            .execute()
                        )
                        data = resp.data or []
                        hist_df = pd.DataFrame(data)
                    except Exception as exc:
                        st.warning(f"Kon historie niet uit Supabase ophalen: {exc}")
                        hist_df = pd.DataFrame()

                    if not hist_df.empty and "run_timestamp" in hist_df.columns:
                        hist_df["timestamp"] = pd.to_datetime(hist_df["run_timestamp"])
                        st.markdown("##### Historie posities (lager = beter)")

                        # Filter periode: 3, 7, 30 dagen
                        period_label = st.radio(
                            "Periode",
                            options=["3 dagen", "7 dagen", "30 dagen", "Alles"],
                            horizontal=True,
                            key=f"periode_radio_{cat_value}",
                        )
                        if period_label != "Alles":
                            days_map = {
                                "3 dagen": 3,
                                "7 dagen": 7,
                                "30 dagen": 30,
                            }
                            days = days_map[period_label]
                            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
                            hist_df = hist_df[hist_df["timestamp"] >= cutoff]

                        if not hist_df.empty:
                            # Voeg een datumkolom toe (zonder tijd) voor de x-as
                            hist_df["datum"] = hist_df["timestamp"].dt.date

                            product_options = sorted(hist_df["product_name"].unique().tolist())
                            default_selection = product_options  # standaard alle producten
                            selected_products = st.multiselect(
                                "Kies producten voor de lijngrafiek",
                                options=product_options,
                                default=default_selection,
                                key=f"product_multiselect_{cat_value}",
                            )
                            if selected_products:
                                hist_sel = hist_df[
                                    hist_df["product_name"].isin(selected_products)
                                    & (hist_df["found"] == True)
                                ].copy()
                                if not hist_sel.empty:
                                    hist_sel = hist_sel.sort_values("datum")
                                    chart = (
                                        alt.Chart(hist_sel)
                                        .mark_line(point=True, strokeWidth=2)
                                        .encode(
                                            x=alt.X(
                                                "datum:T",
                                                title="Datum",
                                                axis=alt.Axis(format="%d-%m-%Y"),
                                            ),
                                            y=alt.Y(
                                                "position:Q",
                                                title="Positie (lager = beter)",
                                                scale=alt.Scale(reverse=True),
                                            ),
                                            color=alt.Color(
                                                "product_name:N",
                                                title="Product",
                                            ),
                                            tooltip=[
                                                alt.Tooltip("timestamp:T", title="Datum/tijd"),
                                                alt.Tooltip(
                                                    "product_name:N", title="Product"
                                                ),
                                                alt.Tooltip(
                                                    "position:Q", title="Positie"
                                                ),
                                            ],
                                        )
                                        .properties(height=260)
                                    )
                                    st.altair_chart(chart, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Marketplace ranking dashboard", layout="wide")

    # Eenvoudige login op basis van secrets.toml
    auth_conf = st.secrets.get("auth", {})
    expected_user = auth_conf.get("username")
    expected_pass = auth_conf.get("password")

    if expected_user and expected_pass:
        if not st.session_state.get("auth_ok", False):
            st.title("Login")
            user = st.text_input("Gebruikersnaam")
            pw = st.text_input("Wachtwoord", type="password")
            if st.button("Inloggen"):
                if user == expected_user and pw == expected_pass:
                    st.session_state["auth_ok"] = True
                    try:
                        st.rerun()
                    except Exception:
                        # Voor oudere Streamlit-versies waar st.rerun nog niet bestaat,
                        # gewoon verder gaan in dezelfde run.
                        pass
                else:
                    st.error("Ongeldige inloggegevens.")
                    st.stop()
            st.stop()

    st.sidebar.title("Navigatie")
    section = st.sidebar.radio(
        "Kies een sectie",
        options=["Upload artikelen", "Ranking overzicht"],
        index=0,
    )

    st.sidebar.markdown("---")
    st.sidebar.markdown("### Upload")
    uploaded_file = st.sidebar.file_uploader(
        "Kies een Excel-bestand",
        type=["xlsx"],
    )

    current_file_path: Path | None = None

    if uploaded_file is not None:
        current_file_path = save_uploaded_file(uploaded_file)
        st.sidebar.success(f"Bestand opgeslagen als: {current_file_path.name}")
    else:
        current_file_path = get_latest_excel_file()

    if current_file_path is None:
        st.title("Marketplace ranking dashboard")
        st.info("Nog geen Excel-bestand beschikbaar. Upload een `.xlsx`-bestand via de sidebar.")
        return

    try:
        df = load_excel(current_file_path)
    except Exception as exc:  # pragma: no cover - eenvoudige foutafhandeling
        st.error(f"Het Excel-bestand kon niet worden geladen: {exc}")
        return

    if section == "Upload artikelen":
        st.title("Upload artikelen")
        st.subheader(f"Huidig bestand: {current_file_path.name}")
        st.dataframe(df, use_container_width=True)
        return

    # Ranking overzicht
    st.title("Ranking overzicht")

    if df.empty:
        st.info("Je Excel bevat geen rijen om te vergelijken.")
        return

    tabs = st.tabs(["ANWB", "Andere marktplaatsen (binnenkort)"])
    with tabs[0]:
        st.subheader("ANWB ranking check")
        _render_anwb_ranking(df)
    with tabs[1]:
        st.info("Ondersteuning voor andere marktplaatsen kan hier later worden toegevoegd.")


if __name__ == "__main__":
    main()

