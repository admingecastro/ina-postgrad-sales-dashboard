#!/usr/bin/env python3
"""Fetch Hotmart sales/subscriptions and build a PII-free dashboard artifact."""
from __future__ import annotations

import base64
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "site-template.html"
OUT_DIR = ROOT / "dist"
TZ = ZoneInfo("America/Sao_Paulo")
API_BASE = "https://developers.hotmart.com/payments/api/v1"
AUTH_URL = "https://api-sec-vlc.hotmart.com/security/oauth/token"
MAX_RESULTS = 50
MAX_PAGES = 100
CENT = Decimal("0.01")

# Product IDs and modality mapping come from the INA offers PDF already used
# to build the original dashboard. No buyer fields are written to the artifact.
PROGRAMS = [
    {"name": "Neurociências e Autismo", "full": 5734705, "rec": 8867217},
    {"name": "Análise do Comportamento Aplicada (ABA) ao TEA", "full": 8868805, "rec": 8869008},
    {"name": "TDAH: avaliação e abordagens", "full": 8868864, "rec": 8869071},
    {"name": "Neurociências da Aprendizagem e seus Transtornos", "full": 8868887, "rec": 8869099},
    {"name": "Psiquiatria da Infância e Adolescência", "full": 8868908, "rec": 8869126},
    {"name": "Neuropediatria", "full": 8870256, "rec": 8870337},
    {"name": "Neuropsicologia", "full": 8868930, "rec": 8869162},
    {"name": "Altas Habilidades/Superdotação e Dupla Excepcionalidade", "full": 8868945, "rec": 8869187},
]
PROGRAM_BY_ID: dict[int, tuple[str, str]] = {}
for _program in PROGRAMS:
    PROGRAM_BY_ID[_program["full"]] = (_program["name"], "Produto cheio")
    PROGRAM_BY_ID[_program["rec"]] = (_program["name"], "Recorrência (*R)")
REC_PRODUCT_IDS = [p["rec"] for p in PROGRAMS]
ALL_PRODUCT_IDS = [pid for p in PROGRAMS for pid in (p["full"], p["rec"])]

# Fallback cycle prices are used only when an active quarterly plan has no
# matching approved sale and the subscription record has no usable price.
FALLBACK_CYCLE_PRICE = {
    8867217: 867.0,
    8869008: 299.0,
    8869071: 299.0,
    8869099: 299.0,
    8869126: None,
    8870337: None,
    8869162: None,
    8869187: 299.0,
}
SUBSCRIPTION_STATUSES = [
    "ACTIVE", "INACTIVE", "DELAYED", "CANCELLED_BY_CUSTOMER",
    "CANCELLED_BY_SELLER", "CANCELLED_BY_ADMIN", "STARTED", "OVERDUE",
]


def money(value) -> float:
    if value is None or value == "":
        value = 0
    return float(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP))


def dec(value) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def money_br(value) -> str:
    return f"{dec(value):.2f}".replace(".", ",")


def as_timestamp_seconds(value) -> float | None:
    if value in (None, "", 0, "0"):
        return None
    number = float(value)
    # Hotmart sales history uses milliseconds; subscription examples may use seconds.
    return number / 1000.0 if number > 100_000_000_000 else number


def local_datetime(value) -> datetime | None:
    seconds = as_timestamp_seconds(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(TZ)


def local_datetime_text(value) -> str:
    dt = local_datetime(value)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def local_date_text(value) -> str:
    dt = local_datetime(value)
    return dt.strftime("%Y-%m-%d") if dt else ""


def int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def get_access_token() -> str:
    client_id = os.environ.get("INA_HOTMART_DASH_REFRESH_CLIENT_ID_20261106", "").strip()
    client_secret = os.environ.get("INA_HOTMART_DASH_REFRESH_CLIENT_SECRET_20261106", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("Secrets Hotmart ausentes no ambiente do workflow.")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    params = urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    })
    request = Request(
        f"{AUTH_URL}?{params}",
        data=b"",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Never print the exception or URL: the OAuth query contains client_secret.
        raise RuntimeError(f"Hotmart OAuth falhou (HTTP {exc.code}).") from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise RuntimeError("Falha de conexão ou resposta inválida ao autenticar na Hotmart.") from None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token:
        raise RuntimeError("Hotmart OAuth não retornou access_token.")
    return token


def hotmart_json(path: str, params: list[tuple[str, str]], token: str, method: str = "GET") -> dict:
    query = urlencode(params, doseq=True)
    url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
    request = Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    delays = (2, 5, 10, 20, 30)
    for attempt in range(len(delays) + 1):
        try:
            with urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"Resposta inesperada da Hotmart em {path}.")
            return payload
        except HTTPError as exc:
            if exc.code == 429 and attempt < len(delays):
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    pause = min(60, max(1, int(retry_after))) if retry_after else delays[attempt]
                except ValueError:
                    pause = delays[attempt]
                time.sleep(pause)
                continue
            if exc.code >= 500 and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            # Only the path and status are logged; response bodies may contain PII.
            raise RuntimeError(f"Hotmart API falhou em {path} (HTTP {exc.code}); a publicação foi interrompida.") from None
        except (URLError, TimeoutError):
            if attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            raise RuntimeError(f"Falha de conexão com Hotmart em {path}; a publicação foi interrompida.") from None
        except json.JSONDecodeError:
            raise RuntimeError(f"Resposta JSON inválida em {path}; a publicação foi interrompida.") from None
    raise RuntimeError(f"Hotmart API indisponível em {path}.")


