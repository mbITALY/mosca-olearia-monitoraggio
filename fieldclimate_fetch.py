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


def extract_climate_score(daily_data) -> dict:
    # FieldClimate restituisce i dati come lista di sensori
    if isinstance(daily_data, list):
        sensors = daily_data
    else:
        sensors = daily_data.get("data", [])

    print("\nSensori disponibili in questa stazione:")
    for s in sensors:
        print(f"  code={s.get('code')} ch={s.get('ch')} "
              f"name='{s.get('name')}' aggr={s.get('aggr')}")

    # Cerca automaticamente temperatura aria (codice 506) e umidità (codice 507 o simile)
    temp_sensor = None
    hum_sensor  = None
    for s in sensors:
        code = str(s.get("code", ""))
        name = s.get("name", "").lower()
        aggr = s.get("aggr", {})
        if "max" in aggr and ("temp" in name or code in ("506", "507")) and temp_sensor is None:
            temp_sensor = s
        if "avg" in aggr and ("humid" in name or "umid" in name) and hum_sensor is None:
            hum_sensor = s

    if temp_sensor is None:
        print("\n⚠️  Sensore temperatura non trovato automaticamente.")
        print("Controlla i codici stampati sopra e comunicali a Claude.")
        return {"score": None, "ok": False, "reason": "temperature sensor not found"}

    tmax_list = temp_sensor["aggr"].get("max", [])
    hum_list  = hum_sensor["aggr"].get("avg", []) if hum_sensor else []

    print(f"\nTemperatura usata: '{temp_sensor.get('name')}' -> max {tmax_list}")
    if hum_sensor:
        print(f"Umidità usata:     '{hum_sensor.get('name')}' -> avg {hum_list}")

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
    print(f"\nSalvato in {OUTPUT_FILE}: {entry}")


if __name__ == "__main__":
    print("Connessione a FieldClimate...")
    weekly = get_last_week_data(STATION_ID)
    print(f"Risposta ricevuta (tipo: {type(weekly).__name__})")
    result = extract_climate_score(weekly)
    print("\nRisultato finale:")
    print(result)
    if result.get("ok"):
        save_result(result)
