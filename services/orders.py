import logging
import time
import uuid

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"

logger = logging.getLogger(__name__)


def _mask_payment_text(payment_text: str) -> str:
    if not payment_text:
        return ""
    text = str(payment_text).strip()
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * (len(text) - 8)}{text[-4:]}"


def create_order(db_query, db_execute, tg_id, plan_key, order_type, target_uuid, menu_message_id=None):
    existing = db_query(
        "SELECT * FROM orders WHERE tg_id=? AND status=? ORDER BY created_at DESC LIMIT 1",
        (tg_id, STATUS_PENDING),
        one=True,
    )
    if existing:
        logger.info("reusing pending order for tg_id=%s order_id=%s", tg_id, existing["order_id"])
        return dict(existing), False

    now = int(time.time())
    order_id = uuid.uuid4().hex[:12]
    db_execute(
        """INSERT INTO orders
        (order_id, tg_id, plan_key, order_type, target_uuid, status, menu_message_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, tg_id, plan_key, order_type, target_uuid, STATUS_PENDING, menu_message_id, now, now),
    )
    created = db_query("SELECT * FROM orders WHERE order_id=?", (order_id,), one=True)
    logger.info("created order order_id=%s tg_id=%s type=%s", order_id, tg_id, order_type)
    return dict(created), True


def get_order(db_query, order_id):
    row = db_query("SELECT * FROM orders WHERE order_id=?", (order_id,), one=True)
    return dict(row) if row else None


def update_order_status(db_execute, order_id, from_statuses, to_status, error_message=None, delivered_uuid=None):
    now = int(time.time())
    placeholders = ",".join(["?"] * len(from_statuses))
    query = f"""UPDATE orders SET status=?, updated_at=?, error_message=?, delivered_uuid=?
    WHERE order_id=? AND status IN ({placeholders})"""
    args = (to_status, now, error_message, delivered_uuid, order_id, *from_statuses)
    changed = db_execute(query, args)
    if changed > 0:
        logger.info("order status updated order_id=%s -> %s", order_id, to_status)
    return changed > 0


def attach_payment_text(db_execute, order_id, payment_text, waiting_message_id=None):
    now = int(time.time())
    masked_payment_text = _mask_payment_text(payment_text)
    db_execute(
        "UPDATE orders SET payment_text=?, waiting_message_id=?, updated_at=? WHERE order_id=?",
        (masked_payment_text, waiting_message_id, now, order_id),
    )


def attach_admin_message(db_execute, order_id, admin_message_id):
    now = int(time.time())
    db_execute("UPDATE orders SET admin_message_id=?, updated_at=? WHERE order_id=?", (admin_message_id, now, order_id))


def get_pending_order_for_user(db_query, tg_id):
    row = db_query(
        "SELECT * FROM orders WHERE tg_id=? AND status=? ORDER BY created_at DESC LIMIT 1",
        (tg_id, STATUS_PENDING),
        one=True,
    )
    return dict(row) if row else None