def get_all(path: str, base_params: list[tuple[str, str]], token: str) -> list[dict]:
    items: list[dict] = []
    page_token = None
    for _ in range(MAX_PAGES):
        params = list(base_params) + [("max_results", str(MAX_RESULTS))]
        if page_token:
            params.append(("page_token", page_token))
        payload = hotmart_json(path, params, token)
        page_items = payload.get("items", [])
        if not isinstance(page_items, list):
            raise RuntimeError(f"Formato inesperado de items em {path}.")
        items.extend(x for x in page_items if isinstance(x, dict))
        info = payload.get("page_info") or {}
        next_token = info.get("next_page_token") if isinstance(info, dict) else None
        if not next_token or next_token == page_token:
            return items
        page_token = str(next_token)
    raise RuntimeError(f"Paginação excedeu o limite defensivo em {path}.")


def product_id_of(row: dict) -> int | None:
    return int_or_none((row.get("product") or {}).get("id"))


def purchase_of(row: dict) -> dict:
    purchase = row.get("purchase")
    return purchase if isinstance(purchase, dict) else {}


def transaction_of(row: dict) -> str:
    return str(purchase_of(row).get("transaction") or "").strip()


def buyer_ucode_of(row: dict) -> str:
    buyer = row.get("buyer") or {}
    return str(buyer.get("ucode") or "").strip()


def price_of(row: dict) -> tuple[float, str]:
    price = purchase_of(row).get("price") or {}
    return money(price.get("value")), str(price.get("currency_code") or "").upper()


def normalize_plan(name, product_id, period=None, max_cycles=None, offer_code=None) -> str:
    n = str(name or "").strip()
    low = n.lower()
    if period == 180 or "semestr" in low or "semestr" in str(offer_code or "").lower():
        return "Semestral"
    if period == 90 or "trim" in low:
        return "Trimestral"
    if max_cycles == 36 or "36x" in low:
        return "Mensal 36x"
    if max_cycles == 24 or "24x" in low:
        return "Mensal 24x"
    if max_cycles == 12 or "12x" in low:
        return "Mensal 12x"
    if n:
        return n.title()
    if period == 30:
        return "Mensal"
    return "Mensal 12x"


def plan_info(sub: dict) -> dict:
    plan = sub.get("plan") or {}
    if not isinstance(plan, dict):
        plan = {}
    period = int_or_none(plan.get("recurrency_period"))
    name = plan.get("name")
    max_cycles = int_or_none(plan.get("max_charge_cycles"))
    if not period:
        low = str(name or "").lower()
        period = 180 if "semestr" in low else (90 if "trim" in low else 30)
    if not max_cycles:
        if period == 180:
            max_cycles = 2
        elif period == 90:
            max_cycles = 4
        elif "36x" in str(name or "").lower():
            max_cycles = 36
        else:
            max_cycles = 12
    return {"name": name, "period": period, "max_cycles": max_cycles, "id": plan.get("id")}


def buyer_document_from_participant(item: dict) -> str | None:
    users = item.get("users") or []
    for participant in users:
        if not isinstance(participant, dict) or str(participant.get("role", "")).upper() != "BUYER":
            continue
        user = participant.get("user") or {}
        for doc in user.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            digits = re.sub(r"\D", "", str(doc.get("value") or ""))
            doc_type = str(doc.get("type") or "").upper()
            if len(digits) == 11 and doc_type in ("CPF", "DOCUMENT"):
                return digits
            if len(digits) == 14 and doc_type in ("CNPJ", "DOCUMENT"):
                return digits
    return None


def collect_sales(token: str, product_ids: list[int], status: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for pid in product_ids:
        params = [("product_id", str(pid))]
        if status:
            params.append(("transaction_status", status))
        rows.extend(get_all("/sales/history", params, token))
    return rows


def collect_subscriptions(token: str) -> list[dict]:
    rows: list[dict] = []
    # Explicit status and date bounds avoid Hotmart's 30-day default accession window.
    end_next_charge = str(int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp() * 1000))
    for pid in REC_PRODUCT_IDS:
        params = [("product_id", str(pid)), ("accession_date", "0"), ("date_next_charge", "0"), ("end_date_next_charge", end_next_charge)]
        params.extend(("status", value) for value in SUBSCRIPTION_STATUSES)
        rows.extend(get_all("/subscriptions", params, token))
    return rows


