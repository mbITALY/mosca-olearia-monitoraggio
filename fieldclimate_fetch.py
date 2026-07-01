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
    """
    Estrae temperatura massima giornaliera e umidità dai dati FieldClimate
    e calcola lo stesso 'climate score' usato nel calcolatore web
    (favorevolezza per la mosca olearia: 18-30°C ottimale, stress da caldo
    secco sopra 34°C con umidità sotto il 40%).

    NOTA: i nomi esatti dei sensor_tag variano da stazione a stazione
    (dipende dal modello/sonde installate). Usa list_stations() o ispeziona
    la risposta di get_last_week_data() per trovare i tag corretti dei tuoi
    sensori di temperatura aria e umidità relativa, poi aggiorna le chiavi
    TEMP_TAG e HUM_TAG qui sotto.
    """
    TEMP_TAG = None  # es. "14_X_X_506" -> trovalo ispezionando daily_data['data']
    HUM_TAG = None   # es. "16_X_X_..." per umidità relativa

    data = daily_data.get("data", {})

    if TEMP_TAG is None or HUM_TAG is None:
        print("\n⚠️  Devi configurare TEMP_TAG e HUM_TAG.")
        print("Sensori disponibili in questa stazione:\n")
        for tag, sensor in data.items():
            print(f"  {tag}: {sensor.get('name')} ({list(sensor.get('aggr', {}).keys())})")
        return {"score": None, "ok": False, "reason": "sensor tags not configured"}

    tmax_list = data[TEMP_TAG]["aggr"]["max"]
    hum_list = data[HUM_TAG]["aggr"]["avg"]

    favorable_days = sum(1 for t in tmax_list if 18 <= t <= 30)
    hot_dry_days = sum(
        1 for t, h in zip(tmax_list, hum_list) if t > 34 and h < 40
    )

    score = (favorable_days / len(tmax_list)) * 100
    score -= hot_dry_days * 8
    score = max(0, min(100, score))

    avg_tmax = sum(tmax_list) / len(tmax_list)

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
