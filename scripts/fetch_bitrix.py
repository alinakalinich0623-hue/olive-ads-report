#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Догружает в data.json продажи из Битрикс24 и считает цену продажи (CAC) и цену лида.

Запускается ПОСЛЕ scripts/fetch_meta.py: тот собирает рекламу, этот дописывает продажи.
Если Битрикс недоступен — скрипт не роняет сборку, а помечает продажи как «нет данных»,
и дашборд продолжает показывать рекламную часть как обычно.

Переменные окружения:
  BITRIX_WEBHOOK  — обязательный. Входящий вебхук, напр. https://segodnya.bitrix24.kz/rest/1/xxxx
  PAY_DATE_FIELD  — поле «Дата оплаты», по умолчанию UF_CRM_1771991929628
  LEAD_CATEGORY   — воронка входящих заявок, по умолчанию 9 («Первичная продажа»)
  NEW_ONLY        — "1" (по умолчанию) считать только первую покупку клиента, "0" — все оплаты
  FX_FALLBACK     — резервный курс тенге за доллар, если внешний источник недоступен

ПОЧЕМУ ПО «ДАТЕ ОПЛАТЫ», А НЕ ПО СТАДИИ.
Стадия сделки в этом портале — это состояние клиента, а не событие. Сделка не остаётся там,
где оплатилась: «Первичная продажа/ЛИД С САЙТА ОПЛАЧЕН» -> «Повтор/Актив» -> «Актив Блюда»
-> «Продление предложено» -> ... Если считать «сделки, лежащие сейчас в стадии X», цифра за
прошлый вторник будет сама уменьшаться каждую неделю. Поле «Дата оплаты» проставляется
один раз и больше не меняется, поэтому день продажи берётся именно из него.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").strip().rstrip("/")
PAY_FIELD = os.environ.get("PAY_DATE_FIELD", "UF_CRM_1771991929628").strip()
LEAD_CATEGORY = os.environ.get("LEAD_CATEGORY", "9").strip()
NEW_ONLY = os.environ.get("NEW_ONLY", "1").strip() != "0"
FX_FALLBACK = float(os.environ.get("FX_FALLBACK", "474") or 474)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")


def day_of(v):
    """Из даты Битрикса берём только день: 2026-08-03."""
    if not v:
        return None
    s = str(v)
    return s[:10] if len(s) >= 10 else None


def bitrix_list(method, params, tries=4):
    """Постраничный вызов Битрикс REST. Возвращает список элементов."""
    out = []
    start = 0
    for _ in range(400):
        body = dict(params)
        body["start"] = start
        data = None
        for attempt in range(tries):
            try:
                r = requests.post(WEBHOOK + "/" + method + ".json", data=body, timeout=120)
                if r.status_code == 200:
                    data = r.json()
                    break
                if r.status_code in (429, 500, 502, 503):
                    time.sleep(5 * (attempt + 1))
                    continue
                raise SystemExit("ОШИБКА Битрикс " + method + ": HTTP " + str(r.status_code) + ": " + r.text[:300])
            except requests.RequestException as e:
                if attempt == tries - 1:
                    raise SystemExit("ОШИБКА Битрикс " + method + ": " + str(e))
                time.sleep(5 * (attempt + 1))
        if data is None:
            raise SystemExit("ОШИБКА Битрикс " + method + ": пустой ответ")
        if "error" in data:
            raise SystemExit("ОШИБКА Битрикс " + method + ": " + str(data.get("error_description") or data["error"]))

        res = data.get("result")
        items = res.get("items") if isinstance(res, dict) else res
        out.extend(items or [])

        nxt = data.get("next")
        if nxt is None:
            break
        start = nxt
    return out


def fx_usd_kzt():
    """Курс доллара к тенге. Бесплатный источник без ключа."""
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=45)
        if r.status_code == 200:
            j = r.json()
            v = (j.get("rates") or {}).get("KZT")
            if v:
                return float(v), "open.er-api.com", j.get("time_last_update_utc", "")
    except requests.RequestException:
        pass
    return FX_FALLBACK, "резервный курс (внешний источник недоступен)", ""