def parse_sale_record(row: dict, plan_by_transaction: dict, plan_by_userproduct: dict) -> dict | None:
    purchase = purchase_of(row)
    product_id = product_id_of(row)
    transaction = transaction_of(row)
    if product_id not in PROGRAM_BY_ID or not transaction:
        return None
    status = str(purchase.get("status") or "").upper()
    if status not in ("APPROVED", "COMPLETE"):
        return None
    gross, currency = price_of(row)
    if currency != "BRL":
        return None
    program, modality = PROGRAM_BY_ID[product_id]
    timestamp = purchase.get("approved_date") or purchase.get("order_date")
    dt = local_datetime(timestamp)
    if not dt:
        return None
    pinfo = plan_by_transaction.get(transaction)
    buyer_ucode = buyer_ucode_of(row)
    if not pinfo and buyer_ucode:
        candidates = plan_by_userproduct.get((product_id, buyer_ucode), [])
        unique = {(x.get("name"), x.get("period"), x.get("max_cycles")) for x in candidates}
        if len(unique) == 1 and candidates:
            pinfo = candidates[0]
    if modality == "Recorrência (*R)":
        pinfo = pinfo or {"name": None, "period": None, "max_cycles": None}
        plan_name = normalize_plan(pinfo.get("name"), product_id, pinfo.get("period"), pinfo.get("max_cycles"), purchase.get("offer", {}).get("code"))
    else:
        pinfo = {"period": None, "max_cycles": None}
        plan_name = "Produto cheio"
    fee = money((purchase.get("hotmart_fee") or {}).get("total"))
    payment = purchase.get("payment") or {}
    return {
        "transaction_internal": transaction,
        "buyer_ucode_internal": buyer_ucode,
        "product_id_internal": product_id,
        "date": dt.strftime("%Y-%m-%d"),
        "dateTime": dt.strftime("%Y-%m-%d %H:%M"),
        "program": program,
        "productId": product_id,
        "modality": modality,
        "plan": plan_name,
        "gross": gross,
        "fee": fee,
        "net": money(gross - fee),
        "paymentType": payment.get("type") or payment.get("method") or "Não informado",
        "installments": max(1, int_or_none(payment.get("installments_number")) or 1),
        "recurrenceNumber": int_or_none(purchase.get("recurrency_number")) if modality == "Recorrência (*R)" else None,
        "offerCode_internal": (purchase.get("offer") or {}).get("code"),
        "planInfo_internal": pinfo,
    }


