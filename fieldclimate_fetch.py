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

# Codici sensori identificati sulla centralina Baldeschi (0020F61F)
TEMP_CODE = 506   # HC Air temperature  -> usa aggregazione "max"
HUM_CODE  = 507   # HC Relative humidity -> usa aggregazione "avg"


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


def find_sensor_values(daily_data: dict, code: int, aggr_name: str) -> list:
    """
    FieldClimate separa la struttura dei sensori (in 'data') dai valori numerici
    (in 'raw' o direttamente nelle date). Cerca il canale (ch) associato al codice
    sensore e recupera i valori giornalieri aggregati.
    """
    sensors = daily_data.get("data", [])
    dates   = daily_data.get("dates", [])

    # Trova il canale (ch) associato al codice sensore
    ch = None
    for s in sensors:
        if s.get("code") == code:
            ch = s.get("ch")
            break

    if ch is None:
        print(f"  ⚠️  Sensore code={code} non trovato.")
        return []

    # I valori giornalieri sono in daily_data[str(ch)][aggr_name]
    ch_key = str(ch)
    values = daily_data.get(ch_key, {})
    if isinstance(values, dict):
        result = values.get(aggr_name, [])
    else:
        # Alcuni firmware Pessl mettono i valori direttamente nelle date
        result = []
        for d in dates:
            entry = daily_data.get(d, {})
            ch_data = entry.get(ch_key, {})
            val = ch_data.get(aggr_name)
            if val is not None:
                result.append(val)

    return result


def extract_climate_score(daily_data: dict) -> dict:
    print("\nRecupero valori temperatura e umidità dalla centralina...")

    tmax_list = find_sensor_values(daily_data, TEMP_CODE, "max")
    hum_list  = find_sensor_values(daily_data, HUM_CODE,  "avg")

    # Stampa raw per verifica
    print(f"  Temperatura max giornaliera (ultimi 7gg): {tmax_list}")
    print(f"  Umidità relativa media (ultimi 7gg):      {hum_list}")

    if not tmax_list:
        # Fallback: stampa l'intera risposta per debug
        print("\n⚠️  Nessun valore di temperatura trovato. Struttura risposta:")
        print(json.dumps({k: v for k, v in daily_data.items() if k != "data"}, indent=2)[:2000])
        return {"score": None, "ok": False, "reason": "no temperature values found"}

    favorable_days = sum(1 for t in tmax_list if 18 <= t <= 30)
    hot_dry_days   = sum(
        1 for i, t in enumerate(tmax_list)
        if t > 34 and (hum_list[i] < 40 if i < len(hum_list) else False)
    )
    score   = (favorable_days / len(tmax_list)) * 100 - hot_dry_days * 8
    score   = max(0, min(100, score))
    avg_max = sum(tmax_list) / len(tmax_list)

    print(f"\n  Giorni favorevoli (18-30°C): {favorable_days}/{len(tmax_list)}")
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
    print(f"\n✅ Salvato in {OUTPUT_FILE}")


if __name__ == "__main__":
    print("Connessione a FieldClimate...")
    weekly = get_last_week_data(STATION_ID)
   print(f"Risposta ricevuta. Chiavi presenti: {list(weekly.keys())[:10]}")
# Stampa il primo sensore per vedere la struttura completa
sensors = weekly.get("data", [])
if sensors:
    print("\nStruttura primo sensore:")
    print(json.dumps(sensors[0], indent=2))
    print("\nStruttura sensore temperatura (code 506):")
    for s in sensors:
        if s.get("code") == 506:
            print(json.dumps(s, indent=2))
            break
    result = extract_climate_score(weekly)
    print("\nRisultato finale:")
    print(result)
    if result.get("ok"):
        save_result(result)
