#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка статистики рекламного кабинета Meta (Facebook/Instagram) в data.json.

Запускается автоматически GitHub Actions по расписанию.
Токен НИКОГДА не хранится в коде — только в GitHub Secrets.

Переменные окружения:
  META_ACCESS_TOKEN   — обязательный. System User Token из Meta Business Manager.
  META_AD_ACCOUNT_ID  — например act_1117251293638187 (по умолчанию — он же).
  META_API_VERSION    — версия Graph API, по умолчанию v21.0
  START_DATE          — с какой даты собирать, по умолчанию 2025-12-12
  PIXEL_NOTE          — сноска про пиксель (по умолчанию — текущая)
"""

import json
import os
import sys
import time
from datetime import date, datetime, timezone, timedelta

import requests

TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
ACCOUNT = os.environ.get("META_AD_ACCOUNT_ID", "act_1117251293638187").strip()
VERSION = os.environ.get("META_API_VERSION", "v21.0").strip()
START_DATE = os.environ.get("START_DATE", "2025-12-12").strip()
PIXEL_NOTE = os.environ.get(
    "PIXEL_NOTE", "Покупки и ROAS отслеживаются Meta Pixel с 23.06.2026"
).strip()

BASE = f"https://graph.facebook.com/{VERSION}"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")

if not TOKEN:
    sys.exit("ОШИБКА: не задан META_ACCESS_TOKEN (GitHub → Settings → Secrets → Actions)")
if not ACCOUNT.startswith("act_"):
    ACCOUNT = "act_" + ACCOUNT

# приоритеты типов действий — берём первый найденный, чтобы не задваивать
PURCHASE_KEYS = ["omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase"]
LEAD_KEYS = ["lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead"]
MSG_KEYS = [
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.total_messaging_connection",
    "onsite_conversion.messaging_first_reply",
]


def api_get(path, params, tries=4):
    """GET к Graph API с постраничной выгрузкой и ретраями."""
    url = f"{BASE}/{path}"
    p = dict(params)
    p["access_token"] = TOKEN
    p.setdefault("limit", 500)
    rows = []
    while url:
        last_err = None
        for attempt in range(tries):
            try:
                r = requests.get(url, params=p if "access_token" not in url else None, timeout=120)
                if r.status_code == 200:
                    break
                # лимиты API / временные ошибки — ждём и пробуем снова
                if r.status_code in (400, 429, 500, 503):
                    body = r.text[:500]
                    last_err = f"HTTP {r.status_code}: {body}"
                    if "rate limit" in body.lower() or r.status_code in (429, 500, 503):
                        time.sleep(8 * (attempt + 1))
                        continue
                    raise SystemExit(f"ОШИБКА Meta API: {last_err}")
                r.raise_for_status()
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(5 * (attempt + 1))
        else:
            raise SystemExit(f"ОШИБКА Meta API после {tries} попыток: {last_err}")

        data = r.json()
        rows.extend(data.get("data", []))
        nxt = (data.get("paging") or {}).get("next")
        url, p = nxt, None
    return rows


def act(row, key, prio):
    """Вытащить значение действия из actions / action_values."""
    items = row.get(key) or []
    index = {i.get("action_type"): i.get("value") for i in items}
    for k in prio:
        if k in index:
            try:
                return float(index[k])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def num(row, key):
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


INSIGHT_FIELDS = (
    "spend,impressions,reach,inline_link_clicks,actions,action_values,"
    "campaign_name,objective,ad_name,ad_id"
)


def insights(level, extra_fields=""):
    fields = INSIGHT_FIELDS
    params = {
        "level": level,
        "fields": fields,
        "time_range": json.dumps({"since": START_DATE, "until": date.today().isoformat()}),
        "action_report_time": "conversion",
        "use_account_attribution_setting": "true",
    }
    if level == "account":
        params["time_increment"] = 1
    return api_get(f"{ACCOUNT}/insights", params)


def base_row(r):
    return {
        "cost": round(num(r, "spend"), 2),
        "impr": int(num(r, "impressions")),
        "reach": int(num(r, "reach")),
        "clicks": int(num(r, "inline_link_clicks")),
        "purch": int(act(r, "actions", PURCHASE_KEYS)),
        "value": round(act(r, "action_values", PURCHASE_KEYS), 2),
        "leads": int(act(r, "actions", LEAD_KEYS)),
        "msg": int(act(r, "actions", MSG_KEYS)),
    }


def merge(acc, row):
    for k, v in row.items():
        if isinstance(v, (int, float)):
            acc[k] = round(acc.get(k, 0) + v, 2)
    return acc


def creative_formats():
    """ad_id -> VIDEO / SHARE и ad_name -> VIDEO / SHARE (видео или баннер)."""
    by_id, by_name = {}, {}
    ads = None
    # Meta отдаёт HTTP 500 «reduce the amount of data», если просить много за раз,
    # поэтому спускаемся по размеру страницы, пока не получится
    for page in (50, 25, 10):
        try:
            ads = api_get(
                f"{ACCOUNT}/ads",
                {"fields": "id,name,creative{object_type,video_id}", "limit": page},
            )
            break
        except SystemExit as e:
            print(f"Креативы: страница по {page} не прошла ({str(e)[:120]}), пробую меньше")
    if ads is None:
        print("Предупреждение: формат креативов определить не удалось, всё помечено как баннер")
        return by_id, by_name

    for a in ads:
        cr = a.get("creative") or {}
        has_video = bool(cr.get("video_id"))
        ot = cr.get("object_type")
        fmt = "VIDEO" if (has_video or ot == "VIDEO") else "SHARE"
        if a.get("id"):
            by_id[a["id"]] = fmt
        if a.get("name"):
            by_name[a["name"]] = fmt

    vids = sum(1 for v in by_id.values() if v == "VIDEO")
    print(f"Креативов прочитано: {len(by_id)} (видео: {vids}, баннеры: {len(by_id) - vids})")
    return by_id, by_name


def main():
    print(f"Аккаунт: {ACCOUNT}, период с {START_DATE} по сегодня")

    acc_info = {}
    try:
        r = requests.get(
            f"{BASE}/{ACCOUNT}",
            params={"fields": "name,currency,account_id", "access_token": TOKEN},
            timeout=60,
        )
        if r.status_code == 200:
            acc_info = r.json()
        else:
            print("Предупреждение: не удалось прочитать имя аккаунта:", r.text[:200])
    except requests.RequestException as e:
        print("Предупреждение:", e)

    currency = acc_info.get("currency", "USD")
    cur_symbol = {"USD": "$", "EUR": "€", "KZT": "₸", "RUB": "₽"}.get(currency, "")

    # --- по дням ---
    daily_raw = insights("account")
    daily = []
    for r in sorted(daily_raw, key=lambda x: x.get("date_start", "")):
        row = base_row(r)
        row["date"] = r.get("date_start")
        daily.append(
            {
                "date": row["date"],
                "cost": row["cost"],
                "impr": row["impr"],
                "reach": row["reach"],
                "clicks": row["clicks"],
                "purch": row["purch"],
                "value": row["value"],
                "leads": row["leads"],
                "msg": row["msg"],
            }
        )
    print(f"Дней: {len(daily)}")

    # --- кампании ---
    camp_map = {}
    for r in insights("campaign"):
        name = r.get("campaign_name") or "—"
        c = camp_map.setdefault(name, {"name": name, "obj": r.get("objective") or "—"})
        merge(c, base_row(r))
    campaigns = []
    for c in camp_map.values():
        c["roas"] = round(c["value"] / c["cost"], 4) if c.get("purch") and c.get("cost") else None
        campaigns.append(c)
    campaigns.sort(key=lambda x: -x["cost"])
    print(f"Кампаний: {len(campaigns)}")

    # --- объявления (креативы) ---
    fmt_by_id, fmt_by_name = creative_formats()
    ad_map = {}
    for r in insights("ad"):
        name = r.get("ad_name") or "—"
        fmt = fmt_by_id.get(r.get("ad_id")) or fmt_by_name.get(name) or "SHARE"
        a = ad_map.setdefault(name, {"name": name, "fmt": fmt})
        if a["fmt"] == "SHARE" and fmt == "VIDEO":
            a["fmt"] = "VIDEO"
        merge(a, base_row(r))
    ads = []
    for a in ad_map.values():
        a["roas"] = round(a["value"] / a["cost"], 4) if a.get("purch") and a.get("cost") else None
        a.pop("leads", None)
        ads.append(a)
    ads.sort(key=lambda x: -x["cost"])
    print(f"Объявлений: {len(ads)}")

    now_almaty = datetime.now(timezone.utc) + timedelta(hours=5)
    payload = {
        "meta": {
            "account": acc_info.get("name", "Olive.kz"),
            "account_id": ACCOUNT,
            "currency": currency,
            "currency_symbol": cur_symbol,
            "source": "Meta Ads (Facebook/Instagram) — Marketing API, автообновление",
            "period_start": daily[0]["date"] if daily else START_DATE,
            "period_end": daily[-1]["date"] if daily else date.today().isoformat(),
            "generated": now_almaty.strftime("%Y-%m-%d"),
            "updated_at": now_almaty.strftime("%Y-%m-%d %H:%M") + " (Алматы)",
            "rows_note": f"{len(campaigns)} кампаний · {len(ads)} креативов",
            "pixel_note": PIXEL_NOTE,
        },
        "daily": daily,
        "campaigns": campaigns,
        "ads": ads,
    }

    if not daily:
        sys.exit("ОШИБКА: Meta вернула пустой набор данных — data.json не перезаписан")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Готово: {OUT} обновлён в {payload['meta']['updated_at']}")


if __name__ == "__main__":
    main()