def prepare_pending(token: str, pending_history: list[dict], approved_sales: list[dict]) -> tuple[list[dict], dict]:
    pending_by_tx = {}
    product_stats = defaultdict(lambda: {
        "raw_count": 0, "raw_gross": Decimal("0.00"), "unique_count": 0,
        "unique_gross": Decimal("0.00"), "excluded": 0,
    })
    empty_by_program = {
        p["name"]: {"raw_count": 0, "raw_gross": 0.0, "unique_count": 0, "unique_gross": 0.0, "excluded": 0}
        for p in PROGRAMS
    }
    for row in pending_history:
        tx = transaction_of(row)
        pid = product_id_of(row)
        if not tx or pid not in PROGRAM_BY_ID:
            continue
        purchase = purchase_of(row)
        if str(purchase.get("status") or "").upper() != "WAITING_PAYMENT":
            continue
        gross, currency = price_of(row)
        if currency == "BRL":
            if tx not in pending_by_tx:
                pending_by_tx[tx] = row
                product_stats[pid]["raw_count"] += 1
                product_stats[pid]["raw_gross"] += dec(gross)
    if not pending_by_tx:
        return [], {"raw_count": 0, "raw_gross": 0.0, "excluded": 0, "by_program": empty_by_program}

    pending_product_ids = sorted({product_id_of(row) for row in pending_by_tx.values()})
    participant_by_tx: dict[str, tuple[int, str | None]] = {}
    for pid in pending_product_ids:
        # Sales Participants includes PII, but it stays in memory and is used only
        # for internal CPF/CNPJ deduplication; nothing from it is serialized.
        participants = get_all("/sales/users", [("product_id", str(pid))], token)
        participants += get_all("/sales/users", [("product_id", str(pid)), ("transaction_status", "WAITING_PAYMENT")], token)
        for item in participants:
            tx = str(item.get("transaction") or "").strip()
            actual_pid = product_id_of(item) or pid
            if tx:
                participant_by_tx[tx] = (actual_pid, buyer_document_from_participant(item))

    approved_doc_keys = set()
    for sale in approved_sales:
        pid = sale["product_id_internal"]
        if pid not in pending_product_ids:
            continue
        tx = sale["transaction_internal"]
        participant = participant_by_tx.get(tx)
        doc = participant[1] if participant else None
        if not doc:
            raise RuntimeError("Conciliação pendente interrompida: falta CPF/CNPJ de uma venda aprovada do mesmo produto.")
        approved_doc_keys.add((pid, doc))

    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    raw_gross = Decimal("0.00")
    for tx, row in pending_by_tx.items():
        pid = product_id_of(row)
        gross, _ = price_of(row)
        raw_gross += dec(gross)
        participant = participant_by_tx.get(tx)
        doc = participant[1] if participant else None
        if not doc:
            raise RuntimeError("Conciliação pendente interrompida: falta CPF/CNPJ de uma transação aguardando pagamento.")
        groups[(pid, doc)].append(row)

    selected = []
    excluded = 0
    for (pid, doc), group in groups.items():
        if (pid, doc) in approved_doc_keys:
            excluded += len(group)
            product_stats[pid]["excluded"] += len(group)
            continue
        group.sort(key=lambda item: int_or_none(purchase_of(item).get("order_date")) or 0)
        chosen = group[-1]
        selected.append(chosen)
        excluded += len(group) - 1
        product_stats[pid]["excluded"] += len(group) - 1
        product_stats[pid]["unique_count"] += 1
        product_stats[pid]["unique_gross"] += dec(price_of(chosen)[0])

    pending_sales = []
    for row in selected:
        purchase = purchase_of(row)
        pid = product_id_of(row)
        dt = local_datetime(purchase.get("order_date"))
        gross, currency = price_of(row)
        if currency != "BRL":
            continue
        program, modality = PROGRAM_BY_ID[pid]
        payment = purchase.get("payment") or {}
        pending_sales.append({
            "date": dt.strftime("%Y-%m-%d") if dt else datetime.now(TZ).strftime("%Y-%m-%d"),
            "dateTime": dt.strftime("%Y-%m-%d %H:%M") if dt else "Data não informada",
            "program": program,
            "productId": pid,
            "modality": modality,
            "plan": "Produto cheio" if modality == "Produto cheio" else "Recorrência (*R)",
            "gross": gross,
            "paymentType": payment.get("type") or payment.get("method") or "Não informado",
            "status": "Aguardando pagamento",
        })
    pending_sales.sort(key=lambda row: (row["dateTime"], row["program"]), reverse=True)
    by_program = empty_by_program
    for pid, stats in product_stats.items():
        program = PROGRAM_BY_ID[pid][0]
        target = by_program[program]
        target["raw_count"] += stats["raw_count"]
        target["raw_gross"] = money(Decimal(str(target["raw_gross"])) + stats["raw_gross"])
        target["unique_count"] += stats["unique_count"]
        target["unique_gross"] = money(Decimal(str(target["unique_gross"])) + stats["unique_gross"])
        target["excluded"] += stats["excluded"]
    return pending_sales, {"raw_count": len(pending_by_tx), "raw_gross": money(raw_gross), "excluded": excluded, "by_program": by_program}


