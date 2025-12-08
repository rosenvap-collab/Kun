"""load_orders.py

Toast Orders Detailed API에서 데이터를 수집하여 PostgreSQL에 적재하는 ETL 스크립트.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import OrderedDict, defaultdict
from configparser import ConfigParser, ParsingError
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

import requests

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from toast_client import ToastClient


# ----------------------------------------------------------------------
# 로깅 설정
# ----------------------------------------------------------------------
def configure_logging(log_dir: Path) -> logging.Logger:
    """로그 출력을 위한 로거를 초기화한다.

    Windows PowerShell에서도 한글이 깨지지 않도록 UTF-8 인코딩을 강제한다.
    """

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"load_orders_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger("toast_etl")
    logger.setLevel(logging.DEBUG)

    # 기존 핸들러가 있다면 초기화(스크립트 재실행 시 중복 방지)
    if logger.handlers:
        logger.handlers.clear()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


# ----------------------------------------------------------------------
# 설정 파일 로드 유틸리티
# ----------------------------------------------------------------------
def load_config(config_path: Path) -> ConfigParser:
    """config.ini 파일을 UTF-8로 읽어 ConfigParser를 반환한다.

    Windows 환경에서 복사/붙여넣기 과정에서 `access type ...`처럼 `=` 기호가 없는
    메모 라인이 삽입되는 경우가 있어 ConfigParser가 ParsingError를 던질 수 있다.
    이런 라인을 자동으로 주석 처리해 실행을 계속할 수 있도록 보정한다.
    """

    raw_text = config_path.read_text(encoding="utf-8")
    parser = ConfigParser()
    try:
        parser.read_string(raw_text)
        return parser
    except Exception:
        fixed_lines: List[str] = []
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(("[", ";", "#")) and "=" not in stripped:
                fixed_lines.append(f"; {line}")
            else:
                fixed_lines.append(line)

        parser.read_string("\n".join(fixed_lines))
        sys.stderr.write(
            "경고: config.ini에 '=' 문자가 없는 라인이 있어 자동으로 주석 처리했습니다.\n"
        )
        return parser


# ----------------------------------------------------------------------
# 스키마 보강 로직 (기존 테이블에 누락된 컬럼 추가)
# ----------------------------------------------------------------------
def ensure_schema(engine, logger: logging.Logger) -> None:
    """기존 DB에 필요한 컬럼이 없을 경우 안전하게 추가한다.

    기존 환경에 fact_orders/fact_order_items 테이블이 이미 생성되어 있더라도
    새로 정의된 컬럼이 없을 수 있으므로, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
    구문을 실행해 스키마를 최신 정의와 일치시킨다.
    """

    ddl_statements = [
        # fact_orders 컬럼 추가
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS revenue_center TEXT;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS order_type TEXT;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS source TEXT;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS order_type_norm TEXT;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS platform TEXT;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS subtotal NUMERIC;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS discount_total NUMERIC;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS tax_total NUMERIC;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS service_charge NUMERIC;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS total_amount NUMERIC;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS customer_name TEXT;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS customer_phone TEXT;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS customer_email TEXT;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS delivery_address_json JSONB;
        """,
        """
        ALTER TABLE fact_orders
        ADD COLUMN IF NOT EXISTS raw_json JSONB;
        """,
        # fact_order_items 컬럼 추가
        """
        ALTER TABLE fact_order_items
        ADD COLUMN IF NOT EXISTS menu_name TEXT;
        """,
        """
        ALTER TABLE fact_order_items
        ADD COLUMN IF NOT EXISTS plu TEXT;
        """,
        """
        ALTER TABLE fact_order_items
        ADD COLUMN IF NOT EXISTS quantity NUMERIC;
        """,
        """
        ALTER TABLE fact_order_items
        ADD COLUMN IF NOT EXISTS unit_price NUMERIC;
        """,
        """
        ALTER TABLE fact_order_items
        ADD COLUMN IF NOT EXISTS total_line_amount NUMERIC;
        """,
        """
        ALTER TABLE fact_order_items
        ADD COLUMN IF NOT EXISTS modifiers_json JSONB;
        """,
        # dim_revenue_centers 컬럼 추가
        """
        ALTER TABLE dim_revenue_centers
        ADD COLUMN IF NOT EXISTS restaurant_guid TEXT;
        """,
        """
        ALTER TABLE dim_revenue_centers
        ALTER COLUMN restaurant_guid DROP NOT NULL;
        """,
        """
        ALTER TABLE dim_revenue_centers
        ALTER COLUMN name DROP NOT NULL;
        """,
    ]

    with engine.begin() as conn:
        for ddl in ddl_statements:
            conn.execute(text(ddl))
    logger.info("DB 스키마 보강 완료(누락된 컬럼 추가 확인)")


