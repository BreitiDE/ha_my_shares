# Changelog

## 1.3.0

- Neuer Menüpunkt **Log**: zeigt jeden Abruf (Yahoo, Finnhub, Nasdaq, MarketScreener, EZB, OpenFIGI) mit Aktie,
  Ergebnis, Adresse, HTTP-Status und Dauer, dazu Beginn und Ende jedes Laufs und eigene Aktionen
  (Hinzufügen, Verschieben, Entfernen, Einstellungen)
- Suche über Meldung, Aktie und Adresse, Filter nach Stufe, Quelle und Aktie, Blättern, Live-Aktualisierung
- Log leeren: alles, älter als 7 Tage oder nur Info-Einträge; Herunterladen als Textdatei (mit aktuellem Filter)
- Einträge älter als 30 Tage werden automatisch gelöscht; API-Keys erscheinen nie im Log
- Rote Zahl am Menüpunkt zeigt die Fehler der letzten 24 Stunden

## 1.2.1

- Alpha Vantage durch **Nasdaq** ersetzt: Alpha Vantage gibt Analystendaten nur noch mit Premium-Key heraus
  („This is a premium endpoint“). Nasdaq liefert Kaufen/Halten/Verkaufen und Kursziel für US-Aktien ohne Key
- Einstellung für den Alpha-Vantage-Key entfällt

## 1.2.0

- Neue Quelle **MarketScreener**: Analystenkonsens, Zahl der Analysten und Kursziel für alle Aktien, auch Deutschland und Europa.
  Die Seite der Aktie wird automatisch gesucht und am Kurs geprüft; im Detail-Dialog lässt sie sich korrigieren
- Neue Quelle **Alpha Vantage** (kostenloser Key) statt FMP: Einstufungen und Kursziel für US-Aktien.
  Höchstens 25 Abrufe pro Tag, 13 s Abstand, Ergebnisse werden 20 h zwischengespeichert
- FMP entfernt: der kostenlose Tarif liefert nur rund 87 große US-Werte
- Finnhub und Alpha Vantage werden nur noch für US-Aktien abgefragt; bei anderen Aktien steht „nur für US-Aktien“ statt eines Fehlers
- Kursziel-Potenzial ist jetzt der Mittelwert der Kursziele aller Analysten-Quellen
- Damit 6 Quellen: Yahoo, Finnhub, Alpha Vantage, MarketScreener, Kursziel-Potenzial, Technik

## 1.1.0

- Neue Liste „Beobachten“ für Aktien, die nicht im Depot sind; Bewertung wie im Depot
- Wunschkurs pro beobachteter Aktie; erreichte Wunschkurse werden markiert
- Aktien zwischen Depot und Beobachten verschieben (ins Depot mit Stückzahl und optionalem Kaufkurs)
- Kaufkurs im Depot: Gewinn/Verlust pro Aktie und für das ganze Depot
- Anzeigewährung Euro oder US-Dollar; alle Beträge werden mit den EZB-Referenzkursen umgerechnet
- Depotwert als eine Summe in der gewählten Währung, Gesamtbewertung nach Depotwert gewichtet
- Menü: „Aktien“ heißt jetzt „Depot“; bestehende Aktien landen beim Update automatisch im Depot

## 1.0.0

- Erste Version
- Aktien per WKN, ISIN oder Symbol hinzufügen (OpenFIGI, ersatzweise Yahoo-Suche)
- Empfehlungsindex 0–10 aus Yahoo-Analysten, Finnhub, FMP, Kursziel-Potenzial und eigener Technik
- Übersicht mit Anzahl, Gesamtbewertung, Depotwert und letzter Aktualisierung
- Detailansicht je Aktie mit allen Quellen und Verlauf des Index
- Zeitplan (Uhrzeit und Wochentage), „Neu berechnen“ von Hand
- Oberfläche nach Styleguide, hell und dunkel, Deutsch und Englisch