def build_dashboard_data(token: str) -> dict:
    approved_raw = collect_sales(token, ALL_PRODUCT_IDS)
    pending_raw = collect_sales(token, ALL_PRODUCT_IDS, "WAITING_PAYMENT")
    cancelled_raw = collect_sales(token, REC_PRODUCT_IDS, "CANCELLED")
    no_funds_raw = collect_sales(token, REC_PRODUCT_IDS, "NO_FUNDS")
    overdue_raw = collect_sales(token, REC_PRODUCT_IDS, "OVERDUE")
    subscriptions = collect_subscriptions(token)

    # Enrich sale rows with plan metadata from subscription records.
    plan_by_transaction: dict[str, dict] = {}
    plan_by_userproduct: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for sub in subscriptions:
        pid = product_id_of(sub)
        if pid not in REC_PRODUCT_IDS:
            continue
        plan = plan_info(sub)
        transaction = str(sub.get("transaction") or "").strip()
        if transaction:
            plan_by_transaction[transaction] = plan
        subscriber = sub.get("subscriber") or {}
        ucode = str(subscriber.get("ucode") or "").strip()
        if ucode:
            plan_by_userproduct[(pid, ucode)].append(plan)

    sale_by_tx: dict[str, dict] = {}
    sales_by_userproduct: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in approved_raw:
        sale = parse_sale_record(row, plan_by_transaction, plan_by_userproduct)
        if not sale:
            continue
        tx = sale["transaction_internal"]
        sale_by_tx[tx] = sale
        ucode = sale["buyer_ucode_internal"]
        if ucode and sale["modality"] == "Recorrência (*R)":
            sales_by_userproduct[(sale["product_id_internal"], ucode)].append(sale)
    if not sale_by_tx:
        raise RuntimeError("A consulta não retornou vendas aprovadas em BRL para os produtos mapeados; publicação interrompida.")

    sales = []
    for sale in sale_by_tx.values():
        sales.append({key: sale[key] for key in (
            "date", "dateTime", "program", "productId", "modality", "plan", "gross", "fee", "net",
            "paymentType", "installments", "recurrenceNumber",
        )})
    sales.sort(key=lambda row: (row["dateTime"], row["program"]), reverse=True)

    pending_sales, pending_meta = prepare_pending(token, pending_raw, list(sale_by_tx.values()))

    # Failed first-cycle attempts are drawn from the documented transaction
    # statuses and counted only when Hotmart identifies them as recurring cycle 1.
    failed_rows_by_tx = {}
    for row in cancelled_raw + no_funds_raw:
        tx = transaction_of(row)
        pid = product_id_of(row)
        purchase = purchase_of(row)
        if not tx or pid not in REC_PRODUCT_IDS:
            continue
        if not bool(purchase.get("is_subscription")) or int_or_none(purchase.get("recurrency_number")) != 1:
            continue
        gross, currency = price_of(row)
        if currency == "BRL":
            failed_rows_by_tx[tx] = (pid, gross)

    # Confirmed overdue amounts come only from Hotmart transactions explicitly
    # marked OVERDUE, not from a guessed subscription principal.
    overdue_by_tx = {}
    for row in overdue_raw:
        tx = transaction_of(row)
        pid = product_id_of(row)
        purchase = purchase_of(row)
        if not tx or pid not in REC_PRODUCT_IDS:
            continue
        if str(purchase.get("status") or "").upper() != "OVERDUE":
            continue
        gross, currency = price_of(row)
        if currency == "BRL":
            overdue_by_tx[tx] = (pid, gross)

    as_of = datetime.now(TZ)
    as_of_date = as_of.strftime("%Y-%m-%d")
    as_of_text = as_of.strftime("%d/%m/%Y %H:%M BRT")
    program_counts = {p["name"]: {"active": 0, "late": 0, "initialFailures": 0} for p in PROGRAMS}
    seen_subscription_keys = set()
    active_rows = []
    late_total = 0
    active_total = 0
    for sub in subscriptions:
        pid = product_id_of(sub)
        if pid not in REC_PRODUCT_IDS:
            continue
        program = PROGRAM_BY_ID[pid][0]
        status = str(sub.get("status") or "").upper()
        sid = str(sub.get("subscription_id") or "").strip()
        sub_tx = str(sub.get("transaction") or "").strip()
        subscriber = sub.get("subscriber") or {}
        ucode = str(subscriber.get("ucode") or "").strip()
        dedup_key = (pid, sid or sub_tx or ucode or str(sub.get("subscriber_code") or ""))
        if dedup_key in seen_subscription_keys:
            continue
        seen_subscription_keys.add(dedup_key)
        if status == "ACTIVE":
            active_total += 1
            program_counts[program]["active"] += 1
        if status in ("DELAYED", "OVERDUE"):
            late_total += 1
            program_counts[program]["late"] += 1

    for pid, _gross in failed_rows_by_tx.values():
        program_counts[PROGRAM_BY_ID[pid][0]]["initialFailures"] += 1
    late_amount = money(sum((Decimal(str(value)) for _pid, value in overdue_by_tx.values()), Decimal("0.00")))

    # Determine ambiguity before matching subscriptions to a buyer/product sale history.
    active_counts_by_userproduct = defaultdict(int)
    for sub in subscriptions:
        if str(sub.get("status") or "").upper() != "ACTIVE":
            continue
        pid = product_id_of(sub)
        subscriber = sub.get("subscriber") or {}
        ucode = str(subscriber.get("ucode") or "").strip()
        if pid in REC_PRODUCT_IDS and ucode:
            active_counts_by_userproduct[(pid, ucode)] += 1

    forecast = []
    subscription_ref = 0
    for sub in subscriptions:
        pid = product_id_of(sub)
        if pid not in REC_PRODUCT_IDS or str(sub.get("status") or "").upper() != "ACTIVE":
            continue
        next_charge = sub.get("date_next_charge")
        next_dt_utc = None
        seconds = as_timestamp_seconds(next_charge)
        if seconds is not None:
            next_dt_utc = datetime.fromtimestamp(seconds, tz=timezone.utc)
        if not next_dt_utc:
            continue
        next_local = next_dt_utc.astimezone(TZ)
        if next_local.date() < as_of.date():
            continue

        plan = plan_info(sub)
        period = int(plan["period"])
        max_cycles = int(plan["max_cycles"])
        sub_tx = str(sub.get("transaction") or "").strip()
        subscriber = sub.get("subscriber") or {}
        ucode = str(subscriber.get("ucode") or "").strip()
        sale_match = sale_by_tx.get(sub_tx) if sub_tx else None
        candidate_sales = sales_by_userproduct.get((pid, ucode), []) if ucode else []
        if not sale_match and candidate_sales and active_counts_by_userproduct[(pid, ucode)] == 1:
            sale_match = max(candidate_sales, key=lambda sale: sale.get("recurrenceNumber") or 0)
        current_cycle = None
        cycle_source = "estimativa: primeira cobrança aprovada"
        if candidate_sales and (active_counts_by_userproduct[(pid, ucode)] == 1 or sale_match):
            cycle_values = [sale.get("recurrenceNumber") for sale in candidate_sales if sale.get("recurrenceNumber")]
            if cycle_values:
                current_cycle = max(int(value) for value in cycle_values)
                cycle_source = "Hotmart: número da recorrência nas vendas"
        if current_cycle is None and sale_match and sale_match.get("recurrenceNumber"):
            current_cycle = int(sale_match["recurrenceNumber"])
            cycle_source = "Hotmart: venda aprovada correspondente"
        if current_cycle is None:
            current_cycle = 1
        current_cycle = min(max(1, int(current_cycle)), max_cycles)

        price_obj = sub.get("price") or {}
        gross = sale_match["gross"] if sale_match else money(price_obj.get("value"))
        fallback = FALLBACK_CYCLE_PRICE.get(pid)
        if not sale_match and period == 90 and fallback:
            gross = money(fallback)
        if gross <= 0:
            continue
        installments = sale_match["installments"] if sale_match else (6 if period == 180 else (3 if period == 90 else 1))
        plan_name = normalize_plan(plan.get("name"), pid, period, max_cycles, sale_match.get("offerCode_internal") if sale_match else None)
        subscription_ref += 1
        ref = f"ASS-{subscription_ref:03d}"
        forecast_status = "Conciliada" if sale_match else "Provisória"
        active_rows.append({
            "product_id": pid,
            "program": PROGRAM_BY_ID[pid][0],
            "status": "ACTIVE",
            "plan": plan_name,
            "subscription_ref": ref,
            "current_cycle": current_cycle,
            "max_cycles": max_cycles,
            "date_next_charge": next_local.strftime("%Y-%m-%d %H:%M"),
            "gross_per_cycle": money(gross),
            "cycle_source": cycle_source,
            "has_approved_sale_match": bool(sale_match),
            "forecast_status": forecast_status,
        })
        remaining = max(0, max_cycles - current_cycle)
        for offset in range(remaining):
            scheduled = next_dt_utc + timedelta(days=period * offset)
            local_due = scheduled.astimezone(TZ)
            forecast.append({
                "date": local_due.strftime("%Y-%m-%d"),
                "dateTime": local_due.strftime("%Y-%m-%d %H:%M"),
                "program": PROGRAM_BY_ID[pid][0],
                "productId": pid,
                "modality": "Recorrência (*R)",
                "plan": plan_name,
                "subscriptionRef": ref,
                "cycle": current_cycle + offset + 1,
                "maxCycles": max_cycles,
                "gross": money(gross),
                "installments": int(installments),
                "periodDays": period,
                "cycleSource": cycle_source,
                "status": forecast_status,
            })
    forecast.sort(key=lambda row: (row["dateTime"], row["program"], row["subscriptionRef"]))
    active_rows.sort(key=lambda row: (row["program"], row["plan"], row["date_next_charge"]))

    confirmed = [row for row in forecast if row["status"] == "Conciliada"]
    provisional = [row for row in forecast if row["status"] == "Provisória"]
    sales_gross = money(sum((dec(row["gross"]) for row in sales), Decimal("0.00")))
    sales_fees = money(sum((dec(row["fee"]) for row in sales), Decimal("0.00")))
    sales_net = money(sum((dec(row["net"]) for row in sales), Decimal("0.00")))
    future_confirmed = money(sum((dec(row["gross"]) for row in confirmed), Decimal("0.00")))
    future_provisional = money(sum((dec(row["gross"]) for row in provisional), Decimal("0.00")))
    pending_unique = money(sum((dec(row["gross"]) for row in pending_sales), Decimal("0.00")))
    future_only = money(Decimal(str(future_confirmed)) + Decimal(str(pending_unique)))
    vgv_estimate = money(Decimal(str(sales_gross)) + Decimal(str(future_only)))
    vgv_potential = money(Decimal(str(vgv_estimate)) + Decimal(str(future_provisional)))
    next30_end = (as_of + timedelta(days=30)).strftime("%Y-%m-%d")
    next30 = money(sum((dec(row["gross"]) for row in confirmed if as_of_date <= row["date"] <= next30_end), Decimal("0.00")))
    start_date = min((row["date"] for row in sales), default=as_of_date)
    end_date = max((row["date"] for row in sales), default=as_of_date)

    summary = {
        "saleCount": len(sales),
        "gross": sales_gross,
        "fees": sales_fees,
        "net": sales_net,
        "fullCount": sum(row["modality"] == "Produto cheio" for row in sales),
        "recurringCount": sum(row["modality"] == "Recorrência (*R)" for row in sales),
        "fullGross": money(sum((dec(row["gross"]) for row in sales if row["modality"] == "Produto cheio"), Decimal("0.00"))),
        "recurringGross": money(sum((dec(row["gross"]) for row in sales if row["modality"] == "Recorrência (*R)"), Decimal("0.00"))),
        "activeSubscriptions": len(active_rows),
        "confirmedSubscriptions": sum(row["has_approved_sale_match"] for row in active_rows),
        "provisionalSubscriptions": sum(not row["has_approved_sale_match"] for row in active_rows),
        "futureCycles": len(confirmed),
        "futureCyclesProvisional": len(provisional),
        "futureGross": future_confirmed,
        "futureGrossProvisional": future_provisional,
        "futureTotalGross": money(Decimal(str(future_confirmed)) + Decimal(str(future_provisional))),
        "futureNet30": next30,
        "futureProvisional30": money(sum((dec(row["gross"]) for row in provisional if as_of_date <= row["date"] <= next30_end), Decimal("0.00"))),
        "unmatchedActive": sum(not row["has_approved_sale_match"] for row in active_rows),
    }
    vgv = {
        "realizedApproved": sales_gross,
        "pendingRawCount": pending_meta["raw_count"],
        "pendingRawGross": pending_meta["raw_gross"],
        "pendingUniqueCount": len(pending_sales),
        "pendingUniqueGross": pending_unique,
        "pendingDuplicatesExcluded": pending_meta["excluded"],
        "pendingByProgram": pending_meta["by_program"],
        "futureRecurringConfirmed": future_confirmed,
        "futureRecurringProvisional": future_provisional,
        "vgvEstimate": vgv_estimate,
        "futureOnly": future_only,
        "vgvPotential": vgv_potential,
    }
    delinquency = {
        "asOf": as_of_text,
        "activeTotal": active_total,
        "lateTotal": late_total,
        "lateAmount": late_amount,
        "firstChargeAttempts": len(failed_rows_by_tx),
        "programs": [
            {"program": p["name"], **program_counts[p["name"]]}
            for p in PROGRAMS
        ],
    }
    return {
        "asOf": as_of_text,
        "asOfDate": as_of_date,
        "asOfSubscriptions": as_of_text,
        "startDate": start_date,
        "endDate": end_date,
        "summary": summary,
        "programs": [p["name"] for p in PROGRAMS],
        "sales": sales,
        "forecast": forecast,
        "activeSubscriptions": active_rows,
        "pendingSales": pending_sales,
        "vgv": vgv,
        "delinquency": delinquency,
    }