# ----------------------------------------------------------------------
# DB 스키마 정의
# ----------------------------------------------------------------------
metadata = MetaData()

fact_orders = Table(
    "fact_orders",
    metadata,
    Column("order_guid", String, primary_key=True),
    Column("restaurant_guid", String, nullable=False),
    Column("business_date", Date, nullable=False),
    Column("opened_at", DateTime(timezone=True)),
    Column("closed_at", DateTime(timezone=True)),
    Column("revenue_center", String),
    Column("order_type", String),
    Column("source", String),
    Column("order_type_norm", String),
    Column("platform", String),
    Column("subtotal", Numeric),
    Column("discount_total", Numeric),
    Column("tax_total", Numeric),
    Column("service_charge", Numeric),
    Column("total_amount", Numeric),
    Column("customer_name", String),
    Column("customer_phone", String),
    Column("customer_email", String),
    Column("delivery_address_json", JSONB),
    Column("raw_json", JSONB, nullable=False),
)

fact_order_items = Table(
    "fact_order_items",
    metadata,
    Column("order_item_id", String, primary_key=True),
    Column("order_guid", String, nullable=False),
    Column("menu_name", String),
    Column("plu", String),
    Column("quantity", Numeric),
    Column("unit_price", Numeric),
    Column("total_line_amount", Numeric),
    Column("modifiers_json", JSONB),
)

dim_menu_items = Table(
    "dim_menu_items",
    metadata,
    Column("menu_item_guid", String, primary_key=True),
    Column("name", String),
    Column("plu", String),
    Column("external_id", String),
    Column("category", String),
    Column("price", Numeric),
    Column("raw_json", JSONB),
)

dim_revenue_centers = Table(
    "dim_revenue_centers",
    metadata,
    Column("revenue_center_guid", String, primary_key=True),
    Column("restaurant_guid", String, nullable=True),
    Column("name", String),
    Column("external_id", String),
    Column("raw_json", JSONB),
)

dim_tax_rates = Table(
    "dim_tax_rates",
    metadata,
    Column("tax_rate_guid", String, primary_key=True),
    Column("name", String),
    Column("rate", Numeric),
    Column("external_id", String),
    Column("raw_json", JSONB),
)

fact_taxes = Table(
    "fact_taxes",
    metadata,
    Column("tax_id", String, primary_key=True),
    Column("order_guid", String, nullable=False),
    Column("tax_rate_guid", String),
    Column("amount", Numeric),
    Column("raw_json", JSONB),
)


# ----------------------------------------------------------------------
# 데이터 파싱 헬퍼
# ----------------------------------------------------------------------



