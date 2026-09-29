"""Streamlit dashboard for viewing scanner results (reads deals.db).

Run with:
    streamlit run dashboard.py
"""
from __future__ import annotations

import html
import os
import sqlite3

import pandas as pd
import streamlit as st

DB_PATH = "deals.db"

st.set_page_config(
    page_title="Pokemon Deal Scanner",
    page_icon="🃏",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.tdcard { background:#1e1e2e; border-left:4px solid #f38ba8; padding:12px 14px;
          border-radius:6px; margin-bottom:6px; color:#cdd6f4; }
.tdcard .name { font-weight:700; font-size:1em; }
.tdcard .meta { font-size:0.82em; color:#a6adc8; margin-top:4px; line-height:1.7; }
.tdcard .pct  { font-size:1.2em; font-weight:700; color:#f38ba8; }
</style>
""", unsafe_allow_html=True)

_COND_DOT = {
    "NM": "🟢", "LP": "🟡", "MP": "🟠", "HP": "🔴",
    "DMG": "⚫", "Graded": "🏆", "Unknown": "⚪",
}


def _deal_flame(pct: float, is_deal: bool) -> str:
    """One flame per deal, more for deeper discounts."""
    if not is_deal or pd.isna(pct):
        return ""
    if pct >= 70:
        return "🔥🔥🔥"
    if pct >= 50:
        return "🔥🔥"
    return "🔥"


def _grade_cond_label(row: pd.Series) -> str:
    """Single display string combining grade or condition info."""
    if row.get("is_graded"):
        parts = [p for p in (row.get("grading_company"), row.get("grade"))
                 if isinstance(p, str) and p]
        return "🏆 " + (" ".join(parts) or "Graded")
    cond = row.get("condition") or "Unknown"
    return f"{_COND_DOT.get(cond, '⚪')} {cond}"


# ── data ──────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_data() -> pd.DataFrame:
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM listings ORDER BY scraped_at DESC LIMIT 5000", conn
    )
    conn.close()
    if df.empty:
        return df

    # Ensure newer columns exist even on older databases
    for col in ("grading_company", "grade", "set_name", "card_number",
                "shipping", "total_price", "is_graded"):
        if col not in df.columns:
            df[col] = None

    df["scraped_at"] = pd.to_datetime(df["scraped_at"], errors="coerce", utc=True)
    df["is_deal"]    = df["is_deal"].fillna(0).astype(bool)
    df["is_graded"]  = df["is_graded"].fillna(0).astype(bool)
    for col in ("ebay_price", "shipping", "total_price", "tcg_market_price",
                "fair_value", "discount_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # Rows saved before shipping was tracked: total = item price
    df["total_price"] = df["total_price"].fillna(df["ebay_price"])

    df["grade_cond"] = df.apply(_grade_cond_label, axis=1)
    return df


# ── page ──────────────────────────────────────────────────────────────────────

def main() -> None:
    st.title("🃏 Pokemon Card Deal Scanner")

    df = load_data()

    if df.empty:
        st.info(
            "No data yet — start the scanner first:\n\n"
            "```\npython main.py\n```\n\n"
            "Then refresh this page after a minute."
        )
        st.stop()

    deals_df = df[df["is_deal"]]

    # ── summary metrics ───────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Scanned", f"{len(df):,}")
    c2.metric("Deals Found",   f"{len(deals_df):,}")
    c3.metric("Graded Deals",  f"{deals_df['is_graded'].sum():,}")
    c4.metric(
        "Avg Deal Discount",
        f"{deals_df['discount_pct'].mean():.1f}%" if len(deals_df) else "—",
    )
    last_ts = df["scraped_at"].max()
    c5.metric("Last Updated", last_ts.strftime("%b %d %H:%M") if pd.notna(last_ts) else "—",
              help="Times are UTC")

    # ── top deals callout ─────────────────────────────────────────────────────
    if len(deals_df):
        with st.expander("🔥 Top 5 Deals Right Now", expanded=True):
            top = deals_df.nlargest(5, "discount_pct")
            cols = st.columns(len(top))
            for col, (_, row) in zip(cols, top.iterrows()):
                ship = "+ shipping" if pd.isna(row["shipping"]) else "incl. shipping"
                with col:
                    st.markdown(
                        f'<div class="tdcard">'
                        f'<div class="name">{html.escape(row["parsed_card_name"] or "Unknown")}</div>'
                        f'<div class="meta">'
                        f'{html.escape(row["grade_cond"])}<br>'
                        f'eBay: <b>${row["total_price"]:.2f}</b> {ship}<br>'
                        f'Fair Value: ${row["fair_value"]:.2f}<br>'
                        f'NM Price: ${row["tcg_market_price"]:.2f}'
                        f'</div>'
                        f'<div class="pct">▼ {row["discount_pct"]:.0f}% off</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                    st.link_button("View on eBay →", row["url"], width="stretch")

    st.divider()

    # ── sidebar filters ───────────────────────────────────────────────────────
    with st.sidebar:
        st.header("🔍 Filters")

        deals_only = st.toggle("🔥 Deals only", value=True)

        card_type = st.radio(
            "Card type", ["All", "Raw only", "Graded only"], horizontal=True
        )

        # Condition filter only makes sense for raw cards
        all_conds = sorted(
            [c for c in df["condition"].dropna().unique() if c != "Graded"]
        )
        sel_conds = st.multiselect("Condition (raw)", all_conds, default=all_conds)

        # Grade filter only makes sense for graded cards
        graded_df = df[df["is_graded"]]
        all_companies = sorted(graded_df["grading_company"].dropna().unique().tolist())
        sel_companies = st.multiselect("Grading company", all_companies, default=all_companies)

        def _grade_key(g: str) -> float:
            try:
                return float(g)
            except (TypeError, ValueError):
                return -1.0   # "Authentic" etc. sort last

        all_grades = sorted(graded_df["grade"].dropna().unique().tolist(),
                            key=_grade_key, reverse=True)
        sel_grades = st.multiselect("Grade", all_grades, default=all_grades)

        st.divider()

        min_disc = st.slider("Min discount %", 0, 100, 0, step=5)

        st.write("eBay total, price + shipping ($)")
        pc1, pc2 = st.columns(2)
        with pc1:
            min_price = st.number_input("Min", 0.0, value=0.0, step=5.0, format="%.0f",
                                        label_visibility="collapsed")
        with pc2:
            max_price_cap = float(df["total_price"].max(skipna=True) or 500)
            max_price = st.number_input("Max", 0.0, value=max_price_cap, step=5.0, format="%.0f",
                                        label_visibility="collapsed")

        card_search = st.text_input("🔎 Card name contains")

        all_conf = sorted(df["match_confidence"].dropna().unique().tolist())
        sel_conf = st.multiselect("Match confidence", all_conf, default=all_conf)

        st.divider()
        st.subheader("Sort")
        sort_col = st.selectbox(
            "Sort by",
            ["discount_pct", "total_price", "tcg_market_price", "fair_value", "scraped_at"],
            format_func=lambda x: {
                "discount_pct":     "Discount %",
                "total_price":      "eBay Total",
                "tcg_market_price": "NM Market Price",
                "fair_value":       "Fair Value",
                "scraped_at":       "Date Found",
            }.get(x, x),
        )
        sort_asc = st.toggle("Ascending", value=False)

        st.divider()
        if st.button("🔄 Refresh", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        st.caption("Data caches for 30 s. Click Refresh for latest.")

    # ── apply filters ─────────────────────────────────────────────────────────
    filt = df.copy()

    if deals_only:
        filt = filt[filt["is_deal"]]

    if card_type == "Raw only":
        filt = filt[~filt["is_graded"]]
    elif card_type == "Graded only":
        filt = filt[filt["is_graded"]]

    # Condition filter applies to raw cards only; grade filters to graded cards only.
    # Each mask is built from the current frame so the indexes always line up.
    if sel_conds:
        filt = filt[filt["is_graded"] | filt["condition"].isin(sel_conds)]
    if sel_companies:
        filt = filt[~filt["is_graded"] | filt["grading_company"].isin(sel_companies)]
    if sel_grades:
        filt = filt[~filt["is_graded"] | filt["grade"].isin(sel_grades)]

    if min_disc:
        filt = filt[filt["discount_pct"] >= min_disc]
    filt = filt[(filt["total_price"] >= min_price) & (filt["total_price"] <= max_price)]
    if card_search:
        filt = filt[filt["parsed_card_name"].str.contains(card_search, case=False, na=False)]
    if sel_conf:
        filt = filt[filt["match_confidence"].isin(sel_conf)]

    filt = filt.sort_values(sort_col, ascending=sort_asc, na_position="last")

    # ── table ─────────────────────────────────────────────────────────────────
    st.subheader(f"Listings — {len(filt):,} results")

    if filt.empty:
        st.info("No listings match the current filters.")
        st.stop()

    display = filt[[
        "parsed_card_name", "set_name", "card_number", "grade_cond", "ebay_price",
        "shipping", "total_price", "tcg_market_price", "fair_value", "discount_pct",
        "match_confidence", "url", "scraped_at",
    ]].copy()
    # Name-only matches have no set or number; show a dash instead of "None".
    for col in ("set_name", "card_number"):
        display[col] = display[col].replace({"None": None, "": None}).fillna("—")
    display.insert(0, "Deal", [_deal_flame(p, d) for p, d in zip(filt["discount_pct"], filt["is_deal"])])

    st.dataframe(
        display,
        column_config={
            "Deal":             st.column_config.TextColumn("",              width=40),
            "parsed_card_name": st.column_config.TextColumn("Card Name",    width="medium"),
            "set_name":         st.column_config.TextColumn("Set",          width="medium"),
            "card_number":      st.column_config.TextColumn("#",            width=60),
            "grade_cond":       st.column_config.TextColumn("Grade / Cond", width=110),
            "ebay_price":       st.column_config.NumberColumn("Price",       format="$%.2f", width="small"),
            "shipping":         st.column_config.NumberColumn("Shipping",    format="$%.2f", width="small",
                                                              help="Blank = calculated at checkout"),
            "total_price":      st.column_config.NumberColumn("Total",       format="$%.2f", width="small"),
            "tcg_market_price": st.column_config.NumberColumn("NM Market",   format="$%.2f", width="small"),
            "fair_value":       st.column_config.NumberColumn("Fair Value",  format="$%.2f", width="small"),
            "discount_pct":     st.column_config.NumberColumn("Discount",    format="%.1f%%", width="small"),
            "match_confidence": st.column_config.TextColumn("Match",         width="small"),
            "url":              st.column_config.LinkColumn("eBay Link",     width="medium"),
            "scraped_at":       st.column_config.DatetimeColumn("Found (UTC)", format="MMM D HH:mm", width="small"),
        },
        hide_index=True,
        width="stretch",
        height=560,
    )

    # ── downloads ─────────────────────────────────────────────────────────────
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "⬇ Download filtered CSV",
            filt.drop(columns=["id", "grade_cond"], errors="ignore").to_csv(index=False),
            file_name="filtered_listings.csv",
            mime="text/csv",
            width="stretch",
        )
    with dl2:
        deals_export = filt[filt["is_deal"]].drop(columns=["id", "grade_cond"], errors="ignore")
        st.download_button(
            "⬇ Download deals only CSV",
            deals_export.to_csv(index=False),
            file_name="deals_export.csv",
            mime="text/csv",
            width="stretch",
        )


if __name__ == "__main__":
    main()
