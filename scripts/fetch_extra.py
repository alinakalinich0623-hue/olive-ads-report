#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Дописывает в data.json ступени воронки, которых нет ни в Meta, ни в Битриксе:
  visits / users — визиты и посетители сайта
  otp            — подтверждённые коды WhatsApp (если появится эндпоинт)

Основной источник — Supabase: там уже лежит таблица metrika_daily, которую
наполняет отдельная синхронизация с Яндекс.Метрикой. Поэтому OAuth-токен
Метрики не нужен: читаем готовые строки из базы.

Запускается ПОСЛЕ fetch_meta.py и fetch_bitrix.py.
Каждый источник независим: нет секрета — ступень пропускается, сборка не падает.

Переменные окружения:
  SUPABASE_URL   — https://vymjccflwkbvlqefsule.supabase.co
  SUPABASE_KEY   — service_role ключ. На metrika_daily включён RLS без политик,
                   поэтому anon/publishable ключ прочитать её не сможет.
  METRIKA_COUNTER — номер счётчика, по умолчанию 104935856
  TRAFFIC_ONLY_AD — 1 (по умолчанию): в воронку идут только рекламные визиты
                    (metrika_sources_daily, traffic_source=ad). 0 — весь трафик.

  METRIKA_TOKEN  — запасной путь: если Supabase не задан, ходим напрямую в API Метрики
  OLIVE_API_URL / OLIVE_API_KEY — эндпоинт со статистикой OTP по дням, формат:
                   {"daily": [{"day": "2026-07-29", "verified": 26}]}
"""

import json
import os
import sys

import requests

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")

SB_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SB_KEY = os.environ.get("SUPABASE_KEY", "").strip()
MET_COUNTER = os.environ.get("METRIKA_COUNTER", "104935856").strip()
MET_TOKEN = os.environ.get("METRIKA_TOKEN", "").strip()
OLIVE_URL = os.environ.get("OLIVE_API_URL", "").strip()
OLIVE_KEY = os.environ.get("OLIVE_API_KEY", "").strip()
ONLY_AD = os.environ.get("TRAFFIC_ONLY_AD", "1").strip() != "0"


def from_supabase_ad(d1, d2):
    """Только рекламные визиты — metrika_sources_daily, traffic_source='ad'.
    Иначе в воронку попадёт органика, прямые заходы и соцсети."""
    r = requests.get(
        SB_URL + "/rest/v1/metrika_sources_daily",
        params={
            "select": "report_date,visits,users",
            "counter_id": "eq." + MET_COUNTER,
            "traffic_source": "eq.ad",
            "report_date": "gte." + d1,
            "and": "(report_date.lte." + d2 + ")",
            "order": "report_date.asc",
            "limit": "10000",
        },
        headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError("Supabase HTTP %s: %s" % (r.status_code, r.text[:300]))
    out = {}
    for row in r.json():
        day = str(row["report_date"])[:10]
        v, u = int(row.get("visits") or 0), int(row.get("users") or 0)
        pv, pu = out.get(day, (0, 0))
        out[day] = (pv + v, pu + u)
    return out


def from_supabase(d1, d2):
    """Визиты и посетители из таблицы metrika_daily. {'2026-08-01': (visits, users)}."""
    r = requests.get(
        SB_URL + "/rest/v1/metrika_daily",
        params={
            "select": "report_date,visits,users",
            "counter_id": "eq." + MET_COUNTER,
            "report_date": "gte." + d1,
            "and": "(report_date.lte." + d2 + ")",
            "order": "report_date.asc",
            "limit": "10000",
        },
        headers={"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError("Supabase HTTP %s: %s" % (r.status_code, r.text[:300]))
    out = {}
    for row in r.json():
        out[str(row["report_date"])[:10]] = (int(row.get("visits") or 0), int(row.get("users") or 0))
    return out


def from_metrika_api(d1, d2):
    """Запасной путь — напрямую в API Яндекс.Метрики, нужен OAuth-токен."""
    r = requests.get(
        "https://api-metrika.yandex.net/stat/v1/data",
        params={"ids": MET_COUNTER, "metrics": "ym:s:visits,ym:s:users",
                "dimensions": "ym:s:date", "date1": d1, "date2": d2,
                "group": "day", "limit": 100000, "accuracy": "full"},
        headers={"Authorization": "OAuth " + MET_TOKEN},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError("Метрика HTTP %s: %s" % (r.status_code, r.text[:300]))
    out = {}
    for row in (r.json().get("data") or []):
        m = row.get("metrics") or [0, 0]
        out[row["dimensions"][0]["name"]] = (int(m[0] or 0), int(m[1] or 0))
    return out


def otp_daily(d1, d2):
    """Подтверждённые OTP по дням."""
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
    status = {}

    # --- визиты и посетители ---
    src, fn = None, None
    if SB_URL and SB_KEY:
        if ONLY_AD:
            src, fn = "supabase.metrika_sources_daily (traffic_source=ad)", from_supabase_ad
        else:
            src, fn = "supabase.metrika_daily (весь трафик)", from_supabase
    elif MET_TOKEN:
        src, fn = "api-metrika.yandex.net", from_metrika_api

    if fn:
        try:
            m = fn(d1, d2)
            for row in daily:
                v, u = m.get(row["date"], (None, None))
                row["visits"], row["users"] = v, u
            got = sum(1 for r in daily if r.get("visits"))
            status["traffic"] = {"ok": True, "source": src, "counter": MET_COUNTER, "days": got}
            print("Трафик: источник %s, дней с данными %d" % (src, got))
        except Exception as e:
            status["traffic"] = {"ok": False, "source": src, "note": str(e)}
            print("Трафик недоступен (%s): %s" % (src, e))
    else:
        status["traffic"] = {"ok": False, "note": "не задан ни SUPABASE_URL/SUPABASE_KEY, ни METRIKA_TOKEN"}
        print("Источник трафика не задан — ступени «визиты» и «посетители» пропущены")

    # --- OTP ---
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

    payload.setdefault("meta", {})["funnel_sources"] = status

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print("Готово: data.json дополнен ступенями воронки")


if __name__ == "__main__":
    main()
