"""Hurtig opdatering: henter kun Divisionen og Metal Ligaen og retter dem ind i hockeydata.json."""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import hockey_scraper as h

SLUGS = ["dk-divisionen-2318"]


def main():
    path = Path("hockeydata.json")
    if not path.exists():
        print("hockeydata.json findes ikke endnu. Kør den fulde hentning først.", file=sys.stderr)
        return 1
    today = date.today()
    season = today.year - (today.month < 7)
    label = f"{season}/{season + 1}"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["rækker"]
    byid = {row["id"]: i for i, row in enumerate(rows)}
    changed = 0
    try:
        metal = h.fetch_metal_ligaen(season)["rækker"][0]
        if metal["id"] in byid:
            rows[byid[metal["id"]]] = metal
        else:
            rows.insert(0, metal)
        changed += 1
    except Exception as error:
        print(f"Metal Ligaen mislykkedes: {error}", file=sys.stderr)
    box = h.Boxplay()
    for slug in SLUGS:
        if slug not in byid:
            continue
        try:
            fresh = box.league(slug, rows[byid[slug]]["navn"], label)
            if fresh["kampe"]:
                rows[byid[slug]] = fresh
                changed += 1
        except Exception as error:
            print(f"{slug} mislykkedes: {error}", file=sys.stderr)
    if not changed:
        return 1
    h.mark_source_limits(data)
    data["hentet_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    h.write_json(path, data)
    print(f"{changed} rækker opdateret")
    return 0


if __name__ == "__main__":
    sys.exit(main())
