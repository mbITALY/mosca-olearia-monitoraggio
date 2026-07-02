"""
fieldclimate_fetch.py
Recupera i dati climatici dell'ultima settimana dalla centralina Pessl/FieldClimate
(stazione 0020F61F, Agriturismo Baldeschi) e calcola l'indice di favorevolezza
climatica per la mosca olearia (Bactrocera oleae).

Sensori identificati:
  code=506  HC Air temperature   -> values.max (temperatura massima giornaliera)
  code=507  HC Relative humidity -> values.avg (umidità relativa media)
  code=6    Precipitation        -> values.sum (pioggia giornaliera mm)

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

TEMP_CODE  = 506   # HC Air temperature
HUM_CODE   = 507   # HC Relative humidity
RAIN_CODE  = 6     # Precipitation


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
    rain_list = extract_values(sensors, RAIN_CODE, "sum")

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

    # Calcola pioggia totale e giorni con pioggia significativa (>= 1mm)
    total_rain   = round(sum(rain_list), 1) if rain_list else None
    rainy_days   = sum(1 for r in rain_list if r >= 1) if rain_list else None

    # Segnala se un eventuale trattamento potrebbe essere stato dilavato
    # (>= 2mm in un singolo giorno = esca compromessa)
    max_daily_rain = max(rain_list) if rain_list else None
    treatment_risk = max_daily_rain is not None and max_daily_rain >= 2

    print(f"  Tmax giornaliere (°C):      {tmax_list}")
    print(f"  Umidità media (%):          {hum_list}")
    print(f"  Pioggia giornaliera (mm):   {rain_list}")
    print(f"  Pioggia totale settimana:   {total_rain} mm")
    print(f"  Giorni con pioggia >= 1mm:  {rainy_days}")
    print(f"  Rischio dilavamento esca:   {'SÌ' if treatment_risk else 'NO'}")
    print(f"  Giorni favorevoli mosca:    {favorable_days}/{len(tmax_list)}")
    print(f"  Score climatico:            {round(score, 1)}/100")

    return {
        "score":              round(score, 1),
        "avg_tmax":           round(avg_max, 1),
        "favorable_days":     favorable_days,
        "hot_dry_days":       hot_dry_days,
        "total_rain_mm":      total_rain,
        "rainy_days":         rainy_days,
        "max_daily_rain_mm":  max_daily_rain,
        "treatment_washout_risk": treatment_risk,
        "ok":                 True,
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
    print(f"   Score climatico: {result['score']}/100")
    print(f"   Tmax media:      {result['avg_tmax']}°C")
    print(f"   Pioggia totale:  {result['total_rain_mm']} mm")
    if result['treatment_washout_risk']:
        print(f"   ⚠️  Pioggia >= 2mm rilevata — verifica se un trattamento è stato dilavato")


if __name__ == "__main__":
    print("Connessione a FieldClimate...")
    weekly = get_last_week_data(STATION_ID)
    print("Dati ricevuti. Calcolo indice climatico...\n")
    result = compute_climate_score(weekly)
    print(f"\nRisultato: {result}")
    if result.get("ok"):
        save_result(result)
