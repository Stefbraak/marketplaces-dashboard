# Streamlit Excel Viewer

Deze kleine web-app is gebouwd met Streamlit en laat je een Excel-bestand (.xlsx) uploaden, lokaal opslaan in de map `data/` en de inhoud tonen in een tabel.

## Installatie

1. Zorg dat je een recente versie van Python geïnstalleerd hebt.
2. Installeer de benodigde packages (reeds gedaan, maar voor de volledigheid):

```bash
pip install -r requirements.txt
```

## Applicatie starten

Voer in deze map (Desktop) het volgende commando uit:

```bash
streamlit run app.py
```

Daarna opent er automatisch een browservenster (of je kunt naar de URL in de terminal gaan, meestal `http://localhost:8501`).

## Stap 3: De app starten en verfijnen (in Cursor)

Als Cursor klaar is met de code, start de app één keer op om te zien of alles werkt.

1. Open de terminal onderin Cursor (als die er niet is: `Ctrl + ~`).
2. Typ:

```bash
streamlit run app.py
```

Je browser opent nu je eigen dashboard.

## Gebruik

- Gebruik de **sidebar** om een `.xlsx`-bestand te uploaden.
- Het bestand wordt opgeslagen in de lokale map `data/`.
- De **hoofdpagina** toont de inhoud van het laatst geüploade (of anders het meest recente) Excel-bestand in een tabel.

