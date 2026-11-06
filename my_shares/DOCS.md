# My Shares

Aktienliste mit einem Empfehlungsindex von 0 bis 10 (0 = stark verkaufen, 5 = halten, 10 = stark kaufen).
Die App läuft komplett in Home Assistant und wird über die Seitenleiste geöffnet.

## Aktie hinzufügen

„Aktie hinzufügen“ → WKN, ISIN oder Börsensymbol eingeben → „Suchen“ → Treffer wählen → „Hinzufügen“.
Die WKN wird über OpenFIGI einem Börsensymbol zugeordnet (z. B. 716460 → `SAP.DE` für Xetra).
Passt die Zuordnung nicht, lässt sich das Symbol in den Details ändern. Die Stückzahl ist optional
und wird für den Depotwert gebraucht.

## Depot und Beobachten

- **Depot:** Aktien, die Du besitzt. Mit Stückzahl zeigt die Übersicht den Depotwert, mit Kaufkurs auch
  Gewinn und Verlust.
- **Beobachten:** Aktien, die Du im Blick behalten willst. Optional mit Wunschkurs; ist er erreicht,
  wird die Zeile markiert.
- Mit dem Knopf in jeder Zeile (oder im Detail-Dialog) verschiebst Du eine Aktie. Ins Depot fragt die App
  Stückzahl und optional den Kaufkurs ab, beim Verschieben zu Beobachten werden beide entfernt.

## Währung

Unter **Einstellungen → Darstellung** wählst Du Euro oder US-Dollar. Alle Beträge (Kurs, Kursziel,
Depotwert, Kauf- und Wunschkurs) werden mit den Referenzkursen der Europäischen Zentralbank umgerechnet.
Die Kurse werden alle 6 Stunden geholt; die EZB veröffentlicht sie werktags gegen 16 Uhr.
Kauf- und Wunschkurs gibst Du in der gewählten Währung ein.

## Quellen

| Quelle | Zugang | Liefert |
|---|---|---|
| Yahoo Finance | ohne Key | Analystenkonsens (1–5), Kursziel, Kursverlauf – alle Aktien |
| Finnhub | kostenloser Key ([finnhub.io](https://finnhub.io/register)) | Empfehlungen pro Monat – nur US-Aktien |
| Nasdaq | ohne Key | Kaufen/Halten/Verkaufen, Kursziel – nur US-Aktien |
| MarketScreener | ohne Key | Konsens, Zahl der Analysten, Kursziel – alle Aktien |
| Kursziel-Potenzial | aus den Analysten-Quellen | Abstand Kurs → mittleres Kursziel, ±30 % = 0 bzw. 10 |
| Technik | aus dem Kursverlauf | 200-Tage-Linie, Momentum 3 und 12 Monate, RSI |

MarketScreener hat keine offizielle Schnittstelle; die App liest die öffentliche Konsens-Seite der Aktie
(ein Abruf pro Aktie und Lauf). Die Seite wird beim ersten Lauf gesucht und am Kurs geprüft. Hat die App die
falsche Aktie erwischt, trägst Du im Detail-Dialog die richtige Adresse ein (jede Sprachversion geht).
Nasdaq hat ebenfalls keine offizielle Schnittstelle; die App nutzt die öffentlichen Analystendaten von nasdaq.com.

Jede Quelle wird auf 0–10 umgerechnet. Der Index ist der gewichtete Mittelwert der Quellen, die Daten
liefern. Die Gewichte, die Grenzen für „Kaufen“ und „Verkaufen“ und die Mindestanzahl an Quellen stehen
unter **Einstellungen**. Finnhub und Nasdaq werden nur für US-Aktien abgefragt, weil ihre kostenlosen Tarife nichts anderes liefern.

## Zeitplan

Unter **Einstellungen → Zeitplan** stellst Du Uhrzeit und Wochentage ein (Standard Mo–Fr 07:30).
„Neu berechnen“ startet einen Lauf sofort. Zwischen zwei Aktien wartet die App 1,5 s, um die Anbieter
nicht zu überlasten.

## Daten

Alles liegt in `/data/my_shares.db` und bleibt bei Updates erhalten.

Der Index fasst Einschätzungen Dritter zusammen und ist keine Anlageberatung.