def parse_order(
    order_json: object, *, fallback_restaurant_guid: str | None = None
) -> Tuple[
    Dict[str, object],
    List[Dict[str, object]],
    List[Dict[str, object]],
    List[Dict[str, object]],
    List[Dict[str, object]],
    List[Dict[str, object]],
]:
    """주문 JSON에서 팩트/차원 테이블에 들어갈 데이터를 추출한다."""

    def _to_number(val: Union[str, int, float, None]) -> Optional[float]:
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

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

    def _normalize_entity_label(value: object) -> Optional[str]:
        """EntityRef 또는 단순 문자열에서 사람이 읽을 수 있는 라벨을 뽑는다."""

        if isinstance(value, dict):
            for key in ("displayName", "name", "label", "externalId"):
                if value.get(key):
                    return str(value.get(key))
            if value.get("guid"):
                return str(value.get("guid"))
        elif isinstance(value, str):
            text = value.strip()
            return text if text else None
        return None

    def _entity_ref(
        entity: object,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[Dict[str, object]]]:
        """dict 또는 문자열 GUID를 (guid, name, external_id, raw_json)로 정규화한다."""

        if isinstance(entity, dict):
            guid = entity.get("guid") or entity.get("id") or entity.get("externalId")
            name = (
                entity.get("displayName")
                or entity.get("name")
                or entity.get("externalId")
                or guid
            )
            external_id = entity.get("externalId") or entity.get("id")
            return guid, name, external_id, entity
        if isinstance(entity, str) and entity.strip():
            guid = entity.strip()
            raw = {"guid": guid}
            return guid, guid, guid, raw
        return None, None, None, None

    def _parse_date(value: object) -> Optional[date]:
        """여러 형식의 날짜 입력을 date 객체로 변환한다."""

        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value

        text: Optional[str] = None
        if isinstance(value, int):
            text = str(value)
        elif isinstance(value, str):
            text = value.strip()

        if not text:
            return None

        # yyyymmdd 형식
        if re.fullmatch(r"\d{8}", text):
            try:
                return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
            except ValueError:
                return None

        # YYYY-MM-DD 또는 ISO 형식
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return datetime.strptime(text, "%Y-%m-%d").date()
            except ValueError:
                return None

    def _parse_datetime(value: object) -> Optional[datetime]:
        """ISO 문자열 등을 datetime 객체로 변환한다."""

        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value))
            except (TypeError, ValueError, OSError):
                return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                try:
                    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
        return None

    def _extract_rate(raw: object) -> Optional[float]:
        rate_val = _to_number(raw)
        if rate_val is None and isinstance(raw, dict):
            for key in ("rate", "value", "percentage"):
                rate_val = _to_number(raw.get(key))
                if rate_val is not None:
                    break
        if rate_val is not None and rate_val > 1.5:
            return rate_val / 100.0
        return rate_val

    def _find_amount(obj: object, *keys: str) -> Optional[float]:
        if not isinstance(obj, dict):
            return None
        for key in keys:
            val = obj.get(key)
            if val is not None:
                num = _to_number(val)
                if num is not None:
                    return num
            amts = obj.get("amounts") if isinstance(obj.get("amounts"), dict) else None
            if amts and amts.get(key) is not None:
                num = _to_number(amts.get(key))
                if num is not None:
                    return num
        return None

    order_guid: Optional[str] = None
    raw_order_payload: Dict[str, object] = {}
    if isinstance(order_json, dict):
        order_guid = order_json.get("guid") or order_json.get("orderGuid")
        raw_order_payload = order_json

    if not order_guid:
        return {}, [], [], [], [], []

    checks = order_json.get("checks") if isinstance(order_json, dict) else None
    customer_info: Dict[str, object] = {}
    if isinstance(checks, list) and checks:
        first_check = checks[0]
        if isinstance(first_check, dict):
            customer_info = first_check.get("customer") if isinstance(first_check.get("customer"), dict) else {}

    restaurant_guid_value = None
    if isinstance(order_json, dict):
        restaurant_guid_value = order_json.get("restaurantGuid") or fallback_restaurant_guid
    else:
        restaurant_guid_value = fallback_restaurant_guid

    amounts = order_json.get("amounts", {}) if isinstance(order_json, dict) else {}

    menu_dim_by_guid: Dict[str, Dict[str, object]] = {}
    revenue_dim_by_guid: Dict[str, Dict[str, object]] = {}
    tax_rate_dim_by_guid: Dict[str, Dict[str, object]] = {}
    tax_fact_rows: List[Dict[str, object]] = []
    item_totals: List[float] = []

    dining_option = order_json.get("diningOption") if isinstance(order_json, dict) else None

    def _dining_option_label(value: object) -> Optional[str]:
        label = _normalize_entity_label(value)
        if label:
            return label
        if isinstance(value, dict):
            behavior = value.get("behavior") or value.get("type")
            if behavior:
                return str(behavior)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    dining_option_label = _dining_option_label(dining_option) or "unknown"

    rc_guid, rc_name, rc_external_id, rc_raw = _entity_ref(
        order_json.get("revenueCenter") if isinstance(order_json, dict) else None
    )
    revenue_center_label = rc_name or rc_guid or "unknown"

    def _source_label(value: object) -> Optional[str]:
        label = _normalize_entity_label(value)
        if label:
            return label
        if isinstance(value, dict):
            for key in ("source", "channel", "behavior"):
                if value.get(key):
                    return str(value.get(key))
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    source_label = (
        _source_label(order_json.get("source")) if isinstance(order_json, dict) else None
    ) or "unknown"

    order_type_norm_value = _categorize_order_type(dining_option_label)
    platform_value = _categorize_platform(source_label)

    def _sum_from_checks(amount_key: str) -> Optional[float]:
        total = 0.0
        found = False
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict):
                    continue
                raw = check.get("amounts")
                if not isinstance(raw, dict):
                    continue
                val = raw.get(amount_key)
                if val is None:
                    continue
                num = _to_number(val)
                if num is None:
                    continue
                total += num
                found = True
        return total if found else None

    delivery_info = order_json.get("delivery")
    delivery_address = delivery_info.get("address") if isinstance(delivery_info, dict) else None

    def _handle_taxes(taxes_obj: object, scope: str) -> None:
        if not isinstance(taxes_obj, list):
            return
        for idx, tx in enumerate(taxes_obj, start=1):
            if not isinstance(tx, dict):
                continue
            rate_entity = tx.get("taxRate") if "taxRate" in tx else tx.get("tax_rate")
            rate_guid, rate_name, rate_external_id, rate_raw = _entity_ref(rate_entity)
            if not rate_guid:
                rate_guid = tx.get("taxRateGuid") or tx.get("guid") or f"{order_guid}-{scope}-taxrate-{idx}"
            rate_value = _extract_rate(tx.get("rate"))
            if rate_value is None and rate_raw is not None:
                rate_value = _extract_rate(rate_raw)
            if rate_value is None:
                rate_value = _extract_rate(tx.get("percentage") or tx.get("value") or tx.get("rateValue"))
            if rate_guid not in tax_rate_dim_by_guid:
                tax_rate_dim_by_guid[rate_guid] = {
                    "tax_rate_guid": rate_guid,
                    "name": rate_name or tx.get("name") or rate_guid,
                    "rate": rate_value,
                    "external_id": rate_external_id,
                    "raw_json": rate_raw or tx,
                }
            fact_id = tx.get("guid") or tx.get("taxLineGuid") or f"{order_guid}-{scope}-{idx}"
            tax_fact_rows.append(
                {
                    "tax_id": fact_id,
                    "order_guid": order_guid,
                    "tax_rate_guid": rate_guid,
                    "amount": _to_number(tx.get("amount") or tx.get("taxAmount") or tx.get("value")),
                    "raw_json": tx,
                }
            )

    items: List[Dict[str, object]] = []
    if isinstance(checks, list):
        for check_idx, check in enumerate(checks, start=1):
            if not isinstance(check, dict):
                continue
            _handle_taxes(check.get("taxes"), f"check-{check_idx}-taxes")
            _handle_taxes(check.get("appliedTaxes"), f"check-{check_idx}-applied")

            ordered_items = None
            if isinstance(check.get("orderedItems"), list):
                ordered_items = check.get("orderedItems")
            elif isinstance(check.get("selections"), list):
                ordered_items = check.get("selections")
            if not isinstance(ordered_items, list):
                continue

            for item_idx, item in enumerate(ordered_items, start=1):
                if not isinstance(item, dict):
                    continue

                menu_guid = None
                menu_name = None
                menu_external_id = None
                menu_raw = None
                menu_entity = item.get("menuItem")
                if menu_entity is not None:
                    menu_guid, menu_name, menu_external_id, menu_raw = _entity_ref(menu_entity)
                if not menu_guid:
                    menu_guid = (
                        item.get("menuItemGuid")
                        or item.get("selectionGuid")
                        or item.get("guid")
                        or item.get("id")
                    )
                if not menu_guid:
                    menu_guid = f"{order_guid}-selection-{check_idx}-{item_idx}"

                display_name = (
                    item.get("displayName")
                    or item.get("name")
                    or (menu_raw.get("displayName") if isinstance(menu_raw, dict) else None)
                    or (menu_raw.get("name") if isinstance(menu_raw, dict) else None)
                    or menu_name
                    or menu_guid
                )

                plu_val = (
                    item.get("plu")
                    or item.get("selectionPlu")
                    or (menu_raw.get("plu") if isinstance(menu_raw, dict) else None)
                )

                category_val = None
                if isinstance(menu_raw, dict) and isinstance(menu_raw.get("salesCategory"), dict):
                    cat = menu_raw.get("salesCategory")
                    category_val = cat.get("name") or cat.get("guid") or cat.get("externalId")

                base_price = _to_number(
                    item.get("basePrice")
                    or item.get("unitPrice")
                    or item.get("price")
                )
                if base_price is None and isinstance(menu_raw, dict):
                    base_price = _to_number(menu_raw.get("price"))

                if menu_guid and menu_guid not in menu_dim_by_guid:
                    menu_dim_by_guid[menu_guid] = {
                        "menu_item_guid": menu_guid,
                        "name": display_name,
                        "plu": plu_val,
                        "external_id": menu_external_id,
                        "category": category_val,
                        "price": base_price,
                        "raw_json": menu_raw or item,
                    }

                item_guid = item.get("guid") or item.get("orderItemGuid") or item.get("selectionGuid")
                order_item_id = item_guid or f"{order_guid}-item-{check_idx}-{item_idx}"

                raw_item_amounts = item.get("amounts") if isinstance(item.get("amounts"), dict) else {}
                qty = _to_number(item.get("quantity") or raw_item_amounts.get("quantity")) or 1.0
                unit_price = _to_number(
                    raw_item_amounts.get("unit")
                    if isinstance(raw_item_amounts, dict)
                    else None
                )
                if unit_price is None:
                    unit_price = _to_number(
                        item.get("unitPrice") or item.get("price") or item.get("basePrice")
                    )
                total_line = _to_number(
                    raw_item_amounts.get("total")
                    if isinstance(raw_item_amounts, dict)
                    else None
                )
                if total_line is None:
                    total_line = _to_number(
                        item.get("totalAmount")
                        or item.get("amount")
                        or item.get("priceTotal")
                    )
                if total_line is None and unit_price is not None:
                    total_line = unit_price * qty

                if total_line is not None:
                    item_totals.append(total_line)

                modifiers = item.get("modifiers") or item.get("options")

                items.append(
                    {
                        "order_item_id": order_item_id,
                        "order_guid": order_guid,
                        "menu_name": display_name,
                        "plu": plu_val,
                        "quantity": qty,
                        "unit_price": unit_price,
                        "total_line_amount": total_line,
                        "modifiers_json": modifiers,
                    }
                )

                _handle_taxes(item.get("appliedTaxes"), f"item-{order_item_id}-applied")
                _handle_taxes(item.get("taxes"), f"item-{order_item_id}-taxes")

    _handle_taxes(order_json.get("taxes") if isinstance(order_json, dict) else None, "order-taxes")
    _handle_taxes(
        order_json.get("appliedTaxes") if isinstance(order_json, dict) else None,
        "order-applied",
    )

    derived_subtotal = _find_amount(order_json, "amount", "subtotal", "totalBeforeTax", "preTaxAmount")
    if derived_subtotal is None:
        derived_subtotal = _find_amount(amounts, "subtotal", "amount")
    if derived_subtotal is None:
        derived_subtotal = _sum_from_checks("subtotal")
    if derived_subtotal is None and item_totals:
        derived_subtotal = sum(item_totals)

    derived_discount = _find_amount(order_json, "discountAmount", "discount", "totalDiscountAmount")
    if derived_discount is None:
        derived_discount = _find_amount(amounts, "discount", "totalDiscountAmount")
    if derived_discount is None:
        derived_discount = _sum_from_checks("discount")

    derived_tax = _find_amount(order_json, "taxAmount", "totalTaxAmount", "tax")
    if derived_tax is None:
        derived_tax = _find_amount(amounts, "tax")
    if derived_tax is None:
        derived_tax = _sum_from_checks("tax")
    if derived_tax is None and tax_fact_rows:
        derived_tax = sum(_to_number(tx.get("amount")) or 0.0 for tx in tax_fact_rows)

    derived_service = _find_amount(order_json, "serviceChargeAmount", "serviceCharge")
    if derived_service is None:
        derived_service = _sum_from_checks("serviceCharge")

    derived_total = _find_amount(order_json, "totalAmount", "total", "grandTotal")
    if derived_total is None:
        derived_total = _find_amount(amounts, "total")
    if derived_total is None:
        derived_total = _sum_from_checks("total")
    if derived_total is None and derived_subtotal is not None:
        pieces = [
            _to_number(derived_subtotal) or 0.0,
            -(_to_number(derived_discount) or 0.0),
            _to_number(derived_tax) or 0.0,
            _to_number(derived_service) or 0.0,
        ]
        derived_total = sum(pieces)

    if rc_guid:
        revenue_dim_by_guid[rc_guid] = {
            "revenue_center_guid": rc_guid,
            "restaurant_guid": restaurant_guid_value,
            "name": revenue_center_label,
            "external_id": rc_external_id,
            "raw_json": rc_raw or {"guid": rc_guid},
        }

    order_record: Dict[str, object] = {
        "order_guid": order_guid,
        "restaurant_guid": restaurant_guid_value,
        "business_date": _parse_date(order_json.get("businessDate")),
        "opened_at": _parse_datetime(order_json.get("openedDate")),
        "closed_at": _parse_datetime(order_json.get("closedDate")),
        "revenue_center": revenue_center_label,
        "order_type": dining_option_label,
        "source": source_label,
        "order_type_norm": order_type_norm_value,
        "platform": platform_value,
        "subtotal": derived_subtotal,
        "discount_total": derived_discount,
        "tax_total": derived_tax,
        "service_charge": derived_service,
        "total_amount": derived_total,
        "customer_name": customer_info.get("name"),
        "customer_phone": customer_info.get("phone"),
        "customer_email": customer_info.get("email"),
        "delivery_address_json": delivery_address,
        "raw_json": raw_order_payload,
    }

    return (
        order_record,
        items,
        list(menu_dim_by_guid.values()),
        list(revenue_dim_by_guid.values()),
        list(tax_rate_dim_by_guid.values()),
        tax_fact_rows,
    )
