"""同步最近两周 MLB 已完成比赛到 docs/ 并生成 GitHub Pages 站点。"""

from __future__ import annotations

import json
import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from scripts import render_play_by_play

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DOCS_DIR = ROOT / "docs"
GAMES_DIR = DOCS_DIR / "games"
TEMPLATES_DIR = ROOT / "templates"
PLAY_TEMPLATE = TEMPLATES_DIR / "play_by_play_page.html.j2"
INDEX_TEMPLATE = TEMPLATES_DIR / "index_page.html.j2"
MANIFEST_PATH = DOCS_DIR / "games.json"
NOJEKYLL_PATH = DOCS_DIR / ".nojekyll"
LOOKBACK_DAYS = 14
MLB_TIMEZONE = ZoneInfo("America/Los_Angeles") if ZoneInfo else timezone(timedelta(hours=-7), name="America/Los_Angeles")
API_BASE = "https://statsapi.mlb.com/api/v1"
LIVE_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"


@dataclass
class GameRecord:
    game_pk: int
    official_date: str
    game_datetime: str
    away_label: str
    home_label: str
    away_name: str
    home_name: str
    away_score: int
    home_score: int
    venue_name: str
    away_starting_pitcher: str
    home_starting_pitcher: str
    html_path: str

    @property
    def title(self) -> str:
        return f"{self.away_label} @ {self.home_label}"

    @property
    def final_score(self) -> str:
        return f"{self.away_label} {self.away_score} - {self.home_score} {self.home_label}"


