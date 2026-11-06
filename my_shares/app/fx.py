"""Währungsumrechnung mit den Referenzkursen der Europäischen Zentralbank (pro 1 EUR)."""
import re

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

# Börsen notieren manche Werte in Untereinheiten (London in Pence usw.)
SUBUNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}


def parse_ecb(xml):
    date = re.search(r"time=['\"](\d{4}-\d{2}-\d{2})['\"]", xml)
    rates = {m.group(1): float(m.group(2))
             for m in re.finditer(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([0-9.]+)['\"]", xml)}
    if not date or "USD" not in rates:
        raise ValueError("EZB-Kurse unvollständig")
    rates["EUR"] = 1.0
    return {"date": date.group(1), "rates": rates}


def convert(amount, cur, target, fx):
    """Rechnet amount von cur in target um. None, wenn kein Kurs bekannt ist."""
    if amount is None:
        return None
    cur = cur or target
    if cur in SUBUNITS:
        cur, div = SUBUNITS[cur]
        amount = amount / div
    if cur == target:
        return amount
    rates = (fx or {}).get("rates") or {}
    if cur not in rates or target not in rates:
        return None
    return amount / rates[cur] * rates[target]
