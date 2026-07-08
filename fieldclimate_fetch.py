"""
fieldclimate_fetch.py
Recupera i dati dalla centralina Pessl/FieldClimate (0020F61F, Agriturismo Baldeschi).

Calcola:
  - Score favorevolezza climatica per adulti mosca olearia
  - Indice di soppressione termica (stress da calore su uova/larve)
  - Dati orari: ore/giorno sopra soglie critiche (35°C, 37°C, 40°C)
  - Gradi giorno accumulati (GDD base 10°C)
  - Pioggia settimanale e rischio dilavamento

Soglie scientifiche (Bactrocera oleae):
  35°C x 6h/gg x 3+ giorni consecutivi -> mortalità uova/larve significativa
  37°C x 4h/gg x 2+ giorni consecutivi -> mortalità molto elevata
  40°C x 2h/gg                         -> quasi letale per uova e larve I età

Tutti i parametri configurabili in config.json.
Gira ogni lunedì via GitHub Actions. Salva in data/climate_history.json.

Sensori centralina:
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
    """Tenta di scaricare dati orari degli ultimi 7 giorni (168 ore)."""
    # Prova prima l'endpoint hourly, poi raw come fallback
    for path in [
        f"/data/{station_id}/hourly/last/7d",
        f"/data/{station_id}/raw/last/168h",
    ]:
        try:
            data = api_get(path)
            print(f"  Dati orari da: {path}")
            return data
        except Exception as e:
            print(f"  Endpoint {path} non disponibile: {e}")
    return None


def extract_values(sensors: list, code: int, aggr_key: str) -> list:
    for s in sensors:
        if s.get("code") == code:
            values = s.get("values", {})
            if isinstance(values, dict):
                return values.get(aggr_key, [])
    return []


def compute_hourly_thermal_stats(hourly_data: dict, cfg: dict) -> dict:
    """
    Analizza i dati orari di temperatura e calcola:
    - Ore/giorno sopra ogni soglia critica
    - Giorni consecutivi sopra soglia
    - Indice di soppressione termica (0-100)
    - Gradi giorno accumulati (GDD base 10°C)
    """
    if not hourly_data:
        return None

    # Stampa struttura per debug al primo run
    keys = list(hourly_data.keys())
    print(f"\n  Chiavi risposta oraria: {keys[:8]}")
    sensors = hourly_data.get("data", [])
    if sensors:
        for s in sensors:
            if s.get("code") == TEMP_CODE:
                vals = s.get("values", {})
                print(f"  Struttura valori temperatura oraria: tipo={type(vals).__name__}, "
                      f"chiavi={list(vals.keys())[:5] if isinstance(vals, dict) else 'lista'}")
                if isinstance(vals, dict):
                    for k, v in vals.items():
                        print(f"    aggr '{k}': {len(v) if isinstance(v, list) else type(v).__name__} valori")
                break

    # Estrai valori orari di temperatura
    hourly_temps = extract_values(sensors, TEMP_CODE, "avg")
    if not hourly_temps:
        hourly_temps = extract_values(sensors, TEMP_CODE, "inst")  # istantaneo
    if not hourly_temps:
        # Alcuni firmware usano "raw"
        for s in sensors:
            if s.get("code") == TEMP_CODE:
                vals = s.get("values", {})
                if isinstance(vals, dict):
                    for k, v in vals.items():
                        if isinstance(v, list) and len(v) > 24:
                            hourly_temps = v
                            print(f"    Usata aggregazione '{k}' come proxy orario")
                            break
                break

    if not hourly_temps or len(hourly_temps) < 24:
        print(f"  ⚠️  Dati orari insufficienti ({len(hourly_temps) if hourly_temps else 0} valori)")
        return None

    print(f"  Valori orari disponibili: {len(hourly_temps)} ore")

    # Soglie da config
    th = cfg.get("soglie_termiche", {
        "s35_ore_min": 6, "s35_giorni_cons": 3,
        "s37_ore_min": 4, "s37_giorni_cons": 2,
        "s40_ore_min": 2, "s40_giorni_cons": 1,
        "gdd_base": 10
    })

    # Raggruppa per giorno (blocchi di 24 ore)
    n_days = len(hourly_temps) // 24
    days = [hourly_temps[i*24:(i+1)*24] for i in range(n_days)]

    daily_h35, daily_h37, daily_h40 = [], [], []
    daily_h_optimal = []  # 18-30°C
    daily_gdd = []

    for day_temps in days:
        valid = [t for t in day_temps if t is not None]
        if not valid:
            daily_h35.append(0); daily_h37.append(0)
            daily_h40.append(0); daily_h_optimal.append(0)
            daily_gdd.append(0)
            continue
        daily_h35.append(sum(1 for t in valid if t >= 35))
        daily_h37.append(sum(1 for t in valid if t >= 37))
        daily_h40.append(sum(1 for t in valid if t >= 40))
        daily_h_optimal.append(sum(1 for t in valid if 18 <= t <= 30))
        avg_day = sum(valid) / len(valid)
        daily_gdd.append(max(0, avg_day - th["gdd_base"]))

    # Giorni consecutivi sopra soglia
    def max_consecutive(daily_h, min_h):
        max_c = cur = 0
        for h in daily_h:
            cur = cur + 1 if h >= min_h else 0
            max_c = max(max_c, cur)
        return max_c

    cons35 = max_consecutive(daily_h35, th["s35_ore_min"])
    cons37 = max_consecutive(daily_h37, th["s37_ore_min"])
    cons40 = max_consecutive(daily_h40, th["s40_ore_min"])

    # Indice soppressione termica (0-100)
    # Scala: quant più giorni sopra soglia e più ore, maggiore la soppressione
    score_35 = min(1.0, cons35 / th["s35_giorni_cons"]) * 30
    score_37 = min(1.0, cons37 / th["s37_giorni_cons"]) * 40
    score_40 = min(1.0, cons40 / th["s40_giorni_cons"]) * 30
    soppressione = round(min(100, score_35 + score_37 + score_40), 1)

    gdd_tot = round(sum(daily_gdd), 1)

    # Score favorevolezza adulti basato su dati orari
    avg_hours_optimal = sum(daily_h_optimal) / len(daily_h_optimal) if daily_h_optimal else 0
    hourly_adult_score = round(min(100, avg_hours_optimal / 24 * 100 * 1.5), 1)
    avg_h37 = sum(daily_h37) / len(daily_h37) if daily_h37 else 0
    hourly_adult_score = round(max(0, hourly_adult_score - avg_h37 * 3), 1)

    print(f"\n  Ore/giorno sopra 35°C: {daily_h35}")
    print(f"  Ore/giorno sopra 37°C: {daily_h37}")
    print(f"  Ore/giorno sopra 40°C: {daily_h40}")
    print(f"  Ore/giorno 18-30°C:    {daily_h_optimal}")
    print(f"  GDD settimanali:       {gdd_tot}")
    print(f"  Giorni consecutivi ≥35°C (≥{th['s35_ore_min']}h): {cons35}")
    print(f"  Giorni consecutivi ≥37°C (≥{th['s37_ore_min']}h): {cons37}")
    print(f"  Giorni consecutivi ≥40°C (≥{th['s40_ore_min']}h): {cons40}")
    print(f"  Indice soppressione termica: {soppressione}/100")
    print(f"  Score favorevolezza adulti (orario): {hourly_adult_score}/100")
    print(f"  [confronto: score da Tmax giornaliera sarebbe diverso]")

    return {
        "daily_hours_above_35": daily_h35,
        "daily_hours_above_37": daily_h37,
        "daily_hours_above_40": daily_h40,
        "daily_hours_optimal":  daily_h_optimal,
        "cons_days_above_35":   cons35,
        "cons_days_above_37":   cons37,
        "cons_days_above_40":   cons40,
        "thermal_suppression":  soppressione,
        "gdd_weekly":           gdd_tot,
        "hourly_adult_score":   hourly_adult_score,
        "avg_hours_optimal":    round(avg_hours_optimal, 1),
    }


def compute_climate_score(daily_data: dict, cfg: dict) -> dict:
    sensors   = daily_data.get("data", [])
    tmax_list = extract_values(sensors, TEMP_CODE, "max")
    tmin_list = extract_values(sensors, TEMP_CODE, "min")
    hum_list  = extract_values(sensors, HUM_CODE,  "avg")
    rain_list = extract_values(sensors, RAIN_CODE, "sum")

    if not tmax_list:
        return {"ok": False, "reason": "no temperature values"}

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
    avg_min = round(sum(tmin_list) / len(tmin_list), 1) if tmin_list else None
    cold_nights = sum(1 for t in tmin_list if t < 10) if tmin_list else None

    d = cfg["dilavamento"]
    rain_breve     = rain_list[-d["finestra_giorni"]:] if rain_list else []
    max_rain_breve = max(rain_breve) if rain_breve else 0
    washout_risk   = max_rain_breve >= d["soglia_mm_giornalieri"]
    total_rain     = round(sum(rain_list), 1) if rain_list else None
    rainy_days     = sum(1 for r in rain_list if r >= 1) if rain_list else None
    max_daily_rain = max(rain_list) if rain_list else None

    print(f"  Tmax giornaliere (°C):  {tmax_list}")
    print(f"  Tmin giornaliere (°C):  {tmin_list}")
    print(f"  Umidità media (%):      {hum_list}")
    print(f"  Pioggia (mm):           {rain_list}")
    print(f"  Pioggia totale:         {total_rain}mm | Giorni pioggia: {rainy_days}")
    print(f"  Tmax media: {round(avg_max,1)}°C | Tmin media: {avg_min}°C")
    print(f"  Notti fredde (<10°C):   {cold_nights}")
    print(f"  Giorni favorevoli mosca ({finestra_score}gg): {favorable_days}")
    print(f"  Rischio dilavamento (ultimi {d['finestra_giorni']}gg): {'⚠️  SÌ' if washout_risk else '✅ NO'} ({max_rain_breve}mm)")
    print(f"  Score favorevolezza adulti (da Tmax giornaliera, fallback): {round(score,1)}/100")

    return {
        "score_daily_fallback":   round(score, 1),
        "score":                  round(score, 1),  # verrà sovrascritto da hourly se disponibile
        "avg_tmax":               round(avg_max, 1),
        "avg_tmin":               avg_min,
        "cold_nights":            cold_nights,
        "favorable_days":         favorable_days,
        "hot_dry_days":           hot_dry_days,
        "total_rain_mm":          total_rain,
        "rainy_days":             rainy_days,
        "max_daily_rain_mm":      max_daily_rain,
        "washout_window_days":    d["finestra_giorni"],
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
    if result.get("treatment_washout_risk"):
        print(f"   ⚠️  {result['washout_rain_mm']}mm negli ultimi {result['washout_window_days']}gg — verifica se un trattamento è stato dilavato")
    if result.get("thermal_suppression", 0) > 50:
        print(f"   🌡️  Soppressione termica elevata ({result['thermal_suppression']}/100) — caldo intenso sta riducendo la sopravvivenza di uova e larve")


if __name__ == "__main__":
    print("Caricamento config.json...")
    cfg = load_config()

    print("\nRecupero dati giornalieri (7gg)...")
    daily  = get_daily_data(STATION_ID)
    result = compute_climate_score(daily, cfg)

    print("\nRecupero dati orari (7gg)...")
    hourly = get_hourly_data(STATION_ID)
    thermal = compute_hourly_thermal_stats(hourly, cfg)
    if thermal:
        result.update(thermal)
        # Sostituisce lo score grezzo (da Tmax) con quello preciso (da dati orari)
        result["score"] = thermal["hourly_adult_score"]
        print(f"  Score favorevolezza adulti AGGIORNATO (da orario): {result['score']}/100")
        print(f"  Score da Tmax giornaliera (vecchio metodo): {result['score_daily_fallback']}/100")
    else:
        print("  ⚠️  Dati orari non disponibili — uso score da Tmax giornaliera come fallback")

    print(f"\nRisultato finale: {result}")
    if result.get("ok"):
        save_result(result)
