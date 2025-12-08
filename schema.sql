-- schema.sql
-- PostgreSQL에서 fact_orders 및 fact_order_items 테이블을 생성하기 위한 스크립트.

CREATE TABLE IF NOT EXISTS fact_orders (
    order_guid TEXT PRIMARY KEY,
    restaurant_guid TEXT NOT NULL,
    business_date DATE NOT NULL,
    opened_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    revenue_center TEXT,
    order_type TEXT,
    source TEXT,
    order_type_norm TEXT,
    platform TEXT,
    subtotal NUMERIC,
    discount_total NUMERIC,
    tax_total NUMERIC,
    service_charge NUMERIC,
    total_amount NUMERIC,
    customer_name TEXT,
    customer_phone TEXT,
    customer_email TEXT,
    delivery_address_json JSONB,
    raw_json JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_order_items (
    order_item_id TEXT PRIMARY KEY,
    order_guid TEXT NOT NULL REFERENCES fact_orders(order_guid),
    menu_name TEXT,
    plu TEXT,
    quantity NUMERIC,
    unit_price NUMERIC,
    total_line_amount NUMERIC,
    modifiers_json JSONB
);

-- 메뉴 차원 테이블: 메뉴 GUID를 키로 이름/PLU 등을 보관
CREATE TABLE IF NOT EXISTS dim_menu_items (
    menu_item_guid TEXT PRIMARY KEY,
    name TEXT,
    plu TEXT,
    external_id TEXT,
    category TEXT,
    price NUMERIC,
    raw_json JSONB
);

CREATE TABLE IF NOT EXISTS dim_revenue_centers (
    revenue_center_guid TEXT PRIMARY KEY,
    restaurant_guid TEXT,
    name TEXT,
    external_id TEXT,
    raw_json JSONB
);

-- 세율 차원 테이블
CREATE TABLE IF NOT EXISTS dim_tax_rates (
    tax_rate_guid TEXT PRIMARY KEY,
    name TEXT,
    rate NUMERIC,
    external_id TEXT,
    raw_json JSONB
);

-- 주문별 세금 내역 팩트 테이블
CREATE TABLE IF NOT EXISTS fact_taxes (
    tax_id TEXT PRIMARY KEY,
    order_guid TEXT NOT NULL REFERENCES fact_orders(order_guid),
    tax_rate_guid TEXT,
    amount NUMERIC,
    raw_json JSONB
);

CREATE INDEX IF NOT EXISTS idx_fact_orders_business_date
    ON fact_orders (business_date);

CREATE INDEX IF NOT EXISTS idx_fact_orders_restaurant_guid
    ON fact_orders (restaurant_guid);

CREATE INDEX IF NOT EXISTS idx_fact_order_items_order_guid
    ON fact_order_items (order_guid);
