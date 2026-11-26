"""Hent offentlige kampe og stillinger for danske ishockeyrækker."""

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser


# Metal Ligaens egne kamp- og tabelwidgets læser fra denne offentlige datakilde.
METAL_FEED = "https://s3.dualstack.eu-west-1.amazonaws.com/den.hokejovyzapis.cz"
METAL_SITE = "https://metalligaen.dk/"
STATUS = {"BEFORE_MATCH": "planlagt", "LIVE": "live", "AFTER_MATCH": "afsluttet"}
BOXPLAY = "https://boxplay.app"
CATALOG = BOXPLAY + "/da/competitions?country=DK"
USER_AGENT = "HoldappScraper/1.0"
YOUNGEST = re.compile(r"^dk-u(?:6|7|8|9|10|11)-")
GAME_DATE = re.compile(r"-(\d{4}-\d{2}-\d{2})-")
SCORE = re.compile(r"(\d+)\s*[–-]\s*(\d+)")
TIME = re.compile(r"\b([01]?\d|2[0-3])[.:]([0-5]\d)\b")


class Element:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.classes = (self.attrs.get("class") or "").split()
        self.children = []

    def find(self, *, tag=None, css=None, prefix=None):
        for child in self.children:
            if isinstance(child, Element):
                if ((tag is None or child.tag == tag)
                        and (css is None or css in child.classes)
                        and (prefix is None or (child.attrs.get("href") or "").startswith(prefix))):
                    yield child
                yield from child.find(tag=tag, css=css, prefix=prefix)

    def text(self):
        pieces = (part if isinstance(part, str) else part.text() for part in self.children)
        return " ".join("".join(pieces).split())


class Page(HTMLParser):
    VOID = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self, html):
        super().__init__()
        self.root = Element("document")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs)
        self.stack[-1].children.append(element)
        if tag not in self.VOID:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(data)


class Boxplay:
    def __init__(self):
        self.last_request = 0
        self.delay = 1
        robots_url = BOXPLAY + "/robots.txt"
        self.rules = RobotFileParser(robots_url)
        self.rules.parse(self.read(robots_url).splitlines())
        rate = self.rules.request_rate(USER_AGENT)
        self.delay = max(1, self.rules.crawl_delay(USER_AGENT) or 0, rate.seconds / rate.requests if rate else 0)

    def read(self, url):
        remaining = self.delay - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        request = Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
        self.last_request = time.monotonic()
        with urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8-sig")

    def page(self, url):
        if not self.rules.can_fetch(USER_AGENT, url):
            raise ValueError("robots.txt tillader ikke automatisk hentning")
        return Page(self.read(url)).root

    def catalog(self):
        entries = {}
        for link in self.page(CATALOG).find(tag="a", prefix="/da/competition/dk-"):
            href = link.attrs["href"]
            slug = urlsplit(href).path.rsplit("/", 1)[-1]
            if re.fullmatch(r"dk-[a-z0-9-]+-\d+", slug) and "testturnering" not in slug:
                labels = [item.text() for item in link.find(css="truncate")]
                entries[slug] = labels[-1].split(" · ")[-1] if labels else slug
        if not entries:
            raise ValueError("Ingen danske rækker fundet i BOXPLAYs katalog")
        return entries

    def league(self, slug, name, season):
        url = f"{BOXPLAY}/da/competition/{slug}"
        matches = {}
        tables = []
        result = {
            "id": slug, "navn": name, "sæson": season, "tidszone": "Europe/Copenhagen",
            "kilde": url, "kampe": [], "stilling": tables, "kildefejl": [],
        }
        youngest = bool(YOUNGEST.match(slug))
        for tab in ("games", "table"):
            if youngest and tab == "table":
                continue
            stages = {None: None}
            while stages:
                stage, stage_name = next(iter(stages.items()))
                del stages[stage]
                query = {"tab": tab}
                if stage is not None:
                    query["stage"] = stage
                try:
                    page = self.page(f"{url}?{urlencode(query)}")
                    header = next(page.find(tag="h1"), None)
                    if header is None:
                        raise ValueError("Rækkens overskrift mangler")
                    listed_season = next((p.text() for p in page.find(tag="p")
                                          if re.fullmatch(r"\d{4}/\d{4}", p.text())), None)
                    if listed_season and listed_season != season:
                        raise ValueError(f"Kilden viser sæson {listed_season}")
                    if stage is None:
                        for link in page.find(tag="a", prefix=f"/da/competition/{slug}?"):
                            stage_key = parse_qs(urlsplit(link.attrs["href"]).query).get("stage")
                            if stage_key and link.attrs.get("aria-current") != "page":
                                stages[stage_key[0]] = link.text()
                    main = next(page.find(tag="main"))
                    if tab == "games":
                        for card in main.find(tag="a", prefix="/da/game/dk-"):
                            try:
                                match = read_match_card(card, youngest)
                                matches[match["id"]] = match
                            except ValueError as error:
                                result["kildefejl"].append(str(error))
                    else:
                        for table in main.find(tag="table"):
                            standings = read_standings(table)
                            if standings and not any(group["hold"] == standings for group in tables):
                                tables.append({"gruppe": stage_name, "hold": standings})
                except HTTPError as error:
                    if error.code == 429:
                        raise
                    result["kildefejl"].append(f"{tab}: HTTP {error.code}")
                except (OSError, ValueError, StopIteration) as error:
                    result["kildefejl"].append(f"{tab}: {error or 'Siden mangler kamp- eller tabelvisningen'}")
        result["kampe"] = sorted(matches.values(), key=lambda match: (match["dato"] or "9999", match["kampstart_lokal"] or ""))
        result["kampdækning"] = "fundet" if matches else "ingen_kampe_fundet"
        result["tabeldækning"] = "fundet" if tables else ("uden_stilling" if youngest else "ingen_tabel_fundet")
        return result