def load_existing_manifest() -> dict[int, GameRecord]:
    """读取已有比赛索引，作为增量同步依据。"""
    if not MANIFEST_PATH.exists():
        return {}

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    records: dict[int, GameRecord] = {}
    for item in payload:
        try:
            record = GameRecord(
                game_pk=int(item["game_pk"]),
                official_date=str(item["official_date"]),
                game_datetime=str(item["game_datetime"]),
                away_label=str(item["away_label"]),
                home_label=str(item["home_label"]),
                away_name=str(item["away_name"]),
                home_name=str(item["home_name"]),
                away_score=int(item["away_score"]),
                home_score=int(item["home_score"]),
                venue_name=str(item.get("venue_name", "")),
                away_starting_pitcher=str(item.get("away_starting_pitcher", "")),
                home_starting_pitcher=str(item.get("home_starting_pitcher", "")),
                html_path=str(item["html_path"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        records[record.game_pk] = record
    return records


def ensure_dirs() -> None:
    """确保输出目录存在。"""
    DOCS_DIR.mkdir(exist_ok=True)
    GAMES_DIR.mkdir(exist_ok=True)
    NOJEKYLL_PATH.write_text("", encoding="utf-8")


def get_today_in_mlb_timezone() -> date:
    """获取洛杉矶当天日期。"""
    return datetime.now(MLB_TIMEZONE).date()


def daterange(start_date: date, end_date: date) -> list[date]:
    """生成日期范围。"""
    days = (end_date - start_date).days
    return [start_date + timedelta(days=offset) for offset in range(days + 1)]


def fetch_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """请求 MLB 官方接口并返回 JSON。"""
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_schedule_for_date(target_date: date) -> list[dict[str, Any]]:
    """获取某一天的赛程。"""
    payload = fetch_json(
        f"{API_BASE}/schedule",
        params={
            "sportId": 1,
            "date": target_date.isoformat(),
            "hydrate": "linescore,team",
        },
    )
    dates = payload.get("dates", [])
    if not dates:
        return []
    return dates[0].get("games", [])


def is_completed_game(game: dict[str, Any]) -> bool:
    """判断比赛是否已经结束。"""
    status = game.get("status", {})
    abstract_state = status.get("abstractGameState")
    detailed_state = status.get("detailedState")
    return abstract_state == "Final" or detailed_state in {"Final", "Completed Early", "Game Over"}


def get_team_label(team_data: dict[str, Any]) -> str:
    """获取球队展示缩写。"""
    team = team_data.get("team", {})
    team_code = team.get("teamCode")
    if team_code:
        return str(team_code).upper()
    abbreviation = team.get("abbreviation")
    if abbreviation:
        return str(abbreviation).upper()
    return team.get("name", "TEAM")


def build_play_payload(live_feed: dict[str, Any]) -> dict[str, Any]:
    """把 live feed 裁成渲染器需要的 play-by-play 结构。"""
    plays = live_feed.get("liveData", {}).get("plays", {})
    return {
        "copyright": live_feed.get("copyright", ""),
        "allPlays": plays.get("allPlays", []),
        "currentPlay": plays.get("currentPlay", {}),
        "scoringPlays": plays.get("scoringPlays", []),
        "playsByInning": plays.get("playsByInning", []),
    }


def slugify_game(official_date: str, away_label: str, home_label: str, game_pk: int) -> str:
    """生成比赛文件名。"""
    return f"{official_date}-{away_label.lower()}-at-{home_label.lower()}-{game_pk}"


def build_record_from_feed(schedule_game: dict[str, Any], live_feed: dict[str, Any]) -> GameRecord:
    """从 schedule 和 live feed 构建比赛记录。"""
    game_data = live_feed.get("gameData", {})
    teams = game_data.get("teams", {})
    away_team = teams.get("away", {})
    home_team = teams.get("home", {})
    linescore = live_feed.get("liveData", {}).get("linescore", {}).get("teams", {})
    away_line = linescore.get("away", {})
    home_line = linescore.get("home", {})

    away_label = str(away_team.get("abbreviation") or away_team.get("teamCode") or get_team_label(schedule_game.get("teams", {}).get("away", {}))).upper()
    home_label = str(home_team.get("abbreviation") or home_team.get("teamCode") or get_team_label(schedule_game.get("teams", {}).get("home", {}))).upper()
    official_date = game_data.get("datetime", {}).get("officialDate") or schedule_game.get("officialDate")
    game_datetime = game_data.get("datetime", {}).get("dateTime") or schedule_game.get("gameDate")
    game_pk = int(game_data.get("game", {}).get("pk") or schedule_game.get("gamePk"))
    slug = slugify_game(official_date, away_label, home_label, game_pk)
    play_payload = build_play_payload(live_feed)
    starting_pitchers = render_play_by_play.extract_starting_pitchers(play_payload, away_label, home_label)

    return GameRecord(
        game_pk=game_pk,
        official_date=official_date,
        game_datetime=game_datetime,
        away_label=away_label,
        home_label=home_label,
        away_name=away_team.get("name") or schedule_game.get("teams", {}).get("away", {}).get("team", {}).get("name", away_label),
        home_name=home_team.get("name") or schedule_game.get("teams", {}).get("home", {}).get("team", {}).get("name", home_label),
        away_score=int(away_line.get("runs", schedule_game.get("teams", {}).get("away", {}).get("score", 0) or 0)),
        home_score=int(home_line.get("runs", schedule_game.get("teams", {}).get("home", {}).get("score", 0) or 0)),
        venue_name=game_data.get("venue", {}).get("name") or "",
        away_starting_pitcher=starting_pitchers["away"].get("display_name", ""),
        home_starting_pitcher=starting_pitchers["home"].get("display_name", ""),
        html_path=f"games/{slug}.html",
    )


def write_game_files(record: GameRecord, play_payload: dict[str, Any], boxscore_payload: dict[str, Any]) -> None:
    """写出单场比赛 HTML。"""
    html_output_path = DOCS_DIR / record.html_path
    html_output_path.parent.mkdir(parents=True, exist_ok=True)

    render_play_by_play.render_html_from_payload(
        payload=play_payload,
        template_path=PLAY_TEMPLATE,
        output_path=html_output_path,
        away_label=record.away_label,
        home_label=record.home_label,
        page_title=f"Play-by-Play {record.game_pk}",
        boxscore_payload=boxscore_payload,
    )


def render_index(records: list[GameRecord]) -> None:
    """生成导航首页。"""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template(INDEX_TEMPLATE.name)
    html = template.render(records=records, generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


def cleanup_untracked_outputs(records: list[GameRecord]) -> None:
    """删除不在最近两周范围内的旧文件。"""
    keep_paths = {
        DOCS_DIR / "index.html",
        MANIFEST_PATH,
        NOJEKYLL_PATH,
    }
    for record in records:
        keep_paths.add(DOCS_DIR / record.html_path)

    for path in DOCS_DIR.rglob("*"):
        if path.is_dir():
            continue
        if path not in keep_paths:
            path.unlink()


def save_manifest(records: list[GameRecord]) -> None:
    """保存比赛列表。"""
    payload = [
        {
            "game_pk": record.game_pk,
            "official_date": record.official_date,
            "game_datetime": record.game_datetime,
            "away_label": record.away_label,
            "home_label": record.home_label,
            "away_name": record.away_name,
            "home_name": record.home_name,
            "away_score": record.away_score,
            "home_score": record.home_score,
            "venue_name": record.venue_name,
            "away_starting_pitcher": record.away_starting_pitcher,
            "home_starting_pitcher": record.home_starting_pitcher,
            "html_path": record.html_path,
            "title": record.title,
            "final_score": record.final_score,
        }
        for record in records
    ]
    MANIFEST_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_recent_completed_games(force_refresh: bool = False) -> list[GameRecord]:
    """抓取最近两周已完成比赛并写出站点文件。"""
    today = get_today_in_mlb_timezone()
    start_date = today - timedelta(days=LOOKBACK_DAYS - 1)
    existing_records = {} if force_refresh else load_existing_manifest()
    records: list[GameRecord] = []

    for target_date in daterange(start_date, today):
        games = fetch_schedule_for_date(target_date)
        completed_games = [game for game in games if is_completed_game(game)]
        print(f"[sync] {target_date.isoformat()} total={len(games)} completed={len(completed_games)}")

        for game in completed_games:
            game_pk = int(game.get("gamePk"))
            existing_record = existing_records.get(game_pk)
            if existing_record and (DOCS_DIR / existing_record.html_path).exists():
                records.append(existing_record)
                print(f"[skip] {existing_record.official_date} {existing_record.away_label}@{existing_record.home_label} gamePk={existing_record.game_pk}")
                continue

            live_feed = fetch_json(LIVE_FEED_URL.format(game_pk=game_pk))
            play_payload = build_play_payload(live_feed)
            if not play_payload.get("allPlays"):
                continue

            record = build_record_from_feed(game, live_feed)
            write_game_files(record, play_payload, live_feed.get("liveData", {}).get("boxscore", {}))
            records.append(record)
            action = "refresh" if force_refresh else "game"
            print(f"[{action}] {record.official_date} {record.away_label}@{record.home_label} gamePk={record.game_pk}")

    records.sort(key=lambda item: (item.official_date, item.game_datetime, item.game_pk), reverse=True)
    return records


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="同步最近两周 MLB 已完成比赛并生成站点")
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="忽略已有输出，重新抓取并覆盖最近两周的全部比赛数据",
    )
    return parser.parse_args()


def main() -> None:
    """程序入口。"""
    args = parse_args()
    ensure_dirs()
    records = fetch_recent_completed_games(force_refresh=args.force_refresh)
    cleanup_untracked_outputs(records)
    save_manifest(records)
    render_index(records)
    print(f"generated {len(records)} completed games into {DOCS_DIR}")


if __name__ == "__main__":
    main()
