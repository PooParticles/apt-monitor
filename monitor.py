import requests
from bs4 import BeautifulSoup
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ---------------------------------------------------------------------------
# CONFIGURATION — edit these values in GitHub Secrets (see README)
# ---------------------------------------------------------------------------
EMAIL_FROM   = os.environ.get("EMAIL_FROM")    # your Gmail address
EMAIL_TO     = os.environ.get("EMAIL_TO")      # where to send alerts
EMAIL_PASS   = os.environ.get("EMAIL_PASS")    # Gmail App Password
PRICES_FILE  = "last_prices.json"

# ---------------------------------------------------------------------------
# LISTINGS TO MONITOR
# Each entry: { id, name, url, parser_fn }
# parser_fn receives BeautifulSoup and returns a dict of { label: price_str }
# Returns None if the site blocked the request or parsing failed.
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch(url):
    """Fetch a URL. Returns BeautifulSoup or None on failure."""
    try:
        # Use verify=False specifically for 1914 Main to bypass the SSL error
        should_verify = False if "1914main.com" in url else True
        
        r = requests.get(url, headers=HEADERS, timeout=15, verify=should_verify)
        
        if r.status_code == 200:
            return BeautifulSoup(r.text, "html.parser")
        else:
            print(f"  blocked/error {r.status_code}: {url}")
            return None
    except Exception as e:
        print(f"  fetch error: {e}")
        return None


# --- Piper Lofts -----------------------------------------------------------
def parse_piper(soup):
    """
    Piper renders floorplan cards. Each card has a price like 'Starting at $1,816'
    and a plan name like 'A1', 'A2', etc.
    Returns { "A2": "$1,816", ... }
    """
    results = {}
    if not soup:
        return results
    cards = soup.find_all(class_=lambda c: c and "floorplan" in c.lower())
    for card in cards:
        name_el = card.find(["h2", "h3", "h4"])
        price_el = card.find(string=lambda t: t and "Starting at" in t)
        if name_el and price_el:
            name = name_el.get_text(strip=True)
            price = price_el.strip().replace("Starting at ", "")
            results[name] = price
    # fallback: scan all text for "Starting at $X"
    if not results:
        import re
        text = soup.get_text(" ", strip=True)
        matches = re.findall(r"(A\d|B\d|P\d)\s.*?Starting at (\$[\d,]+)", text)
        for plan, price in matches:
            results[plan] = price
    return results or None


# --- Piper individual floorplan pages (more reliable) ----------------------
def parse_piper_a2(soup):
    if not soup:
        return None
    import re
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Starting at (\$[\d,]+)", text)
    if match:
        return {"A2 starting": match.group(1)}
    # Look for unit-level prices like "$1,816"
    prices = re.findall(r"\$(\d{1,2},\d{3})", text)
    prices = [p for p in prices if 1000 <= int(p.replace(",","")) <= 5000]
    if prices:
        return {"A2 prices seen": ", ".join(["$"+p for p in sorted(set(prices))])}
    return None


# --- 1914 Main -------------------------------------------------------------
def parse_1914main(soup):
    """
    1914 Main lists units with prices like '$1,460 / MONTH' and unit numbers.
    """
    if not soup:
        return None
    import re
    results = {}
    text = soup.get_text(" ", strip=True)
    # Find unit + price pairs
    unit_matches = re.findall(r"Unit\s+(\d+)[^$]*(\$[\d,]+)\s*/\s*MONTH", text, re.IGNORECASE)
    for unit, price in unit_matches:
        results[f"Unit {unit}"] = price
    # fallback: find all prices in range
    if not results:
        prices = re.findall(r"\$(\d{1,2},\d{3})\s*/\s*MONTH", text, re.IGNORECASE)
        prices = [p for p in prices if 1000 <= int(p.replace(",","")) <= 5000]
        if prices:
            results["prices seen"] = ", ".join(["$"+p for p in sorted(set(prices))])
    return results or None


# --- Reverb KC (likely blocked, try anyway) --------------------------------
def parse_reverb(soup):
    if not soup:
        return None
    import re
    text = soup.get_text(" ", strip=True)
    prices = re.findall(r"\$(\d{1,2},\d{3})", text)
    prices = [p for p in prices if 1000 <= int(p.replace(",","")) <= 4000]
    if prices:
        return {"prices seen": ", ".join(["$"+p for p in sorted(set(prices))])}
    return None