# ----------------------------------------------------------------------
# DB Upsert 로직
# ----------------------------------------------------------------------
def upsert_records(
    session: Session,
    order_record: Dict[str, object],
    item_records: Iterable[Dict[str, object]],
    menu_records: Iterable[Dict[str, object]],
    revenue_center_records: Iterable[Dict[str, object]],
    tax_rate_records: Iterable[Dict[str, object]],
    tax_fact_records: Iterable[Dict[str, object]],
    logger: logging.Logger,
) -> None:
    """주문 및 관련 차원/팩트 데이터를 upsert 방식으로 저장한다."""

    order_stmt = pg_insert(fact_orders).values(**order_record)
    order_update_fields = {col.name: order_stmt.excluded[col.name] for col in fact_orders.columns if col.name != "order_guid"}
    order_stmt = order_stmt.on_conflict_do_update(
        index_elements=[fact_orders.c.order_guid],
        set_=order_update_fields,
    )
    session.execute(order_stmt)

    if item_records:
        item_values = list(item_records)
        if item_values:
            item_stmt = pg_insert(fact_order_items).values(item_values)
            item_update_fields = {
                col.name: item_stmt.excluded[col.name]
                for col in fact_order_items.columns
                if col.name != "order_item_id"
            }
            item_stmt = item_stmt.on_conflict_do_update(
                index_elements=[fact_order_items.c.order_item_id],
                set_=item_update_fields,
            )
            session.execute(item_stmt)

    # 차원 테이블 upsert
    def _bulk_upsert(table, records, pk_field: str) -> None:
        values = list(records)
        if not values:
            return
        stmt = pg_insert(table).values(values)
        update_fields = {
            col.name: stmt.excluded[col.name]
            for col in table.columns
            if col.name != pk_field
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=[getattr(table.c, pk_field)],
            set_=update_fields,
        )
        session.execute(stmt)

    _bulk_upsert(dim_menu_items, menu_records, "menu_item_guid")

    # revenue_center_records에 restaurant_guid가 빠진 케이스를 방어적으로 제거한다.
    cleaned_rc_records = [
        rc for rc in revenue_center_records if rc.get("restaurant_guid")
    ]
    _bulk_upsert(dim_revenue_centers, cleaned_rc_records, "revenue_center_guid")

    _bulk_upsert(dim_tax_rates, tax_rate_records, "tax_rate_guid")

    # fact_taxes upsert
    tax_values = list(tax_fact_records)
    if tax_values:
        tax_stmt = pg_insert(fact_taxes).values(tax_values)
        tax_update_fields = {
            col.name: tax_stmt.excluded[col.name]
            for col in fact_taxes.columns
            if col.name != "tax_id"
        }
        tax_stmt = tax_stmt.on_conflict_do_update(
            index_elements=[fact_taxes.c.tax_id],
            set_=tax_update_fields,
        )
        session.execute(tax_stmt)

    logger.debug("주문 %s 및 관련 아이템 upsert 완료", order_record.get("order_guid"))