def write_csvs(data: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "vendas_aprovadas_pos_graduacoes_hotmart.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";", lineterminator="\n")
        writer.writerow(["Data aprovação (BRT)", "Pós-graduação", "Modalidade", "Plano", "Faturamento bruto (BRL)", "Taxa Hotmart (BRL)", "Líquido estimado após taxa (BRL)", "Meio de pagamento", "Parcelas no checkout", "Nº da recorrência"])
        for row in sorted(data["sales"], key=lambda item: (item["dateTime"], item["program"])):
            writer.writerow([row["dateTime"], row["program"], row["modality"], row["plan"], money_br(row["gross"]), money_br(row["fee"]), money_br(row["net"]), row["paymentType"], row["installments"], row["recurrenceNumber"] or ""])
    with (OUT_DIR / "previsao_recorrencia_pos_graduacoes_hotmart.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";", lineterminator="\n")
        writer.writerow(["Data prevista (BRT)", "Conciliação", "Pós-graduação", "Plano", "Referência anonimizada", "Ciclo futuro", "Ciclos totais", "Valor bruto do ciclo (BRL)", "Parcelas do ciclo", "Periodicidade (dias)"])
        for row in data["forecast"]:
            writer.writerow([row["dateTime"], row["status"], row["program"], row["plan"], row["subscriptionRef"], row["cycle"], row["maxCycles"], money_br(row["gross"]), row["installments"], row["periodDays"]])

    summary_rows = []
    for name in data["programs"]:
        sales = [row for row in data["sales"] if row["program"] == name]
        pending = [row for row in data["pendingSales"] if row["program"] == name]
        pending_stats = data["vgv"]["pendingByProgram"][name]
        confirmed = [row for row in data["forecast"] if row["program"] == name and row["status"] == "Conciliada"]
        provisional = [row for row in data["forecast"] if row["program"] == name and row["status"] == "Provisória"]
        realized = money(sum((dec(row["gross"]) for row in sales), Decimal("0.00")))
        pending_amount = money(sum((dec(row["gross"]) for row in pending), Decimal("0.00")))
        future = money(sum((dec(row["gross"]) for row in confirmed), Decimal("0.00")))
        future_only = money(Decimal(str(pending_amount)) + Decimal(str(future)))
        vgv = money(Decimal(str(realized)) + Decimal(str(future_only)))
        provisional_amount = money(sum((dec(row["gross"]) for row in provisional), Decimal("0.00")))
        potential = money(Decimal(str(vgv)) + Decimal(str(provisional_amount)))
        summary_rows.append(["Pós-graduação", name, money_br(realized), money_br(pending_stats["raw_gross"]), money_br(pending_amount), str(pending_stats["excluded"]), money_br(future), money_br(future_only), money_br(vgv), money_br(provisional_amount), money_br(potential)])
    v = data["vgv"]
    general = ["Geral", "Todas as pós-graduações", money_br(v["realizedApproved"]), money_br(v["pendingRawGross"]), money_br(v["pendingUniqueGross"]), str(v["pendingDuplicatesExcluded"]), money_br(v["futureRecurringConfirmed"]), money_br(v["futureOnly"]), money_br(v["vgvEstimate"]), money_br(v["futureRecurringProvisional"]), money_br(v["vgvPotential"])]
    with (OUT_DIR / "resumo_vgv_pos_graduacoes_hotmart.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";", lineterminator="\n")
        writer.writerow(["Escopo", "Pós-graduação", "Realizado aprovado (BRL)", "Aguardando pagamento bruto original (BRL)", "Aguardando pagamento único por CPF/CNPJ (BRL)", "Tentativas duplicadas excluídas", "Recorrência futura conciliada (BRL)", "Futuro (VGV - realizado) (BRL)", "VGV estimado (BRL)", "Recorrência provisória (BRL)", "VGV potencial (BRL)"])
        writer.writerow(general)
        writer.writerows(summary_rows)


def render_site(data: dict) -> None:
    if not TEMPLATE.exists():
        raise RuntimeError("Template do dashboard ausente.")
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count("__DATA__") != 1:
        raise RuntimeError("Placeholder de dados não encontrado de forma única no template.")
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("__DATA__", serialized)
    # Defensive privacy guard: identifiers and buyer records are never allowed in public data.
    payload = json.dumps(data, ensure_ascii=False).lower()
    forbidden_fields = ("buyer_email", "subscriber_email", "buyer_name", "subscriber_name", "subscriber_code", "transaction_internal", "buyer_ucode_internal", "cpf", "cnpj", "documents")
    if any(field in payload for field in forbidden_fields):
        raise RuntimeError("Validação de privacidade falhou; o arquivo público não foi gerado.")
    if re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", html):
        raise RuntimeError("Validação de privacidade detectou e-mail no HTML; publicação interrompida.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    token = get_access_token()
    data = build_dashboard_data(token)
    if not data["sales"] or not data["programs"] or len(data["programs"]) != 8:
        raise RuntimeError("Validação de integridade falhou; a publicação anterior será mantida.")
    write_csvs(data)
    render_site(data)
    print(json.dumps({
        "ok": True,
        "updated_at_brt": data["asOf"],
        "approved_sales": data["summary"]["saleCount"],
        "approved_gross_brl": data["summary"]["gross"],
        "active_subscriptions": data["delinquency"]["activeTotal"],
        "late_subscriptions": data["delinquency"]["lateTotal"],
        "pending_unique": data["vgv"]["pendingUniqueCount"],
        "future_cycles_confirmed": data["summary"]["futureCycles"],
        "vgv_estimate_brl": data["vgv"]["vgvEstimate"],
        "public_files": ["index.html", "vendas_aprovadas_pos_graduacoes_hotmart.csv", "previsao_recorrencia_pos_graduacoes_hotmart.csv", "resumo_vgv_pos_graduacoes_hotmart.csv"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # No exception details are included because API exceptions might contain private payloads.
        message = str(exc)
        if any(key in message.lower() for key in ("secret", "token", "email", "cpf", "cnpj")):
            message = "Falha segura na rotina Hotmart; detalhes sensíveis foram suprimidos."
        print(f"ERRO: {message}", file=sys.stderr)
        sys.exit(1)