# --- Arterra KC (likely blocked, try anyway) -------------------------------
def parse_arterra(soup):
    if not soup:
        return None
    import re
    text = soup.get_text(" ", strip=True)
    matches = re.findall(r"(S\d|A\d)\s.*?Starting at (\$[\d,]+)", text)
    if matches:
        return {plan: price for plan, price in matches}
    prices = re.findall(r"\$(\d{1,2},\d{3})", text)
    prices = [p for p in prices if 1000 <= int(p.replace(",","")) <= 4000]
    if prices:
        return {"prices seen": ", ".join(["$"+p for p in sorted(set(prices))])}
    return None


# ---------------------------------------------------------------------------
# WATCH LIST
# ---------------------------------------------------------------------------
WATCHES = [
    {
        "id": "piper_a2",
        "name": "Piper Lofts — A2 floorplan",
        "url": "https://piperlofts.com/floorplans/a2/",
        "parser": parse_piper_a2,
    },
    {
        "id": "piper_a1",
        "name": "Piper Lofts — A1 floorplan",
        "url": "https://piperlofts.com/floorplans/a1/",
        "parser": parse_piper_a2,  # same structure
    },
    {
        "id": "main_availability",
        "name": "1914 Main — availability",
        "url": "https://1914main.com/availability/",
        "parser": parse_1914main,
    },
    {
        "id": "reverb_b31",
        "name": "Reverb KC — B3.1 (1BR 992sf)",
        "url": "https://www.reverbkc.com/floorplans/b3.1",
        "parser": parse_reverb,
    },
    {
        "id": "reverb_b42",
        "name": "Reverb KC — B4.2 (1BR 1113sf)",
        "url": "https://www.reverbkc.com/floorplans/b4.2",
        "parser": parse_reverb,
    },
    {
        "id": "arterra_studios",
        "name": "Arterra KC — studios",
        "url": "https://www.arterrakc.com/floorplans?Beds=0",
        "parser": parse_arterra,
    },
]


# ---------------------------------------------------------------------------
# CORE LOGIC
# ---------------------------------------------------------------------------
def load_last_prices():
    if os.path.exists(PRICES_FILE):
        with open(PRICES_FILE) as f:
            return json.load(f)
    return {}


def save_prices(prices):
    with open(PRICES_FILE, "w") as f:
        json.dump(prices, f, indent=2)


def check_all():
    last = load_last_prices()
    current = {}
    changes = []

    for watch in WATCHES:
        print(f"\nChecking: {watch['name']}")
        soup = fetch(watch["url"])
        result = watch["parser"](soup)

        if result is None:
            print(f"  → no data (blocked or parse failed)")
            # carry forward last known prices so we don't false-alert
            if watch["id"] in last:
                current[watch["id"]] = last[watch["id"]]
            continue

        print(f"  → found: {result}")
        current[watch["id"]] = {
            "name": watch["name"],
            "url": watch["url"],
            "prices": result,
            "checked": datetime.now().isoformat(),
        }

        # compare to last
        if watch["id"] in last:
            old_prices = last[watch["id"]].get("prices", {})
            new_prices = result
            for label, new_val in new_prices.items():
                old_val = old_prices.get(label)
                if old_val and old_val != new_val:
                    changes.append({
                        "building": watch["name"],
                        "url": watch["url"],
                        "label": label,
                        "old": old_val,
                        "new": new_val,
                    })
            # check for newly appeared labels
            for label, new_val in new_prices.items():
                if label not in old_prices:
                    changes.append({
                        "building": watch["name"],
                        "url": watch["url"],
                        "label": label,
                        "old": "(new)",
                        "new": new_val,
                    })
        else:
            print(f"  → first check, baseline saved")

    save_prices(current)
    return changes


def send_email(changes):
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASS]):
        print("Email not configured — skipping alert (set EMAIL_FROM, EMAIL_TO, EMAIL_PASS secrets)")
        return

    subject = f"Apartment price change alert — {len(changes)} change(s) detected"

    lines = [f"Price changes detected on {datetime.now().strftime('%b %d at %I:%M %p')}:\n"]
    for c in changes:
        arrow = "↑" if c["new"] > c["old"] else "↓"
        lines.append(f"{arrow} {c['building']}")
        lines.append(f"   {c['label']}: {c['old']} → {c['new']}")
        lines.append(f"   {c['url']}\n")

    body = "\n".join(lines)
    print("\n" + body)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print("Alert email sent.")
    except Exception as e:
        print(f"Failed to send email: {e}")


def main():
    print(f"=== Apartment Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    changes = check_all()

    if changes:
        print(f"\n{len(changes)} price change(s) found!")
        send_email(changes)
    else:
        print("\nNo changes detected.")


if __name__ == "__main__":
    main()