# ----------------------------------------------------------------------
# 날짜 파싱 유틸리티
# ----------------------------------------------------------------------
def resolve_business_dates(
    config: ConfigParser,
    *,
    single_date: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    logger: logging.Logger,
) -> List[date]:
    """CLI 인자 또는 config 설정을 바탕으로 businessDate(date 리스트)를 결정한다.

    - `--date` 또는 위치 인자(YYYY-MM-DD)가 제공되면 단일 날짜만 반환한다.
    - `--start`/`--end`가 주어지면 해당 구간을 하루씩 순회하는 리스트를 반환한다.
    - 아무것도 없으면 기존 동작(orders.default_days_offset 사용)을 유지한다.
    """

    if single_date and (start_date or end_date):
        raise ValueError("--date와 --start/--end는 함께 사용할 수 없습니다.")

    def _to_date(value: str) -> date:
        return datetime.strptime(value, "%Y-%m-%d").date()

    # 단일 날짜 모드
    if single_date:
        return [_to_date(single_date)]

    # 기간 모드
    if start_date or end_date:
        if not start_date and not end_date:
            raise ValueError("--start 또는 --end 둘 중 하나는 반드시 지정되어야 합니다.")
        start = _to_date(start_date or end_date)
        end = _to_date(end_date or start_date)
        if end < start:
            raise ValueError("--end 날짜는 --start 보다 빠를 수 없습니다.")

        days: List[date] = []
        current = start
        logger.info("Starting backfill from %s to %s", start.isoformat(), end.isoformat())
        while current <= end:
            days.append(current)
            current += timedelta(days=1)
        return days

    # 기본: config의 offset 사용
    offset_days = int(config.get("orders", "default_days_offset", fallback=-1))
    target_date = datetime.now().date() + timedelta(days=offset_days)
    return [target_date]