def collect(since):
    """Считает по дням: продажи, выручку, заявки."""
    # 1) Все сделки, у которых заполнена «Дата оплаты».
    #    Всю историю берём намеренно — по ней определяется, новый клиент или повторный.
    paid = bitrix_list("crm.deal.list", {
        "filter[>=" + PAY_FIELD + "]": "2000-01-01",
        "select[0]": "ID",
        "select[1]": "CONTACT_ID",
        "select[2]": "OPPORTUNITY",
        "select[3]": PAY_FIELD,
        "order[DATE_CREATE]": "ASC",
    })

    # 2) Заявки: сделки, СОЗДАННЫЕ в воронке лидов. Создание — событие, оно не «уезжает».
    leads = bitrix_list("crm.deal.list", {
        "filter[CATEGORY_ID]": LEAD_CATEGORY,
        "filter[>=DATE_CREATE]": since,
        "select[0]": "ID",
        "select[1]": "DATE_CREATE",
        "order[DATE_CREATE]": "ASC",
    })

    sales = []
    for d in paid:
        day = day_of(d.get(PAY_FIELD))
        if not day:
            continue
        try:
            amount = float(d.get("OPPORTUNITY") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        sales.append({"day": day, "contact": d.get("CONTACT_ID"), "amount": amount})

    # первая оплата каждого контакта — по ней отличаем нового клиента от повторного
    first_by_contact = {}
    for s in sales:
        c = s["contact"]
        if not c:
            continue
        if c not in first_by_contact or s["day"] < first_by_contact[c]:
            first_by_contact[c] = s["day"]

    by = {}

    def row(day):
        return by.setdefault(day, {"sales": 0, "sales_all": 0, "revenue": 0.0, "bleads": 0})

    for s in sales:
        if s["day"] < since:
            continue
        r = row(s["day"])
        r["sales_all"] += 1
        r["revenue"] += s["amount"]
        if not s["contact"] or first_by_contact.get(s["contact"]) == s["day"]:
            r["sales"] += 1

    for d in leads:
        day = day_of(d.get("DATE_CREATE"))
        if day:
            row(day)["bleads"] += 1

    return by, len(sales)


def main():
    if not os.path.exists(OUT):
        sys.exit("ОШИБКА: data.json не найден — сначала должен отработать fetch_meta.py")

    with open(OUT, encoding="utf-8") as f:
        payload = json.load(f)

    daily = payload.get("daily") or []
    if not daily:
        sys.exit("ОШИБКА: в data.json нет раздела daily")

    since = daily[0]["date"]

    if not WEBHOOK:
        print("BITRIX_WEBHOOK не задан — продажи пропущены, реклама остаётся как есть")
        for d in daily:
            d["sales"] = d["sales_all"] = d["sales_revenue"] = d["bleads"] = None
        payload["meta"]["bitrix"] = {
            "ok": False,
            "note": "BITRIX_WEBHOOK не задан (GitHub -> Settings -> Secrets -> Actions)",
        }
    else:
        try:
            by, total = collect(since)
            rate, src, fx_date = fx_usd_kzt()
            for d in daily:
                r = by.get(d["date"])
                d["sales"] = r["sales"] if r else 0
                d["sales_all"] = r["sales_all"] if r else 0
                d["sales_revenue"] = round(r["revenue"]) if r else 0
                d["bleads"] = r["bleads"] if r else 0
            payload["meta"]["bitrix"] = {
                "ok": True,
                "fx_rate": round(rate, 4),
                "fx_src": src,
                "fx_date": fx_date,
                "new_only": NEW_ONLY,
                "pay_field": PAY_FIELD,
                "lead_category": LEAD_CATEGORY,
                "deals_scanned": total,
                "note": (
                    "Продажа = день из поля «Дата оплаты», только первая покупка контакта."
                    if NEW_ONLY else
                    "Продажа = день из поля «Дата оплаты», включая повторные покупки."
                ),
            }
            got = sum(1 for d in daily if d.get("sales"))
            print("Битрикс: оплат всего " + str(total) + ", дней с продажами " + str(got) + ", курс " + str(round(rate, 2)))
        except SystemExit as e:
            print("Битрикс недоступен: " + str(e))
            for d in daily:
                d["sales"] = d["sales_all"] = d["sales_revenue"] = d["bleads"] = None
            payload["meta"]["bitrix"] = {"ok": False, "note": str(e)}

    now_almaty = datetime.now(timezone.utc) + timedelta(hours=5)
    payload["meta"]["bitrix_updated_at"] = now_almaty.strftime("%Y-%m-%d %H:%M") + " (Алматы)"

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("Готово: data.json дополнен продажами")


if __name__ == "__main__":
    main()