def read_match_card(card, youngest):
    href = card.attrs["href"]
    match_id = href.rsplit("/", 1)[-1]
    sides = [team.text() for team in card.find(css="text-xs") if "truncate" in team.classes]
    center = next(card.find(css="min-w-[88px]"), None)
    if len(sides) != 2 or center is None:
        raise ValueError(f"Ukendt kampformat for {match_id}")
    labels = [child.text() for child in center.children if isinstance(child, Element)]
    if len(labels) < 2:
        raise ValueError(f"Kampstatus mangler for {match_id}")
    title, detail = labels[:2]
    is_live = any(label.text() == "Live" for label in card.find(tag="span"))
    date_match = GAME_DATE.search(match_id)
    local_date = date_match[1] if date_match else None
    kickoff = TIME.fullmatch(title)
    if not kickoff:
        for text in card.find(css="tracking-normal"):
            kickoff = TIME.search(text.text())
            if kickoff:
                break
    status = "ukendt"
    if is_live:
        status = "live"
    elif detail == "Slut":
        status = "afsluttet"
    elif kickoff and not SCORE.fullmatch(title):
        status = "planlagt"
    if youngest and status in {"live", "afsluttet"}:
        status = "ukendt"
    score = SCORE.fullmatch(title) if status in {"live", "afsluttet"} else None
    if status == "live" and local_date and local_date < date.today().isoformat():
        status = "forældet_live"
    return {
        "id": match_id, "dato": local_date, "kilde": urljoin(BOXPLAY, href),
        "kampstart_lokal": f"{local_date}T{int(kickoff[1]):02}:{kickoff[2]}" if local_date and kickoff else None,
        "hjemme": sides[0], "ude": sides[1], "status": status,
        "mål_hjemme": int(score[1]) if score else None,
        "mål_ude": int(score[2]) if score else None,
        "periodekode": detail if status in {"live", "forældet_live"} else None,
    }


def read_standings(table):
    keys = {"GP": "kampe", "V": "sejre", "F": "nederlag", "P": "point", "+/−": "målforskel",
            "MF": "mål_for", "MM": "mål_imod"}
    headers = [cell.text() for cell in table.find(tag="th")]
    if not headers or headers[0] not in {"Lag", "Hold"} or "P" not in headers:
        return []
    teams = []
    for row in table.find(tag="tr"):
        cells = list(row.find(tag="td"))
        if len(cells) != len(headers):
            continue
        team = next(cells[0].find(tag="a", prefix="/da/team/"), None)
        rank = re.match(r"(\d+)\s*\.", cells[0].text())
        if team is None or rank is None:
            continue
        stats = {keys[header]: int(cell.text().replace("−", "-"))
                 for header, cell in zip(headers[1:], cells[1:])
                 if header in keys and re.fullmatch(r"[−-]?\d+", cell.text())}
        teams.append({"placering": int(rank[1]), "hold": team.text(), **stats})
    return teams


