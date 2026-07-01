"""
fieldclimate_fetch.py
Recupera i dati climatici dell'ultima settimana dalla centralina Pessl/FieldClimate
(stazione 0020F61F, Agriturismo Baldeschi) e calcola l'indice di favorevolezza
climatica per la mosca olearia (Bactrocera oleae).

Sensori identificati:
  code=506  HC Air temperature  -> values.max (temperatura massima giornaliera)
  code=507  HC Relative humidity -> values.avg (umidità relativa media)

Gira ogni lunedì via GitHub Actions e salva il risultato in data/climate_history.json.
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

TEMP_CODE = 506   # HC Air temperature
HUM_CODE  = 507   # HC Relative humidity


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


def get_last_week_data(station_id: str) -> dict:
    path = f"/data/{station_id}/daily/last/7d"
    res  = requests.get(BASE_URL + path, headers=_signed_headers("GET", path))
    res.raise_for_status()
    return res.json()


def extract_values(sensors: list, code: int, aggr_key: str) -> list:
    """Estrae i valori giornalieri aggregati per un dato codice sensore."""
    for s in sensors:
        if s.get("code") == code:
            values = s.get("values", {})
            if isinstance(values, dict):
                return values.get(aggr_key, [])
    return []


def compute_climate_score(daily_data: dict) -> dict:
    sensors   = daily_data.get("data", [])
    tmax_list = extract_values(sensors, TEMP_CODE, "max")
    hum_list  = extract_values(sensors, HUM_CODE,  "avg")

    if not tmax_list:
        return {"score": None, "ok": False, "reason": "no temperature values"}

    favorable_days = sum(1 for t in tmax_list if 18 <= t <= 30)
    hot_dry_days   = sum(
        1 for i, t in enumerate(tmax_list)
        if t > 34 and (hum_list[i] < 40 if i < len(hum_list) else False)
    )
    score   = (favorable_days / len(tmax_list)) * 100 - hot_dry_days * 8
    score   = max(0, min(100, score))
    avg_max = sum(tmax_list) / len(tmax_list)

    print(f"  Tmax giornaliere: {tmax_list}")
    print(f"  Umidità media:    {hum_list}")
    print(f"  Giorni favorevoli (18-30°C): {favorable_days}/{len(tmax_list)}")
    print(f"  Giorni stress caldo-secco:   {hot_dry_days}")
    print(f"  Tmax media settimana:        {round(avg_max, 1)}°C")
    print(f"  Score climatico:             {round(score, 1)}/100")

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
    print(f"\n✅ Salvato in {OUTPUT_FILE}: score={result['score']}, avg_tmax={result['avg_tmax']}°C")


if __name__ == "__main__":
    print("Connessione a FieldClimate...")
    weekly = get_last_week_data(STATION_ID)
    print("Dati ricevuti. Calcolo indice climatico...\n")
    result = compute_climate_score(weekly)
    print(f"\nRisultato: {result}")
    if result.get("ok"):
        save_result(result)
