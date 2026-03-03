import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("REMNASHOP_CONFIG", ROOT / "config.json"))
DB_PATH = Path(os.environ.get("REMNASHOP_DB", ROOT / "starlight.db"))

app = FastAPI(title="RemnaShop Web 管理台", version="3.6")


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _query(sql: str, args=(), one=False):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(sql, args)
    rows = cur.fetchall()
    conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def _execute(sql: str, args=()):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(sql, args)
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed


def _ensure_web_token() -> str:
    cfg = _load_config()
    token = str(cfg.get("admin_web_token") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(24)
    cfg["admin_web_token"] = token
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return token


def auth(x_admin_token: Optional[str] = Header(default=None)):
    token = _ensure_web_token()
    if x_admin_token != token:
        raise HTTPException(status_code=401, detail="未授权")


class PaymentSettingsPayload(BaseModel):
    alipay_enabled: bool
    alipay_token_enabled: bool
    alipay_qr_enabled: bool
    wechat_enabled: bool


@app.get("/health")
def health():
    return {"ok": True, "service": "remnashop-web"}


@app.get("/api/summary", dependencies=[Depends(auth)])
def summary():
    pending = _query("SELECT COUNT(1) c FROM orders WHERE status='pending'", one=True)
    failed = _query("SELECT COUNT(1) c FROM orders WHERE status='failed'", one=True)
    today = _query("SELECT COUNT(1) c FROM orders WHERE created_at >= strftime('%s','now','start of day')", one=True)
    return {
        "pending_orders": int(pending["c"]) if pending else 0,
        "failed_orders": int(failed["c"]) if failed else 0,
        "today_orders": int(today["c"]) if today else 0,
    }


@app.get("/api/orders", dependencies=[Depends(auth)])
def list_orders(status: Optional[str] = None, limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if status:
        rows = _query(
            "SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
    else:
        rows = _query("SELECT * FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@app.get("/api/orders/{order_id}", dependencies=[Depends(auth)])
def get_order(order_id: str):
    row = _query("SELECT * FROM orders WHERE order_id=?", (order_id,), one=True)
    if not row:
        raise HTTPException(status_code=404, detail="订单不存在")
    logs = _query(
        "SELECT action, actor_id, detail, created_at FROM order_audit_logs WHERE order_id=? ORDER BY created_at DESC LIMIT 30",
        (order_id,),
    )
    return {"order": dict(row), "audit_logs": [dict(x) for x in logs]}


@app.get("/api/payment-settings", dependencies=[Depends(auth)])
def get_payment_settings():
    def val(key: str, default: str = "1"):
        row = _query("SELECT value FROM settings WHERE key=?", (key,), one=True)
        return str(row["value"]) if row else default

    return {
        "alipay_enabled": val("alipay_enabled") in {"1", "true", "True"},
        "alipay_token_enabled": val("alipay_token_enabled") in {"1", "true", "True"},
        "alipay_qr_enabled": val("alipay_qr_enabled") in {"1", "true", "True"},
        "wechat_enabled": val("wechat_enabled") in {"1", "true", "True"},
        "alipay_qr_file_id": val("alipay_qr_file_id", ""),
        "wechat_qr_file_id": val("wechat_qr_file_id", ""),
    }


@app.post("/api/payment-settings", dependencies=[Depends(auth)])
def set_payment_settings(payload: PaymentSettingsPayload):
    _execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('alipay_enabled',?)", ("1" if payload.alipay_enabled else "0",))
    _execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('alipay_token_enabled',?)", ("1" if payload.alipay_token_enabled else "0",))
    _execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('alipay_qr_enabled',?)", ("1" if payload.alipay_qr_enabled else "0",))
    _execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('wechat_enabled',?)", ("1" if payload.wechat_enabled else "0",))
    return {"ok": True}
