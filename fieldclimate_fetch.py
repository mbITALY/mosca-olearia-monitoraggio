"""
fieldclimate_fetch.py

Recupera i dati climatici dell'ultima settimana dalla centralina Pessl Instruments
(via FieldClimate API v2, autenticazione HMAC) e calcola lo stesso indice di
favorevolezza climatica usato nel calcolatore "Indice di rischio mosca olearia".

REQUISITI
---------
pip install requests --break-system-packages

CONFIGURAZIONE
--------------
1. Login su https://ng.fieldclimate.com
2. Menu utente -> API services -> GENERATE NEW per ottenere PUBLIC_KEY e PRIVATE_KEY
3. Inserisci le chiavi qui sotto (meglio: come variabili d'ambiente, non hardcoded)
4. Trova lo station_id dalla lista stazioni (vedi list_stations() sotto)

USO
---
python fieldclimate_fetch.py
"""

import os
import json
import hmac
import hashlib
import requests
from datetime import datetime, timezone

# --- Configurazione ---
PUBLIC_KEY = os.environ.get("FIELDCLIMATE_PUBLIC_KEY", "INSERISCI_QUI")
PRIVATE_KEY = os.environ.get("FIELDCLIMATE_PRIVATE_KEY", "INSERISCI_QUI")
STATION_ID = os.environ.get("FIELDCLIMATE_STATION_ID", "INSERISCI_QUI")  # es. "10293"
BASE_URL = "https://api.fieldclimate.com/v2"
OUTPUT_FILE = "data/climate_history.json"


def _signed_headers(method: str, path: str) -> dict:
    """
    Costruisce gli header firmati HMAC-SHA256 richiesti da FieldClimate.
    La firma è: HMAC_SHA256(private_key, METHOD + path + date + public_key)
    """
    date_str = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    raw = f"{method}{path}{date_str}{PUBLIC_KEY}"
    signature = hmac.new(
        PRIVATE_KEY.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Date": date_str,
        "Authorization": f"hmac {PUBLIC_KEY}:{signature}",
        "Accept": "application/json",
    }


def list_stations():
    """Elenca le stazioni associate al tuo account, con il relativo station_id."""
    path = "/user/stations"
    headers = _signed_headers("GET", path)
    res = requests.get(BASE_URL + path, headers=headers)
    res.raise_for_status()
    return res.json()


def get_last_week_data(station_id: str):
    """
    Recupera i dati giornalieri aggregati (temperatura max/min, umidità media,
    precipitazione, vento) degli ultimi 7 giorni dalla stazione indicata.
    """
    path = f"/data/{station_id}/daily/last/7d"
    headers = _signed_headers("GET", path)
    res = requests.get(BASE_URL + path, headers=headers)
    res.raise_for_status()
    return res.json()


def extract_climate_score(daily_data: dict) -> dict:
    data = daily_data.get("data", [])

    # Stampa tutti i sensori disponibili per identificare i tag giusti
    print("\nSensori disponibili in questa stazione:")
    for sensor in data:
        name = sensor.get("name", "?")
        code = sensor.get("code", "?")
        ch = sensor.get("ch", "?")
        aggr_keys = list(sensor.get("aggr", {}).keys())
        print(f"  code={code} ch={ch} name='{name}' aggregazioni={aggr_keys}")

    # Cerca automaticamente temperatura aria e umidità relativa
    temp_sensor = None
    hum_sensor = None
    for sensor in data:
        code = sensor.get("code")
        if code in (507, 506) and temp_sensor is None:
            temp_sensor = sensor
        if code in (507, 3) and "avg" in sensor.get("aggr", {}) and hum_sensor is None:
            # codice 3 = umidità relativa in Pessl
            if "humidity" in sensor.get("name", "").lower() or code == 3:
                hum_sensor = sensor

    if temp_sensor is None:
        print("\nNon ho trovato un sensore di temperatura aria. Controlla i codici sopra.")
        return {"score": None, "ok": False, "reason": "temperature sensor not found"}

    tmax_list = temp_sensor.get("aggr", {}).get("max", [])
    hum_list = hum_sensor.get("aggr", {}).get("avg", []) if hum_sensor else []

    if not tmax_list:
        return {"score": None, "ok": False, "reason": "no temperature data"}

    favorable_days = sum(1 for t in tmax_list if 18 <= t <= 30)
    hot_dry_days = sum(
        1 for i, t in enumerate(tmax_list)
        if t > 34 and (hum_list[i] < 40 if i < len(hum_list) else False)
    )

    score = (favorable_days / len(tmax_list)) * 100
    score -= hot_dry_days * 8
    score = max(0, min(100, score))
    avg_tmax = sum(tmax_list) / len(tmax_list)

    print(f"\nSensore temperatura usato: '{temp_sensor.get('name')}' (code={temp_sensor.get('code')})")
    if hum_sensor:
        print(f"Sensore umidità usato: '{hum_sensor.get('name')}' (code={hum_sensor.get('code')})")

    return {
        "score": round(score, 1),
        "avg_tmax": round(avg_tmax, 1),
        "favorable_days": favorable_days,
        "hot_dry_days": hot_dry_days,
        "ok": True,
    }
def save_result(result: dict):
    """
    Aggiunge il risultato della settimana corrente a data/climate_history.json,
    creando la cartella/file se non esistono. Mantiene al massimo le ultime 52 voci.
    """
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    history = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []

    entry = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        **result,
    }
    history.insert(0, entry)
    history = history[:52]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    print(f"Salvato in {OUTPUT_FILE}: {entry}")


if __name__ == "__main__":
    if PUBLIC_KEY == "INSERISCI_QUI":
        print("Configura PUBLIC_KEY, PRIVATE_KEY e STATION_ID prima di eseguire.")
        print("\nPer trovare il tuo station_id, decommenta e lancia list_stations():\n")
        print("  print(list_stations())")
    else:
        weekly = get_last_week_data(STATION_ID)
        import json; print(json.dumps(weekly, indent=2))
        result = extract_climate_score(weekly)
        print("\nRisultato:")
        print(result)
        if result.get("ok"):
            save_result(result)
