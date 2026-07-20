"""
fieldclimate_fetch.py
Centralina Pessl 0020F61F — Agriturismo Baldeschi, Umbria

Calcola settimanalmente:
  1. Score favorevolezza adulti (dati orari, range 23-29°C ottimale)
  2. Indice soppressione termica uova/larve (soglie validate su B. oleae)
  3. Stress pupale (ore >30°C)
  4. GDD accumulati (base 10°C)
  5. Dati pioggia e rischio dilavamento Spintor Fly

Soglie scientifiche da letteratura (8 studi su B. oleae):
  ADULTI
    23-29°C  : range ottimale (PLoS ONE 2015, Fletcher & Kapatos 1978/83, Tzanakakis)
    >31°C    : inizio mortalità tutti gli stadi (PLoS ONE 2015, Wang et al. 2009)
    >35°C    : adulti immobili (Insects 2021, Wang 2009)
  UOVA/LARVE
    >33°C x 4h/gg x 3+ giorni: mortalità significativa (Wang 2009, PLoS ONE, Girolami 1979)
    >37.5°C x 2h/gg x 2+ giorni: nessuna schiusa uova (Wang 2009 - regime 37.8°C)
    >40°C x 1h/gg             : mortalità quasi totale
  PUPE
    >30°C    : limite superiore sviluppo pupale (Girolami 1979/Tsitsipis 1977)

Tutti i parametri configurabili in config.json — non modificare questo script.

Sensori:
  code=506  HC Air temperature   -> max, min, avg
  code=507  HC Relative humidity -> avg
  code=6    Precipitation        -> sum
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


def api_get(path: str) -> dict:
    res = requests.get(BASE_URL + path, headers=_signed_headers("GET", path))
    res.raise_for_status()
    return res.json()


def get_daily_data(station_id: str) -> dict:
    return api_get(f"/data/{station_id}/daily/last/7d")


def get_hourly_data(station_id: str) -> dict:
    try:
        data = api_get(f"/data/{station_id}/hourly/last/7d")
        print("  Dati orari: /hourly/last/7d")
        return data
    except Exception as e:
        print(f"  Endpoint /hourly non disponibile: {e}")
        return None


def extract_values(sensors: list, code: int, aggr_key: str) -> list:
    for s in sensors:
        if s.get("code") == code:
            values = s.get("values", {})
            if isinstance(values, dict):
                return values.get(aggr_key, [])
    return []


def compute_daily_result(daily_data: dict, cfg: dict) -> dict:
    """Elabora dati giornalieri (fallback se orari non disponibili)."""
    sensors   = daily_data.get("data", [])
    tmax_list = extract_values(sensors, TEMP_CODE, "max")
    tmin_list = extract_values(sensors, TEMP_CODE, "min")
    hum_list  = extract_values(sensors, HUM_CODE,  "avg")
    rain_list = extract_values(sensors, RAIN_CODE, "sum")

    if not tmax_list:
        return {"ok": False, "reason": "no temperature values"}

    d  = cfg["dilavamento"]
    rain_breve     = rain_list[-d["finestra_giorni"]:] if rain_list else []
    max_rain_breve = max(rain_breve) if rain_breve else 0
    total_rain     = round(sum(rain_list), 1) if rain_list else None
    rainy_days     = sum(1 for r in rain_list if r >= 1) if rain_list else None

    avg_max    = round(sum(tmax_list) / len(tmax_list), 1)
    avg_min    = round(sum(tmin_list) / len(tmin_list), 1) if tmin_list else None
    cold_nights = sum(1 for t in tmin_list if t < 10) if tmin_list else None

    print(f"  Tmax: {tmax_list}")
    print(f"  Tmin: {tmin_list}")
    print(f"  Umidità: {hum_list}")
    print(f"  Pioggia: {rain_list} | Totale: {total_rain}mm")
    print(f"  Tmax media: {avg_max}°C | Tmin media: {avg_min}°C | Notti fredde: {cold_nights}")

    return {
        "ok":                     True,
        "avg_tmax":               avg_max,
        "avg_tmin":               avg_min,
        "cold_nights":            cold_nights,
        "total_rain_mm":          total_rain,
        "rainy_days":             rainy_days,
        "max_daily_rain_mm":      max(rain_list) if rain_list else None,
        "washout_window_days":    d["finestra_giorni"],
        "washout_rain_mm":        round(max_rain_breve, 1),
        "treatment_washout_risk": max_rain_breve >= d["soglia_mm_giornalieri"],
        "tmax_list":              tmax_list,
    }


def compute_hourly_analysis(hourly_data: dict, cfg: dict) -> dict:
    """
    Analisi oraria completa. Restituisce score adulti e soppressione termica
    basati sulle soglie validate dalla letteratura scientifica su B. oleae.
    """
    if not hourly_data:
        return None

    sensors       = hourly_data.get("data", [])
    hourly_temps  = extract_values(sensors, TEMP_CODE, "avg")

    if not hourly_temps or len(hourly_temps) < 24:
        print(f"  Dati orari insufficienti: {len(hourly_temps) if hourly_temps else 0} valori")
        return None

    print(f"  Dati orari disponibili: {len(hourly_temps)} ore")

    a   = cfg["adulti"]
    sl  = cfg["soppressione_uova_larve"]
    n_days = len(hourly_temps) // 24
    days   = [hourly_temps[i*24:(i+1)*24] for i in range(n_days)]

    # --- Per ogni giorno ---
    daily_h_optimal   = []   # ore 23-29°C (ottimale adulti)
    daily_h_subopt    = []   # ore 18-23°C o 29-31°C (subottimale adulti)
    daily_h_stress    = []   # ore >31°C (stress adulti)
    daily_h_immobile  = []   # ore >35°C (adulti immobili)
    daily_h33         = []   # ore >33°C (stress uova/larve)
    daily_h37         = []   # ore >37°C (stress elevato uova)
    daily_h40         = []   # ore >40°C (quasi letale)
    daily_h_pupal     = []   # ore >30°C (stress pupale)
    daily_gdd         = []   # gradi giorno (base 10°C)

    for day_temps in days:
        valid = [t for t in day_temps if t is not None]
        if not valid:
            for lst in [daily_h_optimal, daily_h_subopt, daily_h_stress,
                        daily_h_immobile, daily_h33, daily_h37, daily_h40,
                        daily_h_pupal, daily_gdd]:
                lst.append(0)
            continue

        daily_h_optimal.append(sum(1 for t in valid if a["temp_ottimale_min"] <= t <= a["temp_ottimale_max"]))
        daily_h_subopt.append(sum(1 for t in valid if
            (a["temp_subottimale_min"] <= t < a["temp_ottimale_min"]) or
            (a["temp_ottimale_max"] < t <= a["temp_subottimale_max"])))
        daily_h_stress.append(sum(1 for t in valid if t > a["temp_stress_inizio"]))
        daily_h_immobile.append(sum(1 for t in valid if t > a["temp_immobilita"]))
        daily_h33.append(sum(1 for t in valid if t > 33))
        daily_h37.append(sum(1 for t in valid if t > 37))
        daily_h40.append(sum(1 for t in valid if t > 40))
        daily_h_pupal.append(sum(1 for t in valid if t > sl["limite_pupe_gradi"]))
        daily_gdd.append(max(0, sum(valid)/len(valid) - sl["gdd_base"]))

    # --- Score favorevolezza adulti (range scientifico 23-29°C) ---
    avg_h_opt    = sum(daily_h_optimal) / n_days
    avg_h_subopt = sum(daily_h_subopt)  / n_days
    avg_h_stress = sum(daily_h_stress)  / n_days
    # Ore ottimali = peso 1.0, ore subottimali = peso 0.4, ore stress = 0
    hourly_adult_score = round(min(100,
        (avg_h_opt + avg_h_subopt * a["peso_ore_subottimali"]) / 24 * 100
    ), 1)

    # --- Funzione giorni consecutivi ---
    def max_consecutive(daily_h, min_h):
        max_c = cur = 0
        for h in daily_h:
            cur = cur + 1 if h >= min_h else 0
            max_c = max(max_c, cur)
        return max_c

    cons33 = max_consecutive(daily_h33, sl["s33_ore_min"])
    cons37 = max_consecutive(daily_h37, sl["s37_ore_min"])
    cons40 = max_consecutive(daily_h40, sl["s40_ore_min"])

    # --- Indice soppressione termica uova/larve (0-100) ---
    score_33 = min(1.0, cons33 / sl["s33_giorni_cons"]) * 30
    score_37 = min(1.0, cons37 / sl["s37_giorni_cons"]) * 40
    score_40 = min(1.0, cons40 / sl["s40_giorni_cons"]) * 30
    soppressione = round(min(100, score_33 + score_37 + score_40), 1)

    # --- Stress pupale (% giorni con ore >30°C) ---
    giorni_stress_pupale = sum(1 for h in daily_h_pupal if h > 0)
    stress_pupale = round(giorni_stress_pupale / n_days * 100, 1)

    gdd_tot = round(sum(daily_gdd), 1)

    print(f"\n  Range ottimale adulti (23-29°C):    ore/gg {daily_h_optimal}")
    print(f"  Range subottimale (18-23/29-31°C):  ore/gg {daily_h_subopt}")
    print(f"  Stress adulti (>31°C):              ore/gg {daily_h_stress}")
    print(f"  Adulti immobili (>35°C):            ore/gg {daily_h_immobile}")
    print(f"  Stress uova/larve (>33°C):          ore/gg {daily_h33}")
    print(f"  Stress elevato uova (>37°C):        ore/gg {daily_h37}")
    print(f"  Quasi letale (>40°C):               ore/gg {daily_h40}")
    print(f"  Stress pupale (>30°C):              ore/gg {daily_h_pupal}")
    print(f"  GDD settimanali (base 10°C):        {gdd_tot}")
    print(f"\n  Score favorevolezza adulti (23-29°C): {hourly_adult_score}/100")
    print(f"  Soppressione termica uova/larve:      {soppressione}/100")
    print(f"  Stress pupale ({giorni_stress_pupale}/7 giorni >30°C): {stress_pupale}%")

    return {
        "hourly_adult_score":       hourly_adult_score,
        "avg_hours_optimal":        round(avg_h_opt, 1),
        "avg_hours_suboptimal":     round(avg_h_subopt, 1),
        "avg_hours_stress":         round(avg_h_stress, 1),
        "daily_hours_optimal":      daily_h_optimal,
        "daily_hours_above_33":     daily_h33,
        "daily_hours_above_37":     daily_h37,
        "daily_hours_above_40":     daily_h40,
        "daily_hours_pupal_stress": daily_h_pupal,
        "cons_days_above_33":       cons33,
        "cons_days_above_37":       cons37,
        "cons_days_above_40":       cons40,
        "thermal_suppression":      soppressione,
        "pupal_stress_pct":         stress_pupale,
        "gdd_weekly":               gdd_tot,
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
    print(f"   Score adulti:          {result.get('score','-')}/100")
    print(f"   Soppressione termica:  {result.get('thermal_suppression','-')}/100")
    print(f"   Pioggia totale:        {result.get('total_rain_mm','-')} mm")
    if result.get("treatment_washout_risk"):
        print(f"   ⚠️  {result['washout_rain_mm']}mm negli ultimi {result['washout_window_days']}gg — verifica se un trattamento è stato dilavato")
    if result.get("thermal_suppression", 0) > 50:
        print(f"   🌡️  Alta soppressione termica — mortalità elevata di uova e larve")


if __name__ == "__main__":
    print("Caricamento config.json...")
    cfg = load_config()

    print("\nRecupero dati giornalieri (7gg)...")
    daily  = get_daily_data(STATION_ID)
    result = compute_daily_result(daily, cfg)

    if not result.get("ok"):
        print(f"Errore dati giornalieri: {result.get('reason')}")
        exit(1)

    print("\nRecupero dati orari (7gg)...")
    hourly  = get_hourly_data(STATION_ID)
    thermal = compute_hourly_analysis(hourly, cfg)

    if thermal:
        result.update(thermal)
        result["score"] = thermal["hourly_adult_score"]
        print(f"\n  Score adulti FINALE (dati orari, 23-29°C): {result['score']}/100")
    else:
        # fallback: stima grezza da Tmax
        tmax_list = result.pop("tmax_list", [])
        fav = sum(1 for t in tmax_list if 23 <= t <= 29)
        result["score"] = round(fav / len(tmax_list) * 100, 1) if tmax_list else 50
        result["score_source"] = "daily_fallback"
        print(f"\n  ⚠️  Dati orari non disponibili — score stimato da Tmax: {result['score']}/100")
    # rimuovi tmax_list dal risultato se non usata
    result.pop("tmax_list", None)

    print(f"\nRisultato finale: {result}")
    if result.get("ok"):
        save_result(result)
