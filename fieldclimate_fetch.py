"""
fieldclimate_fetch.py
Recupera i dati climatici dell'ultima settimana dalla centralina Pessl/FieldClimate
e calcola l'indice di favorevolezza climatica per la mosca olearia.
"""

import os
import json
import hmac
import hashlib
import requests
from datetime import datetime, timezone

PUBLIC_KEY  = os.environ.get("FIELDCLIMATE_PUBLIC_KEY", "INSERISCI_QUI")
PRIVATE_KEY = os.environ.get("FIELDCLIMATE_PRIVATE_KEY", "INSERISCI_QUI")
STATION_ID  = os.environ.get("FIELDCLIMATE_STATION_ID", "INSERISCI_QUI")
BASE_URL    = "https://api.fieldclimate.com/v2"
OUTPUT_FILE = "data/climate_history.json"

TEMP_CODE = 506
HUM_CODE  = 507


def _signed_headers(method: str, path: str) -> dict:
    date_str  = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    raw       = f"{method}{path}{date_str}{PUBLIC_KEY}"
    signature = hmac.new(
        PRIVATE_KEY.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Date":          date_str,
        "Authorization": f"hmac {PUBLIC_KEY}:{signature}",
        "Accept":        "application/json",
    }


def get_last_week_data(station_id: str):
    path = f"/data/{station_id}/daily/last/7d"
    res  = requests.get(BASE_URL + path, headers=_signed_headers("GET", path))
    res.raise_for_status()
    return res.json()


def extract_climate_score(daily_data: dict) -> dict:
    sensors = daily_data.get("data", [])

    # Stampa struttura completa del sensore temperatura per debug
    print("\nStruttura sensore HC Air temperature (code 506):")
    for s in sensors:
        if s.get("code") == TEMP_CODE:
            print(json.dumps(s, indent=2))
            break

    # Cerca i valori in tutti i possibili campi
    tmax_list = []
    hum_list  = []
    for s in sensors:
        code = s.get("code")
        if code == TEMP_CODE:
            # Prova i campi più comuni
            for field in ("values", "aggr", "data", "max"):
                val = s.get(field)
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], (int, float)):
                    tmax_list = val
                    print(f"  Trovati valori temperatura in campo '{field}': {val}")
                    break
                elif isinstance(val, dict):
                    for subkey in ("max", "avg"):
                        subval = val.get(subkey, [])
                        if isinstance(subval, list) and len(subval) > 0:
                            tmax_list = subval
                            print(f"  Trovati valori temperatura in '{field}.{subkey}': {subval}")
                            break
                    if tmax_list:
                        break
        if code == HUM_CODE:
            for field in ("values", "aggr", "data", "avg"):
                val = s.get(field)
                if isinstance(val, list) and len(val) > 0 and isinstance(val[0], (int, float)):
                    hum_list = val
                    break
                elif isinstance(val, dict):
                    subval = val.get("avg", [])
                    if isinstance(subval, list) and len(subval) > 0:
                        hum_list = subval
                        break
                    if hum_list:
                        break

    print(f"\n  Temperatura max (ultimi 7gg): {tmax_list}")
    print(f"  Umidità media  (ultimi 7gg): {hum_list}")

    if not tmax_list:
        return {"score": None, "ok": False, "reason": "no temperature values found"}

    favorable_days = sum(1 for t in tmax_list if 18 <= t <= 30)
    hot_dry_days   = sum(
        1 for i, t in enumerate(tmax_list)
        if t > 34 and (hum_list[i] < 40 if i < len(hum_list) else False)
    )
    score   = (favorable_days / len(tmax_list)) * 100 - hot_dry_days * 8
    score   = max(0, min(100, score))
    avg_max = sum(tmax_list) / len(tmax_list)

    return {
        "score":          round(score, 1),
        "avg_tmax":       round(avg_max, 1),
        "favorable_days": favorable_days,
        "hot_dry_days":   hot_dry_days,
        "ok":             True,
    }


def save_result(result: dict):
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    history = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []
    entry = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), **result}
    history.insert(0, entry)
    history = history[:52]
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    print(f"\n✅ Salvato in {OUTPUT_FILE}")


if __name__ == "__main__":
    print("Connessione a FieldClimate...")
    weekly = get_last_week_data(STATION_ID)
    print(f"Chiavi nella risposta: {list(weekly.keys())}")
    result = extract_climate_score(weekly)
    print("\nRisultato finale:")
    print(result)
    if result.get("ok"):
        save_result(result)
