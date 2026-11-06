# My Shares

Aktienliste mit einem Empfehlungsindex von 0 bis 10 (0 = stark verkaufen, 5 = halten, 10 = stark kaufen).
Die App läuft komplett in Home Assistant und wird über die Seitenleiste geöffnet.

## Aktie hinzufügen

„Aktie hinzufügen“ → WKN, ISIN oder Börsensymbol eingeben → „Suchen“ → Treffer wählen → „Hinzufügen“.
Die WKN wird über OpenFIGI einem Börsensymbol zugeordnet (z. B. 716460 → `SAP.DE` für Xetra).
Passt die Zuordnung nicht, lässt sich das Symbol in den Details ändern. Die Stückzahl ist optional
und wird für den Depotwert gebraucht.

## Quellen

| Quelle | Zugang | Liefert |
|---|---|---|
| Yahoo Finance | ohne Key | Analystenkonsens (1–5), Kursziel, Kursverlauf |
| Finnhub | kostenloser Key ([finnhub.io](https://finnhub.io/register)) | Empfehlungen pro Monat |
| Financial Modeling Prep | kostenloser Key ([FMP](https://site.financialmodelingprep.com/register)) | Konsens der Einstufungen, Kursziel |
| Kursziel-Potenzial | aus Yahoo / FMP | Abstand Kurs → Kursziel, ±30 % = 0 bzw. 10 |
| Technik | aus dem Kursverlauf | 200-Tage-Linie, Momentum 3 und 12 Monate, RSI |

Jede Quelle wird auf 0–10 umgerechnet. Der Index ist der gewichtete Mittelwert der Quellen, die Daten
liefern. Die Gewichte, die Grenzen für „Kaufen“ und „Verkaufen“ und die Mindestanzahl an Quellen stehen
unter **Einstellungen**. Die kostenlosen Tarife von Finnhub und FMP decken vor allem US-Aktien ab.

## Zeitplan

Unter **Einstellungen → Zeitplan** stellst Du Uhrzeit und Wochentage ein (Standard Mo–Fr 07:30).
„Neu berechnen“ startet einen Lauf sofort. Zwischen zwei Aktien wartet die App 1,5 s, um die Anbieter
nicht zu überlasten.

## Daten

Alles liegt in `/data/my_shares.db` und bleibt bei Updates erhalten.

Der Index fasst Einschätzungen Dritter zusammen und ist keine Anlageberatung.