def _detect_region(alias: str) -> str:
    """매장 별칭을 기반으로 간단한 지역(AB/BC/ON/기타)을 판별한다."""

    ab_aliases = {
        "fort_mcmurray",
        "canmore",
        "yorkton",
        "calgary_trail",
        "cochrane",
        "london_square",
        "midnapore",
        "nolan_hill",
        "southland_crossing",
        "stampede",
        "west_edmonton",
        "westbrook",
    }
    bc_aliases = {
        "granville",
        "hanin_village",
        "hastings",
        "main_street",
        "metrotown",
        "poco_place",
        "richmond",
        "robson",
        "surrey_guildford",
        "yale",
    }
    on_aliases = {
        "newmarket",
        "moncton",
        "stouffville",
        "liberty_village",
        "ajax_south",
        "aurora",
        "cummer",
        "danforth",
        "don_mills",
        "dorval_crossing",
        "downtown_markham",
        "eagles_landing",
        "elm",
        "hurontario_dundas",
        "hwy7_leslie",
        "london_downtown",
        "markham_unionville",
        "north_scarborough",
        "oshawa",
        "ut_spadina",
        "yonge_bloor",
        "waterloo_central",
    }

    lower = alias.lower()
    if lower in ab_aliases:
        return "AB"
    if lower in bc_aliases:
        return "BC"
    if lower in on_aliases:
        return "ON"
    return "OTHER"


