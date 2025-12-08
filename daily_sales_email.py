"""daily_sales_email.py

Toast 주문 데이터를 기반으로 일일/기간 매출 리포트를 HTML 이메일로 전송하는 스크립트.
요약 KPI, 전일/전주 대비 증감, 월 누적/목표 대비, 매장별 상세, 이상징후, 시각화를 포함한다.
"""
from __future__ import annotations

import argparse
import base64
import io
import logging
import smtplib
import sys
import warnings
from configparser import ConfigParser
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import matplotlib
import pandas as pd
from matplotlib import pyplot as plt
from sqlalchemy import create_engine, text

# 헤드리스 렌더링을 위해 Agg 백엔드 사용
matplotlib.use("Agg")
# 시각화 시 한글 경고(글리프 누락) 및 fillna 관련 FutureWarning 노이즈를 줄이기 위한 설정
warnings.filterwarnings(
    "ignore",
    message="Downcasting object dtype arrays on .fillna",
    category=FutureWarning,
)
warnings.filterwarnings(
    "ignore",
    message="Glyph .* missing from font",
    category=UserWarning,
)
plt.rcParams["font.family"] = ["DejaVu Sans", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False

# 지역별 매장 분류 (AB/BC/ON/기타)
REGION_BY_GUID: Dict[str, str] = {
    # Alberta
    "d4cf2241-2e17-4c70-8965-d304a7bf34ce": "AB",  # fort_mcmurray
    "a54a5a8e-af4d-4624-b216-52fbd809eed6": "AB",  # canmore
    "907a8d99-6fa4-4b3b-bd43-034dab7f1bff": "AB",  # yorkton
    "4519a914-a85a-4f1f-b2f3-38d4404db386": "AB",  # calgary_trail
    "1256aabb-89cd-4294-9723-5901635806ed": "AB",  # cochrane
    "a6c1fa52-c987-4481-a59b-b1ed61e6e856": "AB",  # london_square
    "d47326ac-8935-49f4-b56c-fa9e9f9d3e2c": "AB",  # midnapore
    "aa8ae7f6-450d-4b78-8733-0cbb2ebb94fd": "AB",  # nolan_hill
    "c2311814-428b-4c09-ae65-8ebf01b47200": "AB",  # southland_crossing
    "bf5be1b4-c779-473a-800d-40c6f9604795": "AB",  # stampede
    "e67b3b1c-7687-4bb0-abaa-f2d654de844f": "AB",  # west_edmonton
    "f26280a9-eb1e-4714-9cec-e22a7fda9b85": "AB",  # westbrook

    # British Columbia
    "972e6a34-eb59-4ad2-a02a-7c40351336e1": "BC",  # granville
    "7b05adc5-43b0-4fbe-a6fd-06cc7f7a02a6": "BC",  # hanin_village
    "73129748-eae1-4bc4-978d-00573f5e2c93": "BC",  # hastings
    "ad3858e9-add3-42db-a0f6-b5bef5dbe731": "BC",  # main_street
    "9031cf01-177d-4cf0-88c1-e19f3bb670ae": "BC",  # metrotown
    "108c2af1-2d41-4a36-8a35-24d1eb929dc7": "BC",  # poco_place
    "6c4aa6bb-dd55-4018-b8f4-3a14e74153b5": "BC",  # richmond
    "aa6aead0-72a3-46b3-b4e8-52feed9ed12a": "BC",  # robson
    "2831d0cf-4762-45e8-8bd3-f4346c9646df": "BC",  # surrey_guildford
    "b9b7e146-e360-46b0-9b9d-c59c90052772": "BC",  # yale

    # Ontario (and NB store treated as ON per provided grouping context)
    "b05659ae-6d94-41eb-923d-3a6614739a58": "ON",  # newmarket
    "d33770ba-fafc-4408-b563-bd87ca9b0387": "ON",  # moncton
    "209694b4-1a4a-461a-8196-9af71038e480": "ON",  # stouffville
    "9a929c91-a3bc-40d6-89f8-3b11d4222c76": "ON",  # liberty_village
    "7d3b9cc6-c355-48be-bf45-bdd9de658be6": "ON",  # ajax_south
    "d307657a-a8fb-4cea-8a10-fcdb464045f6": "ON",  # aurora
    "c153bc6a-e253-4170-ada1-05876f4151de": "ON",  # cummer
    "36b897be-f115-4e4d-80b2-c6c1224692bf": "ON",  # danforth
    "85815826-59e0-4366-a124-4d42a83a478c": "ON",  # don_mills
    "28153c0e-1b60-4d7e-98c4-c9d9869861f8": "ON",  # dorval_crossing
    "0c7ddf51-6960-4fe2-99ad-2000f747176f": "ON",  # downtown_markham
    "7ebcfcf7-0145-4dce-b025-18d0e9c8ccfc": "ON",  # eagles_landing
    "df7c1f69-ef49-40b2-a33e-4d3c150e2715": "ON",  # elm
    "952ed3a0-1f15-4fc1-aef2-30626c9fd672": "ON",  # hurontario_dundas
    "2c3cf4d0-8136-4986-ab72-2da7bc2d0612": "ON",  # hwy7_leslie
    "ab567fba-c3c9-44d6-946e-8f83a8b4a179": "ON",  # london_downtown
    "9d666676-487c-4236-a338-e05426411871": "ON",  # markham_unionville
    "979d83c3-0356-4289-b6e3-f2307bba5189": "ON",  # north_scarborough
    "81bdf5c3-dc2d-40e4-9640-650be7e2e63a": "ON",  # oshawa
    "36d91dd8-078f-4ddb-8546-df1215a7d450": "ON",  # ut_spadina
    "ba1c722d-cb90-4395-b2fa-37bbe4b70db9": "ON",  # yonge_bloor
    "9e764cac-94c4-4e3b-a65a-aa66dff09828": "ON",  # waterloo_central
}


# ---------------------------------------------------------------------------
# 로깅/설정 로더
# ---------------------------------------------------------------------------
def configure_logging(log_dir: Path) -> logging.Logger:
    """이메일 자동화 전용 로거를 초기화한다."""

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"daily_sales_email_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger("toast_email")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        logger.handlers.clear()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def load_config(config_path: Path) -> ConfigParser:
    """config.ini를 UTF-8로 읽는다."""

    parser = ConfigParser()
    with config_path.open("r", encoding="utf-8") as f:
        parser.read_file(f)
    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """CLI 인자를 파싱한다."""

    parser = argparse.ArgumentParser(description="Toast Daily Sales Email")
    parser.add_argument("config", help="config.ini 경로")
    parser.add_argument(
        "--date",
        dest="target_date",
        help="리포트 대상 영업일(YYYY-MM-DD). 기본값: 어제",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 데이터 로딩 / 집계 헬퍼
# ---------------------------------------------------------------------------
def _safe_lower(val: Optional[str]) -> str:
    return val.lower() if isinstance(val, str) else ""


def _categorize_order_type(val: Optional[str]) -> str:
    low = _safe_lower(val)
    if "deliver" in low:
        return "delivery"
    if "take" in low or "pickup" in low:
        return "takeout"
    if "dine" in low or "eat" in low:
        return "dinein"
    return "other"


def _categorize_platform(val: Optional[str]) -> str:
    low = _safe_lower(val)
    if "uber" in low:
        return "uber"
    if "skip" in low:
        return "skip"
    if "door" in low:
        return "doordash"
    if "kiosk" in low:
        return "kiosk"
    return "other"


def _pct_change(cur: float, prev: float) -> Optional[float]:
    if prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def _week_start_sunday(target: date) -> date:
    """주간 비교용 주 시작(일요일)을 반환."""

    return target - timedelta(days=target.weekday() + 1) if target.weekday() != 6 else target


def _read_orders(engine_url: str, start: date, end: date) -> pd.DataFrame:
    """지정 기간의 주문 데이터를 DataFrame으로 로드한다."""

    engine = create_engine(engine_url, future=True)
    query = text(
        """
        SELECT
            order_guid,
            restaurant_guid,
            business_date,
            subtotal,
            discount_total,
            tax_total,
            service_charge,
            total_amount,
            order_type,
            source,
            order_type_norm,
            platform
        FROM fact_orders
        WHERE business_date BETWEEN :start_date AND :end_date
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"start_date": start, "end_date": end})
    return df


def _with_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    numeric_cols = [
        "subtotal",
        "discount_total",
        "tax_total",
        "service_charge",
        "total_amount",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 총액이 비어 있을 경우에도 계산 가능하도록 보정한다.
    # subtotal - discount + tax + service_charge를 기본 공식으로 사용한다.
    df["total_amount"] = df["total_amount"].where(
        df["total_amount"].notna(),
        df["subtotal"].fillna(0)
        - df["discount_total"].fillna(0)
        + df["tax_total"].fillna(0)
        + df["service_charge"].fillna(0),
    )

    # subtotal이 없고 total만 존재한다면 subtotal을 total에서 세금/서비스 차감한 값으로 대략 채운다.
    df["subtotal"] = df["subtotal"].where(
        df["subtotal"].notna(),
        df["total_amount"].fillna(0)
        - df["tax_total"].fillna(0)
        - df["service_charge"].fillna(0)
        + df["discount_total"].fillna(0),
    )

    if "order_type_norm" in df.columns:
        df["order_type_norm"] = df["order_type_norm"].where(
            df["order_type_norm"].notna(), df["order_type"].apply(_categorize_order_type)
        )
    else:
        df["order_type_norm"] = df["order_type"].apply(_categorize_order_type)

    if "platform" in df.columns:
        df["platform"] = df["platform"].where(
            df["platform"].notna(), df["source"].apply(_categorize_platform)
        )
    else:
        df["platform"] = df["source"].apply(_categorize_platform)
    return df


def _summarize(df: pd.DataFrame) -> Dict[str, float]:
    if df.empty:
        return {
            "orders": 0,
            "sales": 0.0,
            "subtotal": 0.0,
            "tax": 0.0,
            "avg_order": 0.0,
            "delivery_pct": 0.0,
            "takeout_pct": 0.0,
            "dinein_pct": 0.0,
            "platform_pct": {},
        }

    orders = len(df)
    sales = df["total_amount"].fillna(0).sum()
    subtotal = df["subtotal"].fillna(0).sum()
    tax = df["tax_total"].fillna(0).sum()
    avg_order = sales / orders if orders else 0.0

    type_counts = df["order_type_norm"].value_counts(normalize=True) * 100
    delivery_pct = float(type_counts.get("delivery", 0))
    takeout_pct = float(type_counts.get("takeout", 0))
    dinein_pct = float(type_counts.get("dinein", 0))

    platform_counts = df["platform"].value_counts(normalize=True) * 100
    platform_pct = {k: float(v) for k, v in platform_counts.items()}

    return {
        "orders": orders,
        "sales": sales,
        "subtotal": subtotal,
        "tax": tax,
        "avg_order": avg_order,
        "delivery_pct": delivery_pct,
        "takeout_pct": takeout_pct,
        "dinein_pct": dinein_pct,
        "platform_pct": platform_pct,
    }


def _summarize_by_store(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    grouped = df.groupby("restaurant_guid")
    rows = []
    for store, g in grouped:
        s = _summarize(g)
        row = {
            "restaurant_guid": store,
            "orders": s["orders"],
            "sales": s["sales"],
            "avg_order": s["avg_order"],
            "delivery_pct": s["delivery_pct"],
            "takeout_pct": s["takeout_pct"],
            "dinein_pct": s["dinein_pct"],
        }
        for platform, pct in s["platform_pct"].items():
            row[f"platform_{platform}_pct"] = pct
        rows.append(row)
    return pd.DataFrame(rows)


def _merge_growth(cur: pd.DataFrame, prev: pd.DataFrame, keys: Iterable[str]) -> pd.DataFrame:
    # 이전 기간 데이터가 없으면 변화율 컬럼만 None으로 채워 반환하여 KeyError를 방지한다.
    if prev.empty:
        merged = cur.copy()
        for key in keys:
            merged[f"{key}_chg_pct"] = None
        return merged

    prev = prev.add_prefix("prev_")
    merged = cur.merge(prev, left_on="restaurant_guid", right_on="prev_restaurant_guid", how="left")
    for key in keys:
        merged[f"{key}_chg_pct"] = merged.apply(
            lambda r: _pct_change(r.get(key, 0) or 0, r.get(f"prev_{key}") or 0), axis=1
        )
    return merged


def _detect_issues(df: pd.DataFrame) -> Dict[str, int]:
    if df.empty:
        return {"discounted_orders": 0}
    discounted = df[df["discount_total"].fillna(0) > 0]
    return {"discounted_orders": len(discounted)}


# ---------------------------------------------------------------------------
# 시각화
# ---------------------------------------------------------------------------
def _fig_to_base64(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def build_sales_trend_chart(df: pd.DataFrame) -> str:
    if df.empty:
        return ""
    fig, ax = plt.subplots(figsize=(6, 3))
    agg = df.groupby("business_date")["total_amount"].sum().reset_index()
    ax.plot(agg["business_date"], agg["total_amount"], marker="o")
    ax.set_title("Sales Trend (Last 7 days)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Sales")
    ax.grid(True, linestyle="--", alpha=0.5)
    fig.autofmt_xdate()
    return _fig_to_base64(fig)


def build_platform_pie_chart(df: pd.DataFrame) -> str:
    if df.empty:
        return ""
    fig, ax = plt.subplots(figsize=(4, 4))
    counts = df["platform"].value_counts()
    ax.pie(counts, labels=counts.index, autopct="%1.1f%%", startangle=90)
    ax.set_title("Platform Share")
    return _fig_to_base64(fig)


# ---------------------------------------------------------------------------
# HTML 렌더링
# ---------------------------------------------------------------------------
def _fmt_pct(val: Optional[float]) -> str:
    return "N/A" if val is None else f"{val:.1f}%"


def build_html(
    target_date: date,
    overall: Dict[str, float],
    day_change: Dict[str, Optional[float]],
    week_change: Dict[str, Optional[float]],
    month_stats: Dict[str, float],
    monthly_target: Optional[float],
    store_table: str,
    issues: Dict[str, int],
    trend_b64: str,
    pie_b64: str,
) -> str:
    def kpi_block():
        return (
            f"<div style='margin-bottom:12px;'>"
            f"<strong>총매출:</strong> ${overall['sales']:,.2f} &nbsp;|&nbsp; "
            f"<strong>주문수:</strong> {overall['orders']} &nbsp;|&nbsp; "
            f"<strong>평균 주문액:</strong> ${overall['avg_order']:,.2f}<br/>"
            f"<strong>배달/테이크아웃/홀:</strong> {overall['delivery_pct']:.1f}% / "
            f"{overall['takeout_pct']:.1f}% / {overall['dinein_pct']:.1f}%"
            f"</div>"
        )

    def change_block(title: str, chg: Dict[str, Optional[float]]):
        return (
            f"<div style='margin-bottom:8px;'>"
            f"<strong>{title}</strong><br/>"
            f"매출: {_fmt_pct(chg.get('sales'))} / 주문수: {_fmt_pct(chg.get('orders'))} / "
            f"평균객단가: {_fmt_pct(chg.get('avg_order'))}<br/>"
            f"플랫폼(우버/스킵/도어대시): {_fmt_pct(chg.get('uber'))} / {_fmt_pct(chg.get('skip'))} / {_fmt_pct(chg.get('doordash'))}"
            f"</div>"
        )

    def month_block():
        target_txt = "N/A" if monthly_target is None else f"${monthly_target:,.0f}"
        achv = "N/A" if monthly_target in (None, 0) else f"{month_stats['sales']/monthly_target*100:.1f}%"
        return (
            f"<div style='margin-bottom:12px;'>"
            f"<strong>월 누적 매출:</strong> ${month_stats['sales']:,.2f}<br/>"
            f"<strong>예상 월말 매출:</strong> ${month_stats['forecast']:,.2f}<br/>"
            f"<strong>월 목표:</strong> {target_txt} &nbsp;|&nbsp; <strong>달성률:</strong> {achv}"
            f"</div>"
        )

    issue_block = ""
    if issues:
        issue_block = "<ul>" + "".join(
            f"<li>할인 적용 주문 수: {issues.get('discounted_orders',0)}</li>"
        ) + "</ul>"

    charts_html = ""
    if trend_b64:
        charts_html += f"<div><img src='data:image/png;base64,{trend_b64}' style='max-width:600px;'/></div>"
    if pie_b64:
        charts_html += f"<div><img src='data:image/png;base64,{pie_b64}' style='max-width:300px;'/></div>"

    return (
        f"<html><body>"
        f"<h2>BBQ Canada Daily Sales Report - {target_date}</h2>"
        f"{kpi_block()}"
        f"{change_block('전일 대비', day_change)}"
        f"{change_block('전주 동일요일 대비', week_change)}"
        f"{month_block()}"
        f"<h3>매장별 요약</h3>"
        f"{store_table}"
        f"<h3>이상 징후</h3>"
        f"{issue_block}"
        f"<h3>차트</h3>"
        f"{charts_html}"
        f"</body></html>"
    )


def build_store_table(df: pd.DataFrame, name_map: Dict[str, str]) -> str:
    if df.empty:
        return "<p>매장 데이터가 없습니다.</p>"
    cols = [
        "매장",
        "매출",
        "주문수",
        "평균객단가",
        "배달%",
        "테이크아웃%",
        "홀%",
        "우버%",
        "스킵%",
        "도어대시%",
        "전일매출변화",
        "전주동요일매출변화",
    ]
    rows = []
    for _, r in df.iterrows():
        def pct(col):
            return f"{r[col]:.1f}%" if pd.notnull(r.get(col)) else "N/A"

        rows.append(
            "<tr>"
            f"<td>{name_map.get(r['restaurant_guid'], r['restaurant_guid'])}</td>"
            f"<td>${r['sales']:,.2f}</td>"
            f"<td>{int(r['orders'])}</td>"
            f"<td>${r['avg_order']:,.2f}</td>"
            f"<td>{pct('delivery_pct')}</td>"
            f"<td>{pct('takeout_pct')}</td>"
            f"<td>{pct('dinein_pct')}</td>"
            f"<td>{pct('platform_uber_pct')}</td>"
            f"<td>{pct('platform_skip_pct')}</td>"
            f"<td>{pct('platform_doordash_pct')}</td>"
            f"<td>{_fmt_pct(r.get('sales_chg_pct'))}</td>"
            f"<td>{_fmt_pct(r.get('sales_chg_pct_week'))}</td>"
            "</tr>"
        )

    header_html = "".join(f"<th>{c}</th>" for c in cols)
    body_html = "".join(rows)
    return f"<table border='1' cellspacing='0' cellpadding='6'><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"


def build_region_tables(df: pd.DataFrame, name_map: Dict[str, str]) -> str:
    """지역(AB/BC/ON/OTHER)별 매장 테이블을 묶어 HTML로 반환한다."""

    if df.empty:
        return "<p>매장 데이터가 없습니다.</p>"

    sections: List[str] = []
    region_order = ["AB", "BC", "ON", "OTHER"]
    for region in region_order:
        region_df = df[df["region"] == region]
        if region_df.empty:
            continue
        sections.append(f"<h4>{region}</h4>" + build_store_table(region_df, name_map))

    if not sections:
        return "<p>매장 데이터가 없습니다.</p>"
    return "".join(sections)


# ---------------------------------------------------------------------------
# 이메일 전송
# ---------------------------------------------------------------------------
def send_email(
    smtp_server: str,
    smtp_port: int,
    username: str,
    sender: str,
    password: str,
    recipients: List[str],
    subject: str,
    html_body: str,
    logger: logging.Logger,
) -> None:
    """SMTP 서버를 사용하여 이메일을 발송한다."""

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)

    message.attach(MIMEText(html_body, "html", _charset="utf-8"))

    logger.info("SMTP 서버(%s:%s)에 연결합니다.", smtp_server, smtp_port)
    with smtplib.SMTP(smtp_server, smtp_port, timeout=60) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(sender, recipients, message.as_string())
    logger.info("이메일 발송 완료")


# ---------------------------------------------------------------------------
# 메인 로직
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config).resolve()
    target_date = (
        datetime.strptime(args.target_date, "%Y-%m-%d").date()
        if args.target_date
        else (datetime.now().date() - timedelta(days=1))
    )

    config = load_config(config_path)
    log_dir = config_path.parent / "logs"
    logger = configure_logging(log_dir)

    email_cfg = config["email"]
    db_url = config.get("postgres", "db_url")
    monthly_target = config.getfloat("goals", "monthly_target", fallback=None)

    # 조회 기간 계산
    prev_day = target_date - timedelta(days=1)
    prev_week_same_day = target_date - timedelta(days=7)
    week_start = _week_start_sunday(target_date)
    seven_days_start = target_date - timedelta(days=6)
    month_start = target_date.replace(day=1)

    # 데이터 로드
    df_target = _with_derived_columns(_read_orders(db_url, target_date, target_date))
    df_prev_day = _with_derived_columns(_read_orders(db_url, prev_day, prev_day))
    df_prev_week = _with_derived_columns(_read_orders(db_url, prev_week_same_day, prev_week_same_day))
    df_7d = _with_derived_columns(_read_orders(db_url, seven_days_start, target_date))
    df_month = _with_derived_columns(_read_orders(db_url, month_start, target_date))

    # 전체 요약
    overall = _summarize(df_target)

    def platform_pct_map(summary: Dict[str, float]) -> Dict[str, float]:
        pct = summary.get("platform_pct", {}) or {}
        return {
            "uber": pct.get("uber", 0.0),
            "skip": pct.get("skip", 0.0),
            "doordash": pct.get("doordash", 0.0),
        }

    # 증감 계산
    day_base = _summarize(df_prev_day)
    week_base = _summarize(df_prev_week)
    day_change = {
        "sales": _pct_change(overall["sales"], day_base["sales"]),
        "orders": _pct_change(overall["orders"], day_base["orders"]),
        "avg_order": _pct_change(overall["avg_order"], day_base["avg_order"]),
        "uber": _pct_change(platform_pct_map(overall)["uber"], platform_pct_map(day_base)["uber"]),
        "skip": _pct_change(platform_pct_map(overall)["skip"], platform_pct_map(day_base)["skip"]),
        "doordash": _pct_change(
            platform_pct_map(overall)["doordash"], platform_pct_map(day_base)["doordash"]
        ),
    }
    week_change = {
        "sales": _pct_change(overall["sales"], week_base["sales"]),
        "orders": _pct_change(overall["orders"], week_base["orders"]),
        "avg_order": _pct_change(overall["avg_order"], week_base["avg_order"]),
        "uber": _pct_change(platform_pct_map(overall)["uber"], platform_pct_map(week_base)["uber"]),
        "skip": _pct_change(platform_pct_map(overall)["skip"], platform_pct_map(week_base)["skip"]),
        "doordash": _pct_change(
            platform_pct_map(overall)["doordash"], platform_pct_map(week_base)["doordash"]
        ),
    }

    # 월 누적 및 예측
    days_elapsed = (target_date - month_start).days + 1
    days_in_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - month_start
    month_sales = _summarize(df_month)["sales"]
    daily_avg = month_sales / days_elapsed if days_elapsed else 0.0
    forecast = daily_avg * days_in_month.days
    month_stats = {"sales": month_sales, "forecast": forecast}

    # 매장별 요약/증감
    by_store_today = _summarize_by_store(df_target)
    by_store_prev_day = _summarize_by_store(df_prev_day)
    by_store_prev_week = _summarize_by_store(df_prev_week)
    by_store_dd = _merge_growth(by_store_today, by_store_prev_day, ["sales", "orders", "avg_order"])
    by_store_dd = by_store_dd.rename(columns={"sales_chg_pct": "sales_chg_pct"})
    by_store_wow = _merge_growth(by_store_today, by_store_prev_week, ["sales", "orders", "avg_order"])
    if not by_store_dd.empty and not by_store_wow.empty:
        by_store = by_store_dd.merge(
            by_store_wow[["restaurant_guid", "sales_chg_pct"]].rename(columns={"sales_chg_pct": "sales_chg_pct_week"}),
            on="restaurant_guid",
            how="left",
        )
    else:
        by_store = by_store_dd

    # 이슈 탐지
    issues = _detect_issues(df_target)

    # 시각화
    trend_b64 = build_sales_trend_chart(df_7d)
    pie_b64 = build_platform_pie_chart(df_target)

    # 매장명 매핑
    restaurant_names = {
        guid: name
        for name, guid in config.items("restaurants")
        if guid and not guid.startswith(";")
    }
    store_table_df = by_store.copy()
    numeric_cols = [
        "sales",
        "orders",
        "avg_order",
        "delivery_pct",
        "takeout_pct",
        "dinein_pct",
        "platform_uber_pct",
        "platform_skip_pct",
        "platform_doordash_pct",
        "sales_chg_pct",
        "sales_chg_pct_week",
    ]
    for col in numeric_cols:
        if col in store_table_df:
            store_table_df[col] = pd.to_numeric(store_table_df[col], errors="coerce").fillna(0)

    # 지역 컬럼 부여 후 지역별 테이블을 생성한다.
    store_table_df["region"] = store_table_df["restaurant_guid"].apply(
        lambda g: REGION_BY_GUID.get(g, "OTHER")
    )
    store_table = build_region_tables(store_table_df, restaurant_names)

    html_body = build_html(
        target_date=target_date,
        overall=overall,
        day_change=day_change,
        week_change=week_change,
        month_stats=month_stats,
        monthly_target=monthly_target,
        store_table=store_table,
        issues=issues,
        trend_b64=trend_b64,
        pie_b64=pie_b64,
    )

    subject = f"[BBQ Canada] Daily Sales Report - {target_date}"

    smtp_server = email_cfg.get("smtp_server", email_cfg.get("smtp_host"))
    smtp_port = email_cfg.getint("smtp_port")
    sender = email_cfg.get("sender", email_cfg.get("from", email_cfg.get("smtp_username")))
    username = email_cfg.get("smtp_username", sender)
    password = email_cfg.get("password", email_cfg.get("smtp_password"))

    recipients_source = email_cfg.get(
        "recipient_list",
        email_cfg.get("to", sender),
    )
    recipients = [addr.strip() for addr in recipients_source.split(",") if addr.strip()]

    send_email(
        smtp_server=smtp_server,
        smtp_port=smtp_port,
        username=username,
        sender=sender,
        password=password,
        recipients=recipients,
        subject=subject,
        html_body=html_body,
        logger=logger,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
