"""Snowflake financial operations dashboard.

Run with: streamlit run app.py
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import streamlit as st
import snowflake.connector


st.set_page_config(page_title="Snowflake Finance Dashboard", page_icon="❄️", layout="wide")


@st.cache_resource
def get_connection(config_items: tuple[tuple[str, Any], ...]):
    """Create one Snowflake connection per Streamlit session/configuration."""
    return snowflake.connector.connect(**dict(config_items))


@st.cache_data(ttl=300, show_spinner=False)
def query_snowflake(config_items: tuple[tuple[str, Any], ...], sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    conn = get_connection(config_items)
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetch_pandas_all()


def snowflake_config() -> dict[str, Any] | None:
    """Read connection credentials from .streamlit/secrets.toml."""
    required = {"account", "user", "password", "warehouse", "database", "schema"}
    if "snowflake" not in st.secrets:
        return None
    config = dict(st.secrets["snowflake"])
    missing = required - config.keys()
    if missing:
        st.error(f"Missing Snowflake secret(s): {', '.join(sorted(missing))}")
        return None
    return config


def money(value: float | int | None) -> str:
    return f"${float(value or 0):,.2f}"


st.title("Snowflake Finance Dashboard")
st.caption("Accounts, customers, and transactions")

config = snowflake_config()
if not config:
    st.info("Add Snowflake credentials to `.streamlit/secrets.toml`, then restart the app. See README.md.")
    st.stop()

config_items = tuple(sorted(config.items()))

with st.sidebar:
    st.header("Filters")
    end_date = st.date_input("Transaction end date", value=date.today())
    start_date = st.date_input("Transaction start date", value=end_date - timedelta(days=30), max_value=end_date)
    statuses = st.multiselect("Transaction status", ["PENDING", "COMPLETED", "FAILED", "CANCELLED"], default=[])
    refresh = st.button("Refresh data", use_container_width=True)

if refresh:
    query_snowflake.clear()

status_clause = ""
params: list[Any] = [start_date, end_date + timedelta(days=1)]
if statuses:
    placeholders = ", ".join(["%s"] * len(statuses))
    status_clause = f" AND t.STATUS IN ({placeholders})"
    params.extend(statuses)

try:
    kpis = query_snowflake(
        config_items,
        """
        SELECT
            (SELECT COUNT(*) FROM DIM_ACCOUNTS WHERE IS_CURRENT = TRUE) AS ACTIVE_ACCOUNTS,
            (SELECT COUNT(*) FROM dim_CUSTOMERS) AS ACTIVE_CUSTOMERS,
            COUNT(*) AS TRANSACTION_COUNT,
            COALESCE(SUM(t.AMOUNT), 0) AS TRANSACTION_VOLUME
        FROM fact_transactions t
        WHERE t.TRANSACTION_TIME >= %s AND t.TRANSACTION_TIME < %s""" + status_clause,
        tuple(params),
    ).iloc[0]

    trend = query_snowflake(
        config_items,
        """
        SELECT DATE_TRUNC('DAY', t.TRANSACTION_TIME)::DATE AS DAY,
               COUNT(*) AS TRANSACTION_COUNT,
               COALESCE(SUM(t.AMOUNT), 0) AS VOLUME
        FROM fact_transactions t
        WHERE t.TRANSACTION_TIME >= %s AND t.TRANSACTION_TIME < %s""" + status_clause + " GROUP BY 1 ORDER BY 1",
        tuple(params),
    )

    transaction_types = query_snowflake(
        config_items,
        """
        SELECT COALESCE(t.TRANSACTION_TYPE, 'UNKNOWN') AS TRANSACTION_TYPE,
               COUNT(*) AS TRANSACTION_COUNT,
               COALESCE(SUM(t.AMOUNT), 0) AS VOLUME
        FROM fact_transactions t
        WHERE t.TRANSACTION_TIME >= %s AND t.TRANSACTION_TIME < %s""" + status_clause + " GROUP BY 1 ORDER BY VOLUME DESC",
        tuple(params),
    )

    accounts = query_snowflake(
        config_items,
        """
        SELECT ACCOUNT_ID, CUSTOMER_ID, ACCOUNT_TYPE, BALANCE, CURRENCY, CREATED_AT
        FROM DIM_ACCOUNTS
        WHERE IS_CURRENT = TRUE
        ORDER BY BALANCE DESC NULLS LAST
        LIMIT 500
        """,
    )

    customers = query_snowflake(
        config_items,
        """
        SELECT CUSTOMER_ID, FIRST_NAME, LAST_NAME, EMAIL, CREATED_AT
        FROM dim_CUSTOMERS
        ORDER BY CREATED_AT DESC
        LIMIT 500
        """,
    )

    transactions = query_snowflake(
        config_items,
        """
        SELECT t.TRANSACTION_ID, t.ACCOUNT_ID, t.CUSTOMER_ID, t.AMOUNT,
               t.RELATED_ACCOUNT_ID, t.STATUS, t.TRANSACTION_TYPE,
               t.TRANSACTION_TIME, t.LOAD_TIMESTAMP,
               CONCAT(c.FIRST_NAME, ' ', c.LAST_NAME) AS CUSTOMER_NAME
        FROM fact_transactions t
        LEFT JOIN dim_CUSTOMERS c ON t.CUSTOMER_ID = c.CUSTOMER_ID 
        WHERE t.TRANSACTION_TIME >= %s AND t.TRANSACTION_TIME < %s""" + status_clause + " ORDER BY t.TRANSACTION_TIME DESC LIMIT 500",
        tuple(params),
    )
except Exception as exc:
    st.error("Snowflake query failed. Confirm the database, schema, table names, and role permissions.")
    st.exception(exc)
    st.stop()

metric_columns = st.columns(4)
metric_columns[0].metric("Active accounts", f"{int(kpis.ACTIVE_ACCOUNTS):,}")
metric_columns[1].metric("Active customers", f"{int(kpis.ACTIVE_CUSTOMERS):,}")
metric_columns[2].metric("Transactions", f"{int(kpis.TRANSACTION_COUNT):,}")
metric_columns[3].metric("Transaction volume", money(kpis.TRANSACTION_VOLUME))

left, right = st.columns((2, 1))
with left:
    st.subheader("Transaction activity")
    if trend.empty:
        st.info("No transactions match the selected filters.")
    else:
        st.line_chart(trend.set_index("DAY")[["VOLUME"]], y_label="Amount")
with right:
    st.subheader("Volume by type")
    if not transaction_types.empty:
        st.bar_chart(transaction_types.set_index("TRANSACTION_TYPE")[["VOLUME"]], horizontal=True)

accounts_tab, customers_tab, transactions_tab = st.tabs(["Accounts", "Customers", "Transactions"])
with accounts_tab:
    st.subheader("Current accounts")
    st.dataframe(
        accounts,
        use_container_width=True,
        hide_index=True,
        column_config={"BALANCE": st.column_config.NumberColumn("Balance", format="$%,.2f")},
    )
with customers_tab:
    st.subheader("Current customers")
    st.dataframe(customers, use_container_width=True, hide_index=True)
with transactions_tab:
    st.subheader("Latest transactions")
    st.dataframe(
        transactions,
        use_container_width=True,
        hide_index=True,
        column_config={"AMOUNT": st.column_config.NumberColumn("Amount", format="$%,.2f")},
    )
