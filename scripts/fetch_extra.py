#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Дописывает в data.json недостающие ступени воронки:
  visits / users — из Яндекс.Метрики
  otp            — из бэкенда olive.kz (подтверждённые коды WhatsApp)

Запускается ПОСЛЕ fetch_meta.py и fetch_bitrix.py.
Каждый источник независим: нет секрета — ступень просто пропускается,
сборка не падает, дашборд продолжает работать на остальных данных.

Переменные окружения (все необязательные):
  METRIKA_TOKEN    — OAuth-токен Яндекс.Метрики с правом на чтение статистики
  METRIKA_COUNTER  — номер счётчика, по умолчанию 104935856 (olive.kz)
  OLIVE_API_URL    — полный адрес эндпоинта со статистикой OTP по дням
  OLIVE_API_KEY    — ключ к нему, уходит заголовком Authorization: Bearer <ключ>

ОЖИДАЕМЫЙ ФОРМАТ ОТВЕТА OLIVE_API_URL:
  GET <OLIVE_API_URL>?from=2025-12-12&to=2026-08-04
  {"daily": [{"day": "2026-07-29", "total": 28, "delivered": 28, "verified": 26}]}
Берётся verified — подтверждённые коды, реально дошедшие до конца шага.
"""

import json
import os
import sys

import requests

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")

MET_TOKEN = os.environ.get("METRIKA_TOKEN", "").strip()
MET_COUNTER = os.environ.get("METRIKA_COUNTER", "104935856").strip()
OLIVE_URL = os.environ.get("OLIVE_API_URL", "").strip()
OLIVE_KEY = os.environ.get("OLIVE_API_KEY", "").strip()


def metrika_daily(d1, d2):
    """Визиты и посетители по дням: {'2026-08-01': (visits, users)}."""
    r = requests.get(
        "https://api-metrika.yandex.net/stat/v1/data",
        params={
            "ids": MET_COUNTER,
            "metrics": "ym:s:visits,ym:s:users",
            "dimensions": "ym:s:date",
            "date1": d1,
            "date2": d2,
            "group": "day",
            "limit": 100000,
            "accuracy": "full",
        },
        headers={"Authorization": "OAuth " + MET_TOKEN},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError("Метрика HTTP %s: %s" % (r.status_code, r.text[:300]))
    out = {}
    for row in (r.json().get("data") or []):
        day = row["dimensions"][0]["name"]
        m = row.get("metrics") or [0, 0]
        out[day] = (int(m[0] or 0), int(m[1] or 0))
    return out


def otp_daily(d1, d2):
    """Подтверждённые OTP по дням: {'2026-08-01': verified}."""
    headers = {"Authorization": "Bearer " + OLIVE_KEY} if OLIVE_KEY else {}
    r = requests.get(OLIVE_URL, params={"from": d1, "to": d2}, headers=headers, timeout=120)
    if r.status_code != 200:
        raise RuntimeError("Бэкенд olive.kz HTTP %s: %s" % (r.status_code, r.text[:300]))
    j = r.json()
    rows = j.get("daily") if isinstance(j, dict) else j
    out = {}
    for row in (rows or []):
        day = str(row.get("day") or row.get("date") or "")[:10]
        if not day:
            continue
        v = row.get("verified")
        if v is None:
            v = row.get("delivered", row.get("total", 0))
        out[day] = int(v or 0)
    return out


def main():
    if not os.path.exists(OUT):
        sys.exit("ОШИБКА: data.json не найден — сначала должен отработать fetch_meta.py")

    with open(OUT, encoding="utf-8") as f:
        payload = json.load(f)

    daily = payload.get("daily") or []
    if not daily:
        sys.exit("ОШИБКА: в data.json нет раздела daily")

    d1, d2 = daily[0]["date"], daily[-1]["date"]
    meta = payload.setdefault("meta", {})
    status = {}

    if MET_TOKEN:
        try:
            m = metrika_daily(d1, d2)
            for row in daily:
                v, u = m.get(row["date"], (None, None))
                row["visits"], row["users"] = v, u
            got = sum(1 for r in daily if r.get("visits"))
            status["metrika"] = {"ok": True, "counter": MET_COUNTER, "days": got}
            print("Метрика: счётчик %s, дней с данными %d" % (MET_COUNTER, got))
        except Exception as e:
            status["metrika"] = {"ok": False, "note": str(e)}
            print("Метрика недоступна: %s" % e)
    else:
        status["metrika"] = {"ok": False, "note": "METRIKA_TOKEN не задан"}
        print("METRIKA_TOKEN не задан — ступени «визиты» и «посетители» пропущены")

    if OLIVE_URL:
        try:
            o = otp_daily(d1, d2)
            for row in daily:
                row["otp"] = o.get(row["date"])
            got = sum(1 for r in daily if r.get("otp"))
            status["otp"] = {"ok": True, "days": got}
            print("OTP: дней с данными %d" % got)
        except Exception as e:
            status["otp"] = {"ok": False, "note": str(e)}
            print("Бэкенд olive.kz недоступен: %s" % e)
    else:
        status["otp"] = {"ok": False, "note": "OLIVE_API_URL не задан"}
        print("OLIVE_API_URL не задан — ступень OTP пропущена")

    meta["funnel_sources"] = status

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("Готово: data.json дополнен ступенями воронки")


if __name__ == "__main__":
    main()