def mark_source_limits(data):
    for league in data["rækker"]:
        if league["kilde"].startswith(BOXPLAY) and len(league["kampe"]) >= 200:
            league["kampdækning"] = "muligvis_afkortet"


def read_feed(url):
    headers = {"User-Agent": "HoldappScraper/1.0", "Accept": "application/json", "Cache-Control": "no-cache"}
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        last_modified = response.headers.get("Last-Modified")
        updated = parsedate_to_datetime(last_modified).isoformat(timespec="seconds") if last_modified else None
        return json.load(response), updated


def normalize_match(match):
    status_from_source = match["status"]
    status = STATUS.get(status_from_source, "ukendt")
    starts = match["start_date"]
    if starts and not starts.startswith("0000-"):
        starts = datetime.fromisoformat(starts).isoformat(timespec="minutes")
    else:
        starts = None
    score = {}
    if status in {"live", "afsluttet"}:
        score = match["results"]["score"]["final"]
    return {
        "id": str(match["id"]),
        "fase": match["league_part_name"],
        "kampstart_lokal": starts,
        "hjemme": match["home"]["name"],
        "ude": match["guest"]["name"],
        "status": status,
        "kildestatus": status_from_source,
        "mål_hjemme": score.get("score_home"),
        "mål_ude": score.get("score_guest"),
        "periodekode": match.get("actual_time_alias") if status == "live" else None,
    }


def normalize_standings(groups):
    return [
        {
            "gruppe": group["groupName"] or None,
            "hold": [
                {
                    "placering": club["position"],
                    "hold": club["name"],
                    "kampe": club["games"],
                    "mål_for": club["score1"],
                    "mål_imod": club["score2"],
                    "point": club["points"],
                }
                for club in group["clubs"]
            ],
        }
        for group in groups
    ]


def fetch_metal_ligaen(season):
    fixtures_url = f"{METAL_FEED}/league-matches/{season}/1.json"
    standings_url = f"{METAL_FEED}/table/{season}/1.json"
    fixture_data, fixtures_updated = read_feed(fixtures_url)
    fixtures = fixture_data["matches"]
    time.sleep(1)
    standings_data, standings_updated = read_feed(standings_url)
    standings = normalize_standings(standings_data)
    if not fixtures or not any(group["hold"] for group in standings):
        raise ValueError("Kildens kampe eller stilling er tom")
    matches = sorted(map(normalize_match, fixtures), key=lambda match: match["kampstart_lokal"] or "9999")
    return {
        "hentet_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rækker": [{
            "id": "metal-ligaen",
            "navn": "Metal Ligaen",
            "sæson": f"{season}/{season + 1}",
            "tidszone": "Europe/Copenhagen",
            "kilde": METAL_SITE,
            "kampkilde": fixtures_url,
            "stillingskilde": standings_url,
            "kampkilde_opdateret_utc": fixtures_updated,
            "stillingskilde_opdateret_utc": standings_updated,
            "kampe": matches,
            "stilling": standings,
        }],
    }


def fetch_all(season, include_catalog):
    data = fetch_metal_ligaen(season)
    if not include_catalog:
        return data
    boxplay = Boxplay()
    catalog = boxplay.catalog()
    data["rækkekatalog"] = CATALOG
    label = f"{season}/{season + 1}"
    for slug, name in catalog.items():
        if re.fullmatch(r"dk-metal-ligaen-\d+", slug):
            continue
        league = boxplay.league(slug, name, label)
        data["rækker"].append(league)
    data["hentet_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return data


def write_json(path, data):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def main():
    today = date.today()
    default_season = today.year - (today.month < 7)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=default_season, help="Sæsonens startår, fx 2026")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("hockeydata.json"))
    parser.add_argument("--watch", action="store_true", help="Vent ét minut mellem hver scanning, indtil du trykker Ctrl+C")
    args = parser.parse_args()
    while True:
        try:
            data = fetch_all(args.season, include_catalog=args.season == default_season)
            mark_source_limits(data)
            write_json(args.output, data)
            leagues = data["rækker"]
            matches = [match for league in leagues for match in league["kampe"]]
            live = sum(match["status"] == "live" for match in matches)
            tables = sum(bool(league["stilling"]) for league in leagues)
            print(f"{len(leagues)} turneringer · {len(matches)} kampe · {live} live · {tables} med stilling · {args.output}", flush=True)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Hentning mislykkedes. Seneste gemte fil er bevaret. {error}", file=sys.stderr, flush=True)
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