def parse_restaurant_entries(config: ConfigParser) -> Sequence[Tuple[str, str, str, str]]:
    """[restaurants] 섹션을 파싱해 (별칭, GUID, External ID, REGION)를 반환한다.

    값 형식은 "<restaurant_guid>[|<toast_restaurant_external_id>]"를 허용한다.
    외부 ID가 제공되지 않으면 GUID를 그대로 사용해도 되지만, Toast 관리 화면에서
    별도로 정의된 외부 식별자가 있다면 해당 값을 입력하면 401 문제를 예방할 수 있다.
    """

    entries: List[Tuple[str, str, str, str]] = []
    for alias, raw_value in config.items("restaurants"):
        raw = (raw_value or "").strip()
        if not raw or raw.startswith(";"):
            continue

        guid: str
        external_id: str
        if "|" in raw:
            guid_part, external_part = raw.split("|", 1)
            guid = guid_part.strip()
            external_id = external_part.strip() or guid
        else:
            guid = raw
            external_id = guid

        if not guid:
            continue

        region = _detect_region(alias)
        entries.append((alias, guid, external_id, region))

    return entries


# ----------------------------------------------------------------------
# 메인 실행 로직
# ----------------------------------------------------------------------
def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Toast Orders ETL")
    parser.add_argument("config_path", help="config.ini 경로")
    parser.add_argument("positional_date", nargs="?", help="(옵션) YYYY-MM-DD 단일 실행 일자")
    parser.add_argument("--date", dest="date_arg", help="YYYY-MM-DD 단일 실행 일자")
    parser.add_argument("--start", dest="start_date", help="YYYY-MM-DD 시작일")
    parser.add_argument("--end", dest="end_date", help="YYYY-MM-DD 종료일")
    args = parser.parse_args(argv[1:])

    config_path = Path(args.config_path).resolve()
    config = load_config(config_path)

    log_dir = config_path.parent / "logs"
    logger = configure_logging(log_dir)
    logger.info("Toast 주문 ETL 시작")

    chosen_date_arg = args.date_arg or args.positional_date
    try:
        business_dates = resolve_business_dates(
            config,
            single_date=chosen_date_arg,
            start_date=args.start_date,
            end_date=args.end_date,
            logger=logger,
        )
    except ValueError as exc:  # 잘못된 입력 조합
        logger.error(str(exc))
        return 1

    if len(business_dates) == 1:
        logger.info("대상 businessDate=%s", business_dates[0].isoformat())
    else:
        logger.info(
            "대상 businessDate 범위: %s ~ %s", business_dates[0].isoformat(), business_dates[-1].isoformat()
        )

    db_url = config.get("postgres", "db_url")
    engine = create_engine(db_url, echo=False, future=True)

    # 필요 시 테이블 자동 생성 (최초 실행 대비).
    try:
        metadata.create_all(engine)
        # 기존 테이블에 누락된 컬럼을 보강한다.
        ensure_schema(engine, logger)
    except OperationalError as exc:
        logger.error(
            "PostgreSQL 연결에 실패했습니다. db_url=%s - 서버가 실행 중인지 확인해 주세요.",
            db_url,
        )
        # 세부 스택은 디버깅용으로만 남긴다.
        logger.debug("DB 연결 오류 상세", exc_info=exc)
        return 1

    toast_config = config["toast_api"]

    raw_scope_string = toast_config.get("scopes", fallback="")
    scope_tokens = [
        token
        for token in re.split(r"[\s,]+", raw_scope_string)
        if token
    ]
    client = ToastClient(
        base_url=toast_config.get("base_url"),
        auth_url=toast_config.get("auth_url"),
        client_id=toast_config.get("client_id"),
        client_secret=toast_config.get("client_secret"),
        scopes=scope_tokens,
        logger=logger,
    )

    restaurants = parse_restaurant_entries(config)

    # 잦은 권한 오류가 발생할 때 API rate limit을 불필요하게 소진하지 않도록
    # 일정 횟수 이상 401/403이 누적되면 조기 종료한다. 기본값은 5회이며
    # config.ini의 [orders] 섹션에서 max_auth_failures로 조정할 수 있다.
    max_auth_failures = int(
        config.get("orders", "max_auth_failures", fallback="5")
    )
    auth_failures = 0
    unauthorized_aliases: List[str] = []

    start_time = datetime.now()
    exit_code = 0
    stop_due_auth = False
    with Session(engine) as session:
        for business_date in business_dates:
            logger.info("Processing %s", business_date.isoformat())
            day_order_count = 0
            failures_by_region: Dict[str, List[str]] = defaultdict(list)

            region_groups: "OrderedDict[str, List[Tuple[str, str, str, str]]]" = OrderedDict()
            for entry in restaurants:
                region_groups.setdefault(entry[3], []).append(entry)

            for region, region_entries in region_groups.items():
                total_region = len(region_entries)
                region_success = 0
                logger.info(
                    "[데이터를 불러오는 중입니다.] %s %s 매장 (%d/%d)",
                    business_date.isoformat(),
                    region,
                    region_success,
                    total_region,
                )

                for name, guid, external_id, _ in region_entries:
                    try:
                        for order_json in client.get_orders_for_business_date(
                            guid,
                            business_date,
                            restaurant_external_id=external_id,
                        ):
                            (
                                order_record,
                                item_records,
                                menu_records,
                                revenue_records,
                                tax_rate_records,
                                tax_fact_records,
                            ) = parse_order(
                                order_json,
                                fallback_restaurant_guid=guid,
                            )
                            if not order_record.get("order_guid"):
                                logger.warning(
                                    "order_guid가 없는 주문은 스킵합니다. raw=%s",
                                    json.dumps(order_json),
                                )
                                continue
                            if not order_record.get("business_date"):
                                logger.warning(
                                    "business_date가 확인되지 않은 주문은 스킵합니다. order_guid=%s",
                                    order_record.get("order_guid"),
                                )
                                continue
                            upsert_records(
                                session,
                                order_record,
                                item_records,
                                menu_records,
                                revenue_records,
                                tax_rate_records,
                                tax_fact_records,
                                logger,
                            )
                            day_order_count += 1
                        session.commit()
                        region_success += 1
                    except requests.HTTPError as http_err:
                        session.rollback()
                        status = http_err.response.status_code if http_err.response is not None else None
                        if status in {401, 403}:
                            auth_failures += 1
                            unauthorized_aliases.append(name)
                            failures_by_region[region].append(name)
                            if auth_failures >= max_auth_failures:
                                logger.error(
                                    "권한 오류가 %s회 누적되어 ETL을 조기 종료합니다. Toast 계정의 매장 권한을 확인해 주세요.",
                                    auth_failures,
                                )
                                exit_code = 1
                                stop_due_auth = True
                                break
                            continue

                        failures_by_region[region].append(name)
                        logger.exception("HTTP 오류로 매장 %s 처리에 실패했습니다.", name)
                        raise
                    except SQLAlchemyError:
                        failures_by_region[region].append(name)
                        logger.exception("DB 작업 중 오류가 발생하여 롤백합니다.")
                        session.rollback()
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        failures_by_region[region].append(name)
                        logger.exception("매장 %s 처리 중 예기치 못한 오류: %s", name, exc)

                logger.info(
                    "[%s 완료] %s 매장 처리: 성공 %d/%d, 실패 %d", 
                    region,
                    business_date.isoformat(),
                    region_success,
                    total_region,
                    len(failures_by_region.get(region, [])),
                )

                if stop_due_auth:
                    break

            if stop_due_auth:
                break

            logger.info("Finished %s with %s orders", business_date.isoformat(), day_order_count)
            for region, failed_list in failures_by_region.items():
                if failed_list:
                    logger.warning(
                        "[%s] 실패 매장: %s", region, ", ".join(sorted(set(failed_list)))
                    )

    if unauthorized_aliases:
        logger.warning(
            "권한 문제로 스킵된 매장: %s",
            ", ".join(sorted(set(unauthorized_aliases))),
        )

    duration = datetime.now() - start_time
    logger.info("ETL 완료. 총 소요 시간: %s", duration)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
