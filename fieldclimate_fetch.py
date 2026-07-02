"""
fieldclimate_fetch.py
Recupera i dati climatici dalla centralina Pessl/FieldClimate
(stazione 0020F61F, Agriturismo Baldeschi) e calcola:
  - Score di favorevolezza climatica per la mosca olearia (finestra: config)
  - Rischio dilavamento esca Spintor Fly (finestra breve: config)
  - Pioggia totale settimanale

Tutti i parametri sono in config.json — non modificare questo script.

Sensori:
  code=506  HC Air temperature   -> values.max
  code=507  HC Relative humidity -> values.avg
  code=6    Precipitation        -> values.sum

Gira ogni lunedì via GitHub Actions. Salva in data/climate_history.json.
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
CONFIG_FILE = "config.json"

TEMP_CODE = 506
HUM_CODE  = 507
RAIN_CODE = 6


def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


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
    for s in sensors:
        if s.get("code") == code:
            values = s.get("values", {})
            if isinstance(values, dict):
                return values.get(aggr_key, [])
    return []


def compute_results(daily_data: dict, cfg: dict) -> dict:
    sensors   = daily_data.get("data", [])
    tmax_list = extract_values(sensors, TEMP_CODE, "max")
    hum_list  = extract_values(sensors, HUM_CODE,  "avg")
    rain_list = extract_values(sensors, RAIN_CODE, "sum")

    if not tmax_list:
        return {"ok": False, "reason": "no temperature values"}

    # --- Score climatico (finestra configurabile, default 7gg) ---
    c = cfg["clima"]
    finestra_score = c["finestra_score_giorni"]
    tmax_score = tmax_list[-finestra_score:]
    hum_score  = hum_list[-finestra_score:] if hum_list else []

    favorable_days = sum(1 for t in tmax_score if c["temp_favorevole_min"] <= t <= c["temp_favorevole_max"])
    hot_dry_days   = sum(
        1 for i, t in enumerate(tmax_score)
        if t > c["temp_stress_caldo"] and (hum_score[i] < c["umidita_stress_secco"] if i < len(hum_score) else False)
    )
    score   = (favorable_days / len(tmax_score)) * 100 - hot_dry_days * c["penalita_giorno_stress"]
    score   = max(0, min(100, score))
    avg_max = sum(tmax_score) / len(tmax_score)

    # --- Rischio dilavamento (finestra breve configurabile, default 2gg) ---
    d = cfg["dilavamento"]
    finestra_dilav  = d["finestra_giorni"]
    soglia_dilav    = d["soglia_mm_giornalieri"]
    rain_breve      = rain_list[-finestra_dilav:] if rain_list else []
    max_rain_breve  = max(rain_breve) if rain_breve else 0
    washout_risk    = max_rain_breve >= soglia_dilav

    # --- Pioggia totale settimana ---
    total_rain    = round(sum(rain_list), 1) if rain_list else None
    rainy_days    = sum(1 for r in rain_list if r >= 1) if rain_list else None
    max_daily_rain = max(rain_list) if rain_list else None

    print(f"  Tmax giornaliere (°C):             {tmax_list}")
    print(f"  Umidità media (%):                 {hum_list}")
    print(f"  Pioggia giornaliera (mm):          {rain_list}")
    print(f"  Pioggia totale settimana:          {total_rain} mm")
    print(f"  Pioggia ultimi {finestra_dilav}gg (dilavamento): {rain_breve} -> max {max_rain_breve}mm")
    print(f"  Rischio dilavamento esca:          {'⚠️  SÌ' if washout_risk else '✅ NO'}")
    print(f"  Giorni favorevoli mosca ({finestra_score}gg):    {favorable_days}/{len(tmax_score)}")
    print(f"  Score climatico:                   {round(score, 1)}/100")

    return {
        "score":                  round(score, 1),
        "avg_tmax":               round(avg_max, 1),
        "favorable_days":         favorable_days,
        "hot_dry_days":           hot_dry_days,
        "total_rain_mm":          total_rain,
        "rainy_days":             rainy_days,
        "max_daily_rain_mm":      max_daily_rain,
        "washout_window_days":    finestra_dilav,
        "washout_rain_mm":        round(max_rain_breve, 1),
        "treatment_washout_risk": washout_risk,
        "ok":                     True,
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
    if result["treatment_washout_risk"]:
        print(f"   ⚠️  {result['washout_rain_mm']}mm negli ultimi {result['washout_window_days']}gg — verifica se un trattamento è stato dilavato")


if __name__ == "__main__":
    print("Caricamento config.json...")
    cfg = load_config()
    print(f"  Finestra score: {cfg['clima']['finestra_score_giorni']}gg | "
          f"Finestra dilavamento: {cfg['dilavamento']['finestra_giorni']}gg | "
          f"Soglia dilavamento: {cfg['dilavamento']['soglia_mm_giornalieri']}mm\n")

    print("Connessione a FieldClimate...")
    weekly = get_last_week_data(STATION_ID)
    print("Dati ricevuti. Calcolo...\n")

    result = compute_results(weekly, cfg)
    print(f"\nRisultato: {result}")
    if result.get("ok"):
        save_result(result)
