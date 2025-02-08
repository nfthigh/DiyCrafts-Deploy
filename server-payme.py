# combined_server.py

import os
import sys
import logging
import time
import uuid
import base64
import hashlib
import json
import threading
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, Blueprint, request, jsonify
from dotenv import load_dotenv
import requests

# Загружаем переменные окружения из .env
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("combined_server")

app = Flask(__name__)
app.logger = logger

# Подключение к PostgreSQL (используем psycopg2)
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не установлен")
try:
    db_conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    db_conn.autocommit = True
    db_cursor = db_conn.cursor(cursor_factory=RealDictCursor)
    app.logger.info("Подключение к PostgreSQL выполнено успешно.")
except Exception as e:
    app.logger.error("Ошибка подключения к PostgreSQL: %s", e)
    raise

# Создаем таблицу orders, если не существует
create_orders_table = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    user_id BIGINT,
    product TEXT,
    quantity INTEGER,
    design_text TEXT,
    design_photo TEXT,
    location_lat REAL,
    location_lon REAL,
    cost_info TEXT,
    status TEXT,
    merchant_trans_id TEXT,
    order_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivery_comment TEXT,
    admin_price REAL,
    payment_url TEXT,
    is_paid INTEGER DEFAULT 0,
    items JSONB
)
"""
try:
    db_cursor.execute(create_orders_table)
    app.logger.info("Таблица orders создана или уже существует.")
except Exception as e:
    app.logger.error("Ошибка создания таблицы orders: %s", e)
    raise

#########################################
# Blueprint для Click API (без изменений)
#########################################
click_bp = Blueprint("click_bp", __name__, url_prefix="/click-api")

@click_bp.route("/create_invoice", methods=["POST"])
def create_invoice():
    app.logger.info("Click: Received create_invoice request: %s", request.data.decode())
    data = request.get_json() or request.form
    app.logger.info("Click: Request data: %s", data)

    required_fields = ["merchant_trans_id", "amount", "phone_number"]
    for field in required_fields:
        if field not in data:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    merchant_trans_id = data["merchant_trans_id"]
    try:
        amount = float(data["amount"])
    except Exception as e:
        error_msg = f"Ошибка преобразования amount: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400
    phone_number = data["phone_number"]

    def generate_auth_header():
        timestamp = str(int(time.time()))
        secret_key = os.getenv("SECRET_KEY")
        digest = hashlib.sha1((timestamp + secret_key).encode('utf-8')).hexdigest()
        header = f"{os.getenv('MERCHANT_USER_ID')}:{digest}:{timestamp}"
        app.logger.info("Click: Generated auth header: %s", header)
        return header

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Auth": generate_auth_header(),
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9"
    }
    payload = {
        "service_id": int(os.getenv("SERVICE_ID")),
        "amount": amount,
        "phone_number": phone_number,
        "merchant_trans_id": merchant_trans_id
    }
    app.logger.info("Click: Payload for invoice creation: %s", json.dumps(payload, indent=2))
    try:
        resp = requests.post("https://api.click.uz/v2/merchant/invoice/create",
                             headers=headers,
                             json=payload,
                             timeout=30)
        app.logger.info("Click: Invoice creation HTTP status: %s", resp.status_code)
        if resp.status_code != 200:
            app.logger.error("Click: Invoice creation failed: %s", resp.text)
            return jsonify({
                "error": "-9",
                "error_note": "Invoice creation failed",
                "http_code": resp.status_code,
                "response": resp.text
            }), 200
        invoice_data = resp.json()
        app.logger.info("Click: Invoice created: %s", json.dumps(invoice_data, indent=2))
        return jsonify(invoice_data), 200
    except Exception as e:
        app.logger.error("Click: Invoice creation exception: %s", str(e))
        return jsonify({"error": "-9", "error_note": str(e)}), 200

@click_bp.route("/prepare", methods=["POST"])
def prepare():
    app.logger.info("Click: Received prepare request: %s", request.data.decode())
    required_fields = ["click_trans_id", "merchant_trans_id", "amount"]
    for field in required_fields:
        if field not in request.form:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    click_trans_id = request.form["click_trans_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    app.logger.info("Click: Prepare: click_trans_id=%s, merchant_trans_id=%s", click_trans_id, merchant_trans_id)
    db_cursor.execute("UPDATE orders SET status=%s, cost_info=%s WHERE merchant_trans_id=%s", 
                   ("pending", click_trans_id, merchant_trans_id))
    if db_cursor.rowcount == 0:
        db_cursor.execute("INSERT INTO orders (merchant_trans_id, status, cost_info) VALUES (%s, %s, %s)",
                       (merchant_trans_id, "pending", click_trans_id))
        app.logger.info("Click: New order created in prepare mode.")
    else:
        app.logger.info("Click: Order updated in prepare mode.")
    response = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_prepare_id": merchant_trans_id,
        "error": "0",
        "error_note": "Success"
    }
    app.logger.info("Click: Prepare response: %s", json.dumps(response, indent=2))
    return jsonify(response)

@click_bp.route("/complete", methods=["POST"])
def complete():
    app.logger.info("Click: Received complete request: %s", request.data.decode())
    required_fields = ["click_trans_id", "merchant_trans_id", "merchant_prepare_id", "amount"]
    for field in required_fields:
        if field not in request.form:
            error_msg = f"Missing field: {field}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    click_trans_id = request.form["click_trans_id"]
    merchant_trans_id = request.form["merchant_trans_id"]
    merchant_prepare_id = request.form["merchant_prepare_id"]
    try:
        amount = float(request.form["amount"])
    except Exception as e:
        error_msg = f"Ошибка преобразования amount: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    db_cursor.execute("SELECT admin_price FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    row = db_cursor.fetchone()
    app.logger.info("Click: Data for unit_price: %s", row)
    if row and row.get("admin_price"):
        admin_price = float(row["admin_price"])
        unit_price = admin_price * 100
        app.logger.info("Click: unit_price from DB: admin_price=%s, unit_price=%s", admin_price, unit_price)
    else:
        error_msg = "Missing field: unit_price and not retrieved from DB"
        app.logger.error(error_msg)
        return jsonify({"error": "-8", "error_note": error_msg}), 400

    quantity_str = request.form.get("quantity")
    if quantity_str:
        try:
            quantity = int(quantity_str)
        except Exception as e:
            error_msg = f"Ошибка преобразования quantity: {e}"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400
    else:
        db_cursor.execute("SELECT quantity FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
        row = db_cursor.fetchone()
        if row and row.get("quantity"):
            quantity = int(row["quantity"])
            app.logger.info("Click: Quantity from DB: %s", quantity)
        else:
            error_msg = "Missing field: quantity and not retrieved from DB"
            app.logger.error(error_msg)
            return jsonify({"error": "-8", "error_note": error_msg}), 400

    db_cursor.execute("SELECT product FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    row = db_cursor.fetchone()
    if row and row.get("product"):
        product_name = row["product"]
    else:
        product_name = "Unknown product"

    app.logger.info(
        "Click /complete parameters: click_trans_id=%s, merchant_trans_id=%s, amount=%s, product_name=%s, quantity=%s, unit_price=%s",
        click_trans_id, merchant_trans_id, amount, product_name, quantity, unit_price
    )

    db_cursor.execute("SELECT * FROM orders WHERE merchant_trans_id=%s", (merchant_trans_id,))
    order_row = db_cursor.fetchone()
    app.logger.info("Click: Order contents: %s", order_row)
    if not order_row:
        error_msg = "Order not found"
        app.logger.error(error_msg)
        return jsonify({"error": "-5", "error_note": error_msg}), 404
    if order_row.get("is_paid") == 1:
        error_msg = "Already paid"
        app.logger.error(error_msg)
        return jsonify({"error": "-4", "error_note": error_msg}), 400

    db_cursor.execute("UPDATE orders SET is_paid=1, status='processing' WHERE merchant_trans_id=%s", (merchant_trans_id,))
    db_conn.commit()

    try:
        from fiscal import create_fiscal_item
        fiscal_item = create_fiscal_item(product_name, quantity, unit_price)
        fiscal_items = [fiscal_item]
        app.logger.info("Fiscal data: %s", json.dumps(fiscal_items, indent=2, ensure_ascii=False))
    except Exception as e:
        error_msg = f"Fiscal data error: {e}"
        app.logger.error(error_msg)
        return jsonify({"error": "-10", "error_note": error_msg}), 400

    fiscal_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Auth": generate_auth_header(),
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9"
    }
    fiscal_payload = {
        "service_id": int(os.getenv("SERVICE_ID")),
        "payment_id": click_trans_id,
        "items": fiscal_items,
        "received_ecash": amount,
        "received_cash": 0,
        "received_card": 0
    }
    app.logger.info("Fiscal payload: %s", json.dumps(fiscal_payload, indent=2, ensure_ascii=False))
    try:
        resp_fiscal = requests.post("https://api.click.uz/v2/merchant/payment/ofd_data/submit_items",
                                      headers=fiscal_headers,
                                      json=fiscal_payload,
                                      timeout=30)
        if resp_fiscal.status_code == 200:
            fiscal_result = resp_fiscal.json()
            app.logger.info("Fiscal response: %s", json.dumps(fiscal_result, indent=2, ensure_ascii=False))
        else:
            fiscal_result = {"error_code": -1, "raw": resp_fiscal.text}
            app.logger.error("Fiscal error, status %s: %s", resp_fiscal.status_code, resp_fiscal.text)
    except Exception as e:
        fiscal_result = {"error_code": -1, "error_note": str(e)}
        app.logger.error("Fiscal exception: %s", e)

    db_cursor.execute("UPDATE orders SET status='completed' WHERE merchant_trans_id=%s", (merchant_trans_id,))
    db_conn.commit()

    response = {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_confirm_id": merchant_prepare_id,
        "error": "0",
        "error_note": "Success",
        "fiscal_items": fiscal_items,
        "fiscal_response": fiscal_result
    }
    app.logger.info("Complete response: %s", json.dumps(response, indent=2, ensure_ascii=False))
    return jsonify(response)

def auto_ping():
    while True:
        try:
            app.logger.info("Auto-ping: Sending request to %s", os.getenv("SELF_URL"))
            requests.get(os.getenv("SELF_URL"), timeout=10)
        except Exception as e:
            app.logger.error("Auto-ping error: %s", e)
        time.sleep(240)

ping_thread = threading.Thread(target=auto_ping, daemon=True)
ping_thread.start()

#########################################
# Blueprint для Payme API
#########################################
payme_bp = Blueprint("payme_bp", __name__, url_prefix="/payme-api")

# Функция-утилита для формирования ошибок (используется в payme callback)
def make_error_response(req_id, code, message, data=None):
    return {
        "error": {
            "code": code,
            "message": {
                "ru": message,
                "uz": message,
                "en": message
            },
            "data": data
        },
        "id": req_id
    }

def error_authorization(req_id):
    return make_error_response(req_id, -32504, "Error during authorization", None)

def error_invalid_json():
    return make_error_response(0, -32700, "Could not parse JSON", None)

def error_order_id(req_id):
    return make_error_response(req_id, -31099, "Order number cannot be found", "order")

def error_amount(req_id):
    return make_error_response(req_id, -31001, "Order amount is incorrect", "amount")

def error_has_another_transaction(req_id):
    return make_error_response(req_id, -31099, "Other transaction for this order is in progress", "order")

def error_unknown(req_id):
    return make_error_response(req_id, -31008, "Unknown error", None)

def error_transaction(req_id):
    return make_error_response(req_id, -31003, "Transaction number is wrong", "id")

def error_cancelled_transaction(req_id):
    return make_error_response(req_id, -31008, "Transaction was cancelled or refunded", "order")

def error_cancel(req_id):
    return make_error_response(req_id, -31007, "It is impossible to cancel. The order is completed", "order")

def error_password(req_id):
    return make_error_response(req_id, -32400, "Cannot change the password", "password")

def error_unknown_method(req_id, method):
    return make_error_response(req_id, -32601, "Unknown method", method)

# Эндпоинт для создания заказа через Payme (используется ботом)
@payme_bp.route("/order/create", methods=["POST"])
def payme_create_order():
    data = request.get_json()
    if not data or "amount" not in data or "items" not in data:
        return jsonify({"error": "Invalid request, missing amount or items"}), 400
    try:
        amount = float(data["amount"])
    except Exception as e:
        return jsonify({"error": "Invalid amount", "detail": str(e)}), 400
    order_id = str(uuid.uuid4())
    amount_coins = int(round(amount * 100))
    items = data["items"]
    try:
        query = "INSERT INTO orders (order_id, amount, status, items) VALUES (%s, %s, %s, %s)"
        db_cursor.execute(query, (order_id, amount_coins, "new", json.dumps(items)))
    except Exception as e:
        app.logger.error("Payme: Error inserting order: %s", e)
        return jsonify({"error": "Database error", "detail": str(e)}), 500
    app.logger.info(f"Payme: Order created: order_id={order_id}, amount={amount_coins}, items={items}")
    return jsonify({"order_id": order_id, "amount": amount_coins, "status": "new"}), 200

# Эндпоинт для получения статуса заказа через Payme
@payme_bp.route("/order/status/<order_id>", methods=["GET"])
def payme_order_status(order_id):
    query = "SELECT * FROM orders WHERE order_id = %s"
    db_cursor.execute(query, (order_id,))
    order = db_cursor.fetchone()
    if not order:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(order), 200

# Единый endpoint для обработки JSON-RPC запросов от Payme
@payme_bp.route("/payme/callback", methods=["POST"])
def payme_callback():
    try:
        req_json = request.get_json()
    except Exception as e:
        app.logger.error("Payme: Failed to parse JSON: %s", e)
        return jsonify(error_invalid_json()), 200
    for field in ["jsonrpc", "method", "params", "id"]:
        if field not in req_json:
            app.logger.error("Payme: Missing field %s in request", field)
            return jsonify(error_invalid_json()), 200
    req_id = req_json["id"]
    app.logger.info(f"Payme: Received callback: method={req_json['method']}, id={req_id}, params={req_json['params']}")
    expected_auth = "Basic " + base64.b64encode(f"Paycom:{os.getenv('MERCHANT_KEY')}".encode()).decode()
    auth_header = request.headers.get("Authorization", "")
    if auth_header.strip() != expected_auth:
        app.logger.warning(f"Payme: Authorization failed for req_id={req_id}")
        return jsonify(error_authorization(req_id)), 200
    method = req_json["method"]
    params = req_json["params"]
    if method == "CheckPerformTransaction":
        response = payme_check_perform_transaction(params, req_id)
    elif method == "CreateTransaction":
        response = payme_create_transaction(params, req_id)
    elif method == "PerformTransaction":
        response = payme_perform_transaction(params, req_id)
    elif method == "CheckTransaction":
        response = payme_check_transaction(params, req_id)
    elif method == "CancelTransaction":
        response = payme_cancel_transaction(params, req_id)
    elif method == "ChangePassword":
        response = payme_change_password(params, req_id)
    else:
        app.logger.warning(f"Payme: Unknown method: {method}")
        response = error_unknown_method(req_id, method)
    app.logger.info(f"Payme: Response for req_id={req_id}: {response}")
    return jsonify(response), 200

def payme_check_perform_transaction(params, req_id):
    account = params.get("account", {})
    order_id = account.get("order_id")
    if not order_id:
        app.logger.error(f"Payme: CheckPerformTransaction missing order_id, req_id={req_id}")
        return error_order_id(req_id)
    query = "SELECT * FROM orders WHERE order_id = %s"
    db_cursor.execute(query, (order_id,))
    order = db_cursor.fetchone()
    if not order:
        app.logger.error(f"Payme: CheckPerformTransaction order not found, order_id={order_id}, req_id={req_id}")
        return error_order_id(req_id)
    if order["amount"] != params.get("amount"):
        app.logger.error(f"Payme: CheckPerformTransaction amount mismatch for order_id={order_id}, req_id={req_id}")
        return error_amount(req_id)
    try:
        items_detail = json.loads(order["items"]) if isinstance(order["items"], str) else order["items"]
    except Exception:
        items_detail = order["items"]
    result = {
        "allow": True,
        "detail": {
            "receipt_type": 0,
            "items": items_detail
        }
    }
    app.logger.info(f"Payme: CheckPerformTransaction OK for order_id={order_id}, req_id={req_id}")
    return {"result": result, "id": req_id}

def payme_create_transaction(params, req_id):
    account = params.get("account", {})
    order_id = account.get("order_id")
    if not order_id:
        app.logger.error(f"Payme: CreateTransaction missing order_id, req_id={req_id}")
        return error_order_id(req_id)
    query = "SELECT * FROM orders WHERE order_id = %s"
    db_cursor.execute(query, (order_id,))
    order = db_cursor.fetchone()
    if not order:
        app.logger.error(f"Payme: CreateTransaction order not found, order_id={order_id}, req_id={req_id}")
        return error_order_id(req_id)
    if order["amount"] != params.get("amount"):
        app.logger.error(f"Payme: CreateTransaction amount mismatch for order_id={order_id}, req_id={req_id}")
        return error_amount(req_id)
    transaction_id = params.get("id")
    current_time = int(time.time() * 1000)
    if order["status"] == "new":
        update_query = "UPDATE orders SET create_time=%s, transaction_id=%s, status=%s WHERE order_id=%s"
        db_cursor.execute(update_query, (current_time, transaction_id, "processing", order_id))
        result = {
            "create_time": current_time,
            "transaction": "000" + order_id,
            "state": 1
        }
        app.logger.info(f"Payme: CreateTransaction new transaction for order_id={order_id}, req_id={req_id}")
        return {"result": result, "id": req_id}
    elif order["status"] == "processing":
        if order["transaction_id"] == transaction_id:
            result = {
                "create_time": order.get("create_time"),
                "transaction": "000" + order_id,
                "state": 1
            }
            app.logger.info(f"Payme: CreateTransaction existing transaction for order_id={order_id}, req_id={req_id}")
            return {"result": result, "id": req_id}
        else:
            app.logger.error(f"Payme: CreateTransaction different transaction exists for order_id={order_id}, req_id={req_id}")
            return error_has_another_transaction(req_id)
    else:
        app.logger.error(f"Payme: CreateTransaction unknown state for order_id={order_id}, req_id={req_id}")
        return error_unknown(req_id)

def payme_perform_transaction(params, req_id):
    account = params.get("account", {})
    order_id = account.get("order_id")
    if not order_id:
        app.logger.error(f"Payme: PerformTransaction missing order_id, req_id={req_id}")
        return error_order_id(req_id)
    query = "SELECT * FROM orders WHERE order_id = %s"
    db_cursor.execute(query, (order_id,))
    order = db_cursor.fetchone()
    if not order:
        app.logger.error(f"Payme: PerformTransaction order not found, order_id={order_id}, req_id={req_id}")
        return error_order_id(req_id)
    transaction_id = params.get("id")
    if order["transaction_id"] != transaction_id:
        app.logger.error(f"Payme: PerformTransaction transaction ID mismatch for order_id={order_id}, req_id={req_id}")
        return error_transaction(req_id)
    if order["status"] == "processing":
        current_time = int(time.time() * 1000)
        update_query = "UPDATE orders SET perform_time=%s, status=%s WHERE order_id=%s"
        db_cursor.execute(update_query, (current_time, "completed", order_id))
        result = {
            "transaction": "000" + order_id,
            "perform_time": current_time,
            "state": 2
        }
        app.logger.info(f"Payme: PerformTransaction completed for order_id={order_id}, req_id={req_id}")
        return {"result": result, "id": req_id}
    elif order["status"] == "completed":
        result = {
            "transaction": "000" + order_id,
            "perform_time": order.get("perform_time"),
            "state": 2
        }
        app.logger.info(f"Payme: PerformTransaction already completed for order_id={order_id}, req_id={req_id}")
        return {"result": result, "id": req_id}
    elif order["status"] in ["cancelled", "refunded"]:
        app.logger.error(f"Payme: PerformTransaction cancelled/refunded for order_id={order_id}, req_id={req_id}")
        return error_cancelled_transaction(req_id)
    else:
        app.logger.error(f"Payme: PerformTransaction unknown error for order_id={order_id}, req_id={req_id}")
        return error_unknown(req_id)

def payme_check_transaction(params, req_id):
    account = params.get("account", {})
    order_id = account.get("order_id")
    if not order_id:
        app.logger.error(f"Payme: CheckTransaction missing order_id, req_id={req_id}")
        return error_order_id(req_id)
    query = "SELECT * FROM orders WHERE order_id = %s"
    db_cursor.execute(query, (order_id,))
    order = db_cursor.fetchone()
    if not order:
        app.logger.error(f"Payme: CheckTransaction order not found, order_id={order_id}, req_id={req_id}")
        return error_order_id(req_id)
    transaction_id = params.get("id")
    if order["transaction_id"] != transaction_id:
        app.logger.error(f"Payme: CheckTransaction transaction ID mismatch for order_id={order_id}, req_id={req_id}")
        return error_transaction(req_id)
    state_val = 0
    if order["status"] == "processing":
        state_val = 1
    elif order["status"] == "completed":
        state_val = 2
    elif order["status"] == "cancelled":
        state_val = -1
    elif order["status"] == "refunded":
        state_val = -2
    result = {
        "create_time": order.get("create_time") or 0,
        "perform_time": order.get("perform_time") or 0,
        "cancel_time": order.get("cancel_time") or 0,
        "transaction": "000" + order_id,
        "state": state_val,
        "reason": order.get("cancel_reason")
    }
    app.logger.info(f"Payme: CheckTransaction for order_id={order_id}, req_id={req_id}, state={state_val}")
    return {"result": result, "id": req_id}

def payme_cancel_transaction(params, req_id):
    account = params.get("account", {})
    order_id = account.get("order_id")
    if not order_id:
        app.logger.error(f"Payme: CancelTransaction missing order_id, req_id={req_id}")
        return error_order_id(req_id)
    query = "SELECT * FROM orders WHERE order_id = %s"
    db_cursor.execute(query, (order_id,))
    order = db_cursor.fetchone()
    if not order:
        app.logger.error(f"Payme: CancelTransaction order not found, order_id={order_id}, req_id={req_id}")
        return error_order_id(req_id)
    transaction_id = params.get("id")
    if order["transaction_id"] != transaction_id:
        app.logger.error(f"Payme: CancelTransaction transaction ID mismatch for order_id={order_id}, req_id={req_id}")
        return error_transaction(req_id)
    current_time = int(time.time() * 1000)
    result = {"transaction": "000" + order_id, "cancel_time": current_time, "state": None}
    if order["status"] in ["new", "processing"]:
        update_query = "UPDATE orders SET cancel_time=%s, status=%s, cancel_reason=%s WHERE order_id=%s"
        db_cursor.execute(update_query, (current_time, "cancelled", params.get("reason"), order_id))
        result["state"] = -1
    elif order["status"] == "completed":
        update_query = "UPDATE orders SET cancel_time=%s, status=%s, cancel_reason=%s WHERE order_id=%s"
        db_cursor.execute(update_query, (current_time, "refunded", params.get("reason"), order_id))
        result["state"] = -2
    elif order["status"] in ["cancelled", "refunded"]:
        result["cancel_time"] = order.get("cancel_time")
        result["state"] = -1 if order["status"] == "cancelled" else -2
    else:
        app.logger.error(f"Payme: CancelTransaction unknown error for order_id={order_id}, req_id={req_id}")
        return error_cancel(req_id)
    app.logger.info(f"Payme: CancelTransaction completed for order_id={order_id}, req_id={req_id}")
    return {"result": result, "id": req_id}

def payme_change_password(params, req_id):
    new_password = params.get("password")
    if new_password and new_password != os.getenv("MERCHANT_KEY"):
        os.environ["MERCHANT_KEY"] = new_password
        app.logger.info(f"Payme: ChangePassword succeeded, req_id={req_id}")
        return {"result": {"success": True}, "id": req_id}
    else:
        app.logger.error(f"Payme: ChangePassword failed (invalid or same password), req_id={req_id}")
        return error_password(req_id)

#########################################
# Регистрация Blueprint'ов в приложении
#########################################
app.register_blueprint(click_bp)
app.register_blueprint(payme_bp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
