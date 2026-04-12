"""把 MLB play-by-play JSON 渲染成 HTML 页面。"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def ordinal_inning(value: int) -> str:
    """把 1 转成 1st，2 转成 2nd。"""
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def build_section_title(half_inning: str, inning: int) -> str:
    """生成分节标题，例如 Top 1st / Bottom 3rd。"""
    half_label = "Top" if str(half_inning).lower() == "top" else "Bottom"
    return f"{half_label} {ordinal_inning(int(inning))}"


def format_pitcher_display_name(full_name: str) -> str:
    """把投手姓名转换为样例使用的 Last, F 格式。"""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    return f"{parts[-1]}, {parts[0][0]}"


def format_lineup_display_name(full_name: str) -> str:
    """把球员名转换成阵容里更紧凑的展示形式。"""
    parts = full_name.split()
    if not parts:
        return full_name
    return parts[-1]


def build_boxscore_player_map(boxscore_payload: dict | None) -> dict[int, dict]:
    """把 box score 里的球员按 id 建索引。"""
    if not boxscore_payload:
        return {}

    players_by_id: dict[int, dict] = {}
    for side in ("away", "home"):
        team_payload = boxscore_payload.get("teams", {}).get(side, {})
        for player in team_payload.get("players", {}).values():
            person = player.get("person", {})
            player_id = person.get("id")
            if player_id is not None:
                players_by_id[int(player_id)] = player
    return players_by_id


def parse_replaced_player_name(description: str) -> str:
    """从 substitution 描述里解析被替换球员姓名。"""
    match = re.search(r"replaces (.+?)(?:, batting \d+(?:st|nd|rd|th),.*|\.)$", description)
    if not match:
        return ""

    replaced_name = match.group(1).strip()
    words = replaced_name.split()
    if len(words) >= 2 and words[0].islower():
        replaced_name = " ".join(words[-2:])
    return replaced_name


def get_substitution_team_key(play: dict, event: dict) -> str | None:
    """根据半局和 substitution 类型判断该换人属于哪支队伍。"""
    half_inning = str(play.get("about", {}).get("halfInning", "")).lower()
    event_type = event.get("details", {}).get("eventType")

    if event_type == "offensive_substitution":
        return "away" if half_inning == "top" else "home"
    if event_type in {"defensive_substitution", "defensive_switch"}:
        return "home" if half_inning == "top" else "away"
    return None


def extract_starting_lineup_overrides(payload: dict, boxscore_payload: dict | None = None) -> dict:
    """从 substitution 事件里反推出被换下的先发球员。"""
    overrides = {"away": {}, "home": {}}
    players_by_id = build_boxscore_player_map(boxscore_payload)

    for play in payload.get("allPlays", []):
        for event in play.get("playEvents", []):
            details = event.get("details", {})
            event_type = details.get("eventType")
            batting_order = event.get("battingOrder")
            if event_type not in {"offensive_substitution", "defensive_substitution"} or not batting_order:
                continue

            order = int(str(batting_order)) // 100
            if order < 1 or order > 9:
                continue

            team_key = get_substitution_team_key(play, event)
            if team_key is None or order in overrides[team_key]:
                continue

            replaced_id = event.get("replacedPlayer", {}).get("id")
            replaced_player = players_by_id.get(int(replaced_id)) if replaced_id is not None else None
            replaced_person = replaced_player.get("person", {}) if replaced_player else {}
            replaced_name = replaced_person.get("fullName") or parse_replaced_player_name(details.get("description") or "")
            if not replaced_name:
                continue

            replaced_position = ""
            if replaced_player:
                replaced_position = replaced_player.get("position", {}).get("abbreviation", "")

            overrides[team_key][order] = {
                "order": order,
                "id": replaced_id,
                "full_name": replaced_name,
                "display_name": format_lineup_display_name(replaced_name),
                "position": replaced_position,
            }

    return overrides


def apply_starting_lineup_overrides(lineups: dict, overrides: dict) -> dict:
    """把被替换下的先发球员覆盖回先发阵容。"""
    merged = {}

    for team_key in ("away", "home"):
        players_by_order = {player["order"]: dict(player) for player in lineups[team_key]["players"]}
        for order, override in overrides.get(team_key, {}).items():
            existing = players_by_order.get(order, {})
            merged_player = dict(existing)
            merged_player.update(override)
            if not merged_player.get("position"):
                merged_player["position"] = existing.get("position", "")
            players_by_order[order] = merged_player

        merged[team_key] = {
            "label": lineups[team_key]["label"],
            "players": [players_by_order[order] for order in sorted(players_by_order)[:9]],
        }

    return merged


def extract_starting_pitchers(payload: dict, away_label: str, home_label: str) -> dict:
    """从 play-by-play 中提取两队先发投手。"""
    away_pitcher = None
    home_pitcher = None

    for play in payload.get("allPlays", []):
        about = play.get("about", {})
        matchup = play.get("matchup", {})
        pitcher = matchup.get("pitcher", {})
        pitch_hand = matchup.get("pitchHand", {})
        if not pitcher:
            continue

        pitcher_view = {
            "id": pitcher.get("id"),
            "full_name": pitcher.get("fullName", ""),
            "display_name": format_pitcher_display_name(pitcher.get("fullName", "")),
            "hand": pitch_hand.get("description") or pitch_hand.get("code") or "",
            "hand_short": (pitch_hand.get("code") or "").upper() + "HP" if pitch_hand.get("code") else "",
            "avatar_url": f"https://midfield.mlbstatic.com/v1/people/{pitcher.get('id')}/spots/120" if pitcher.get("id") else None,
        }

        if about.get("halfInning") == "top" and home_pitcher is None:
            home_pitcher = pitcher_view
        if about.get("halfInning") == "bottom" and away_pitcher is None:
            away_pitcher = pitcher_view
        if away_pitcher and home_pitcher:
            break

    return {
        "away": {
            "label": away_label,
            **(away_pitcher or {"full_name": "", "display_name": "", "hand": "", "hand_short": "", "avatar_url": None}),
        },
        "home": {
            "label": home_label,
            **(home_pitcher or {"full_name": "", "display_name": "", "hand": "", "hand_short": "", "avatar_url": None}),
        },
    }


def extract_starting_lineups(payload: dict, away_label: str, home_label: str) -> dict:
    """从 play-by-play 中提取两队先发 1-9 棒。"""
    seen = {"top": set(), "bottom": set()}
    lineups = {"top": [], "bottom": []}

    for play in payload.get("allPlays", []):
        half_inning = play.get("about", {}).get("halfInning")
        batter = play.get("matchup", {}).get("batter", {})
        batter_id = batter.get("id")
        batter_name = batter.get("fullName", "")
        if half_inning not in lineups or not batter_id or batter_id in seen[half_inning]:
            continue

        seen[half_inning].add(batter_id)
        lineups[half_inning].append(
            {
                "order": len(lineups[half_inning]) + 1,
                "id": batter_id,
                "full_name": batter_name,
                "display_name": format_lineup_display_name(batter_name),
            }
        )

        if len(lineups["top"]) >= 9 and len(lineups["bottom"]) >= 9:
            break

    return {
        "away": {
            "label": away_label,
            "players": lineups["top"][:9],
        },
        "home": {
            "label": home_label,
            "players": lineups["bottom"][:9],
        },
    }


def extract_starting_lineups_from_boxscore(boxscore_payload: dict, away_label: str, home_label: str) -> dict:
    """从 box score 中提取带守位的先发 1-9 棒。"""
    def build_team_lineup(team_payload: dict, label: str) -> dict:
        players = []
        for player in team_payload.get("players", {}).values():
            batting_order = player.get("battingOrder")
            if not batting_order:
                continue

            batting_order_value = int(str(batting_order))
            if batting_order_value % 100 != 0:
                continue

            game_status = player.get("gameStatus", {})
            if game_status.get("isSubstitute"):
                continue

            order = batting_order_value // 100
            if order < 1 or order > 9:
                continue

            person = player.get("person", {})
            position = player.get("position", {})
            full_name = person.get("fullName", "")
            players.append(
                {
                    "order": order,
                    "id": person.get("id"),
                    "full_name": full_name,
                    "display_name": format_lineup_display_name(full_name),
                    "position": position.get("abbreviation", ""),
                }
            )

        players.sort(key=lambda item: item["order"])
        deduped = []
        seen_orders = set()
        for player in players:
            if player["order"] in seen_orders:
                continue
            seen_orders.add(player["order"])
            deduped.append(player)

        return {
            "label": label,
            "players": deduped[:9],
        }

    teams = boxscore_payload.get("teams", {})
    return {
        "away": build_team_lineup(teams.get("away", {}), away_label),
        "home": build_team_lineup(teams.get("home", {}), home_label),
    }


def infer_boxscore_path(json_path: Path) -> Path | None:
    """根据 play-by-play 文件名推断对应的 box score 文件路径。"""
    match = re.search(r"(\d+)", json_path.stem)
    if not match:
        return None

    game_pk = match.group(1)
    candidates = [
        json_path.with_name(f"boxscore_{game_pk}.json"),
        Path(f"boxscore_{game_pk}.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def build_avatar_text(play: dict) -> str:
    """生成圆形头像里的文本。"""
    matchup = play.get("matchup", {})
    batter_name = matchup.get("batter", {}).get("fullName")
    if batter_name:
        parts = batter_name.split()
        if len(parts) >= 2:
            return f"{parts[0][0]}{parts[-1][0]}".upper()
        return batter_name[:2].upper()

    event = play.get("result", {}).get("event") or "P"
    return event[:2].upper()


def build_event_avatar_text(player_name: str | None, event_label: str) -> str:
    """为补充事件生成圆形头像里的文本。"""
    if player_name:
        parts = player_name.split()
        if len(parts) >= 2:
            return f"{parts[0][0]}{parts[-1][0]}".upper()
        return player_name[:2].upper()
    return event_label[:2].upper()


def normalize_name_variants(full_name: str) -> set[str]:
    """为一个球员名生成可匹配的变体。"""
    variants = {full_name.strip()}
    collapsed = full_name.replace(".", "").strip()
    if collapsed:
        variants.add(collapsed)
    return {variant for variant in variants if variant}


def collect_player_names_from_play(play: dict) -> list[str]:
    """从一个 play 中收集可能出现在描述文本里的球员全名。"""
    names: set[str] = set()
    matchup = play.get("matchup", {})

    for key in ("batter", "pitcher", "postOnFirst", "postOnSecond", "postOnThird"):
        full_name = matchup.get(key, {}).get("fullName")
        if full_name:
            names.update(normalize_name_variants(str(full_name)))

    for runner_movement in play.get("runners", []):
        full_name = runner_movement.get("details", {}).get("runner", {}).get("fullName")
        if full_name:
            names.update(normalize_name_variants(str(full_name)))

    return sorted(names, key=len, reverse=True)


def highlight_player_names(text: str, player_names: list[str]) -> str:
    """给描述中的球员名加上浅灰色虚线下划线样式。"""
    escaped_text = html.escape(text)
    if not escaped_text or not player_names:
        return escaped_text

    replacements: list[tuple[re.Pattern[str], str]] = []
    for player_name in player_names:
        escaped_name = html.escape(player_name)
        if not escaped_name:
            continue
        pattern = re.compile(rf"(?<![\w>]){re.escape(escaped_name)}(?![\w<])")
        replacement = f'<span class="player-name-ref">{escaped_name}</span>'
        replacements.append((pattern, replacement))

    highlighted = escaped_text
    for pattern, replacement in replacements:
        highlighted = pattern.sub(replacement, highlighted)
    return highlighted


def normalize_action_event_label(event_type: str | None, fallback_event: str | None) -> str:
    """把 action 事件名转换成更稳定的展示文案。"""
    if event_type == "pitching_substitution":
        return "Pitching Change"
    if event_type == "balk":
        return "Balk"
    if event_type == "runner_placed":
        return "Runner Placed On Base"
    if event_type == "defensive_substitution":
        return "Defensive Substitution"
    if event_type == "defensive_switch":
        return "Defensive Switch"
    if event_type == "offensive_substitution":
        if fallback_event == "Offensive Substitution":
            return "Offensive Substitution"
        return fallback_event or "Offensive Substitution"
    if event_type and event_type.startswith("stolen_base"):
        return "Stolen Base"
    if event_type and "caught_stealing" in event_type:
        return "Runner Out"
    if event_type and "pickoff" in event_type:
        return "Runner Out"
    if fallback_event:
        return fallback_event
    return "Action"


def build_out_text(play: dict) -> str | None:
    """如果该 play 产生出局，则返回追加显示的 out 文案。"""
    about = play.get("about", {})
    count = play.get("count", {})
    outs = count.get("outs")

    if not about.get("hasOut") or outs is None:
        return None

    unit = "out" if int(outs) == 1 else "outs"
    return f"{outs} {unit}"


def build_action_out_text(event: dict) -> str | None:
    """如果补充事件导致 runner 出局，则返回 out 文案。"""
    details = event.get("details", {})
    count = event.get("count", {})
    outs = count.get("outs")

    if not details.get("isOut") or outs is None:
        return None

    unit = "out" if int(outs) == 1 else "outs"
    return f"{outs} {unit}"


def build_outs_header_text(outs: int) -> str:
    """生成局势条里的出局文案。"""
    unit = "Out" if int(outs) == 1 else "Outs"
    return f"{outs} {unit}"


def build_score_update(play: dict, previous_play: dict | None) -> dict | None:
    """如果该 play 发生比分变化，则返回比分展示数据。"""
    result = play.get("result", {})
    away_score = result.get("awayScore")
    home_score = result.get("homeScore")

    if away_score is None or home_score is None:
        return None

    if previous_play is None:
        previous_away_score = 0
        previous_home_score = 0
    else:
        previous_result = previous_play.get("result", {})
        previous_away_score = previous_result.get("awayScore", away_score)
        previous_home_score = previous_result.get("homeScore", home_score)

    if away_score == previous_away_score and home_score == previous_home_score:
        return None

    if away_score > previous_away_score:
        scoring_team = "away"
    elif home_score > previous_home_score:
        scoring_team = "home"
    else:
        scoring_team = "tie"

    return {
        "away_score": away_score,
        "home_score": home_score,
        "scoring_team": scoring_team,
    }


def build_play_view(play: dict, previous_play: dict | None, player_names: list[str]) -> dict:
    """把原始 play 转成模板可直接消费的结构。"""
    result = play.get("result", {})
    batter = play.get("matchup", {}).get("batter", {})
    batter_id = batter.get("id")
    batter_name = batter.get("fullName") or "Batter"
    event_text = str(result.get("event") or "")
    normalized_event = event_text.lower()
    is_negative_result = (
        "out" in normalized_event
        or "double play" in normalized_event
        or "triple play" in normalized_event
        or "grounded into dp" in normalized_event
    )

    return {
        "event": result.get("event") or result.get("eventType") or "Unknown Event",
        "description": result.get("description") or "",
        "description_html": highlight_player_names(result.get("description") or "", player_names),
        "avatar_text": build_avatar_text(play),
        "avatar_url": f"https://midfield.mlbstatic.com/v1/people/{batter_id}/spots/120" if batter_id else None,
        "avatar_alt": batter_name,
        "layout": "default",
        "badge_variant": "negative" if is_negative_result else "default",
        "out_text": build_out_text(play),
        "score_update": build_score_update(play, previous_play),
    }


def should_include_action_event(event: dict) -> bool:
    """筛选需要放在 at-bat 前面的补充事件。"""
    details = event.get("details", {})
    event_type = details.get("eventType")

    if event_type == "pitching_substitution":
        return True
    if event_type == "balk":
        return True
    if event_type == "runner_placed":
        return True
    if event_type == "defensive_substitution":
        return True
    if event_type == "defensive_switch":
        return True
    if event_type == "offensive_substitution" and event.get("position", {}).get("abbreviation") in {"PH", "PR"}:
        return True
    if event_type and event_type.startswith("stolen_base"):
        return True
    if details.get("isOut") and event.get("isPitch") is False:
        return True
    return False


def is_redundant_runner_out_action(play: dict, event: dict) -> bool:
    """如果 runner 出局已经作为该 play 的最终结果展示，则不再重复插入过程事件。"""
    details = event.get("details", {})
    if not (details.get("isOut") and event.get("isPitch") is False):
        return False

    result = play.get("result", {})
    result_event = (result.get("event") or "").lower()
    result_description = (result.get("description") or "").lower()
    event_description = (details.get("description") or "").lower()

    if "pickoff" in result_event or "caught stealing" in result_event:
        return True
    if "picked off" in result_description or "caught stealing" in result_description:
        return True
    if "pickoff attempt" in event_description and (
        "picked off" in result_description or "caught stealing" in result_description
    ):
        return True
    return False


def build_action_view(event: dict, player_names: list[str]) -> dict:
    """把补充事件转换成模板可消费的结构。"""
    details = event.get("details", {})
    player_name = None

    event_label = normalize_action_event_label(
        details.get("eventType"),
        details.get("event"),
    )

    if details.get("isOut") and event_label == "Action":
        event_label = "Runner Out"

    if details.get("eventType") == "pitching_substitution":
        layout = "pitching_substitution"
        event_label = "Pitching Substitution"
    elif details.get("eventType") == "balk":
        layout = "runner_placed"
        event_label = "Balk"
    elif details.get("eventType") == "runner_placed":
        layout = "runner_placed"
        event_label = "Runner Placed On Base"
    elif details.get("eventType") == "defensive_substitution":
        layout = "pitching_substitution"
        event_label = "Defensive Substitution"
    elif details.get("eventType") == "defensive_switch":
        layout = "pitching_substitution"
        event_label = "Defensive Switch"
    elif details.get("eventType") == "offensive_substitution":
        layout = "pitching_substitution"
        position_abbr = event.get("position", {}).get("abbreviation")
        if position_abbr == "PH":
            event_label = "Pinch Hitter"
        elif position_abbr == "PR":
            event_label = "Pinch Runner"
        else:
            event_label = "Offensive Substitution"
    elif details.get("eventType") and details.get("eventType").startswith("stolen_base"):
        layout = "stolen_base"
    else:
        layout = "default"

    description = details.get("description") or ""
    return {
        "event": event_label,
        "description": description,
        "description_html": highlight_player_names(description, player_names),
        "description_lines": [description],
        "description_lines_html": [highlight_player_names(description, player_names)],
        "avatar_text": build_event_avatar_text(player_name, event_label),
        "avatar_url": None,
        "avatar_alt": event_label,
        "layout": layout,
        "out_text": build_action_out_text(event),
        "score_update": None,
    }


def build_preceding_action_views(play: dict) -> list[dict]:
    """提取每个 at-bat 结果之前需要展示的补充事件。"""
    action_views: list[dict] = []
    player_names = collect_player_names_from_play(play)
    for event in play.get("playEvents", []):
        if not should_include_action_event(event) or is_redundant_runner_out_action(play, event):
            continue

        action_view = build_action_view(event, player_names)
        if (
            action_views
            and action_views[-1].get("event") == "Defensive Switch"
            and action_view.get("event") == "Defensive Switch"
        ):
            previous_lines = action_views[-1].setdefault("description_lines", [action_views[-1].get("description", "")])
            previous_lines_html = action_views[-1].setdefault("description_lines_html", [action_views[-1].get("description_html", "")])
            current_lines = action_view.get("description_lines") or [action_view.get("description", "")]
            current_lines_html = action_view.get("description_lines_html") or [action_view.get("description_html", "")]
            previous_lines.extend(line for line in current_lines if line)
            previous_lines_html.extend(line for line in current_lines_html if line)
            action_views[-1]["description"] = "\n".join(previous_lines)
            action_views[-1]["description_html"] = "<br>".join(previous_lines_html)
            continue

        action_views.append(action_view)
    return action_views


def append_action_view(action_views: list[dict], action_view: dict) -> None:
    """把补充事件加入列表；相邻 defensive switch 合并为一条。"""
    if (
        action_views
        and action_views[-1].get("event") == "Defensive Switch"
        and action_view.get("event") == "Defensive Switch"
    ):
        previous_lines = action_views[-1].setdefault("description_lines", [action_views[-1].get("description", "")])
        previous_lines_html = action_views[-1].setdefault("description_lines_html", [action_views[-1].get("description_html", "")])
        current_lines = action_view.get("description_lines") or [action_view.get("description", "")]
        current_lines_html = action_view.get("description_lines_html") or [action_view.get("description_html", "")]
        previous_lines.extend(line for line in current_lines if line)
        previous_lines_html.extend(line for line in current_lines_html if line)
        action_views[-1]["description"] = "\n".join(previous_lines)
        action_views[-1]["description_html"] = "<br>".join(previous_lines_html)
        return

    action_views.append(action_view)


def empty_bases() -> dict:
    """创建空垒包状态。"""
    return {"1B": None, "2B": None, "3B": None}


def clone_state(state: dict) -> dict:
    """复制比赛状态。"""
    return {
        "outs": state["outs"],
        "bases": dict(state["bases"]),
        "away_score": state["away_score"],
        "home_score": state["home_score"],
    }


def apply_runner_movement(state: dict, runner_movement: dict) -> None:
    """把一条 runner movement 应用到当前状态。"""
    movement = runner_movement.get("movement", {})
    details = runner_movement.get("details", {})
    runner = details.get("runner", {})
    runner_id = runner.get("id")

    start = movement.get("start") or movement.get("originBase")
    end = movement.get("end")
    is_out = movement.get("isOut")
    out_number = movement.get("outNumber")

    if start in state["bases"] and state["bases"].get(start) == runner_id:
        state["bases"][start] = None
    elif start in state["bases"] and runner_id is not None:
        for base, occupant in state["bases"].items():
            if occupant == runner_id:
                state["bases"][base] = None
                break

    if is_out:
        if out_number is not None:
            state["outs"] = int(out_number)
        else:
            state["outs"] += 1
        return

    if end in state["bases"]:
        state["bases"][end] = runner_id


def apply_runner_movements(state: dict, runner_movements: list[dict]) -> None:
    """顺序应用多条 runner movement。"""
    for runner_movement in runner_movements:
        apply_runner_movement(state, runner_movement)


def synthesize_runner_placed_movement(event: dict) -> dict | None:
    """为缺少 runners[] movement 的 runner_placed 事件合成跑垒变化。"""
    if event.get("details", {}).get("eventType") != "runner_placed":
        return None

    player = event.get("player") or {}
    runner_id = player.get("id")
    base = event.get("base")
    if runner_id is None or base not in {1, 2, 3}:
        return None

    return {
        "movement": {
            "originBase": None,
            "start": None,
            "end": f"{base}B",
            "outBase": None,
            "isOut": False,
            "outNumber": None,
        },
        "details": {
            "event": event.get("details", {}).get("event"),
            "eventType": "runner_placed",
            "movementReason": None,
            "runner": {
                "id": runner_id,
                "fullName": player.get("fullName"),
                "link": player.get("link"),
            },
            "responsiblePitcher": None,
            "isScoringEvent": False,
            "rbi": False,
            "earned": False,
            "teamUnearned": False,
            "playIndex": event.get("index"),
        },
    }


def get_action_runner_movements(play: dict, event: dict) -> list[dict]:
    """取某个 action event 对应的 runner movement。"""
    event_index = event.get("index")
    runner_movements = [
        runner_movement
        for runner_movement in play.get("runners", [])
        if runner_movement.get("details", {}).get("playIndex") == event_index
    ]
    if runner_movements:
        return runner_movements

    synthetic_movement = synthesize_runner_placed_movement(event)
    if synthetic_movement:
        return [synthetic_movement]

    return []


def get_final_runner_movements(play: dict) -> list[dict]:
    """取属于最终 at-bat 结果的 runner movement。"""
    play_indices = [
        runner_movement.get("details", {}).get("playIndex")
        for runner_movement in play.get("runners", [])
        if runner_movement.get("details", {}).get("playIndex") is not None
    ]
    if not play_indices:
        return []

    final_play_index = max(play_indices)
    return [
        runner_movement
        for runner_movement in play.get("runners", [])
        if runner_movement.get("details", {}).get("playIndex") == final_play_index
    ]


def build_base_out_state_view(state: dict) -> dict:
    """把状态转换成模板可消费的垒包/出局信息。"""
    bases = state["bases"]
    outs = int(state["outs"])
    return {
        "first": bases.get("1B") is not None,
        "second": bases.get("2B") is not None,
        "third": bases.get("3B") is not None,
        "outs_text": build_outs_header_text(outs),
        "away_score": state["away_score"],
        "home_score": state["home_score"],
    }


def build_sections(all_plays: list[dict]) -> list[dict]:
    """按半局对 play 进行分组。"""
    sections: list[dict] = []
    current_key: tuple[str, int] | None = None
    previous_play: dict | None = None
    game_state = {"outs": 0, "bases": empty_bases(), "away_score": 0, "home_score": 0}

    for play in all_plays:
        player_names = collect_player_names_from_play(play)
        about = play.get("about", {})
        half_inning = about.get("halfInning", "")
        inning = int(about.get("inning", 0))
        key = (str(half_inning).lower(), inning)

        if key != current_key:
            current_key = key
            game_state = {
                "outs": 0,
                "bases": empty_bases(),
                "away_score": game_state["away_score"],
                "home_score": game_state["home_score"],
            }
            sections.append(
                {
                    "title": build_section_title(half_inning, inning),
                    "plays": [],
                }
            )

        for event in play.get("playEvents", []):
            if should_include_action_event(event) and not is_redundant_runner_out_action(play, event):
                append_action_view(sections[-1]["plays"], build_action_view(event, player_names))
                apply_runner_movements(game_state, get_action_runner_movements(play, event))

        play_view = build_play_view(play, previous_play, player_names)
        play_view["base_out_state"] = build_base_out_state_view(clone_state(game_state))
        sections[-1]["plays"].append(play_view)

        apply_runner_movements(game_state, get_final_runner_movements(play))
        result = play.get("result", {})
        if result.get("awayScore") is not None:
            game_state["away_score"] = result["awayScore"]
        if result.get("homeScore") is not None:
            game_state["home_score"] = result["homeScore"]
        previous_play = play

    return sections


def render_html(
    json_path: Path,
    template_path: Path,
    output_path: Path,
    away_label: str,
    home_label: str,
) -> None:
    """加载 JSON，渲染模板并写出 HTML。"""
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    boxscore_path = infer_boxscore_path(json_path)
    boxscore_payload = json.loads(boxscore_path.read_text(encoding="utf-8")) if boxscore_path else None
    render_html_from_payload(
        payload=payload,
        template_path=template_path,
        output_path=output_path,
        away_label=away_label,
        home_label=home_label,
        page_title=f"Play-by-Play {json_path.stem}",
        boxscore_payload=boxscore_payload,
    )


def render_html_from_payload(
    payload: dict,
    template_path: Path,
    output_path: Path,
    away_label: str,
    home_label: str,
    page_title: str,
    boxscore_payload: dict | None = None,
) -> None:
    """直接从内存中的 play-by-play 数据渲染 HTML。"""
    sections = build_sections(payload.get("allPlays", []))
    starting_pitchers = extract_starting_pitchers(payload, away_label, home_label)
    if boxscore_payload:
        starting_lineups = extract_starting_lineups_from_boxscore(boxscore_payload, away_label, home_label)
    else:
        starting_lineups = apply_starting_lineup_overrides(
            extract_starting_lineups(payload, away_label, home_label),
            extract_starting_lineup_overrides(payload, None),
        )
    for section in sections:
        for play in section["plays"]:
            if play.get("layout") == "default":
                base_out_state = play.get("base_out_state") or {}
                play["current_score_text"] = f"{away_label} {base_out_state.get('away_score', 0)} - {base_out_state.get('home_score', 0)} {home_label}"

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template(template_path.name)
    html = template.render(
        page_title=page_title,
        away_label=away_label,
        home_label=home_label,
        starting_pitchers=starting_pitchers,
        starting_lineups=starting_lineups,
        sections=sections,
    )
    output_path.write_text(html, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="把 play-by-play JSON 渲染成 HTML")
    parser.add_argument("json_path", help="输入的 play-by-play JSON 文件")
    parser.add_argument(
        "--template",
        default="templates/play_by_play_page.html.j2",
        help="Jinja 模板路径",
    )
    parser.add_argument(
        "--output",
        help="输出 HTML 路径，默认与 JSON 同名",
    )
    parser.add_argument(
        "--away-label",
        default="Away",
        help="客队缩写或名称",
    )
    parser.add_argument(
        "--home-label",
        default="Home",
        help="主队缩写或名称",
    )
    return parser.parse_args()


def main() -> None:
    """程序入口。"""
    args = parse_args()
    json_path = Path(args.json_path)
    template_path = Path(args.template)
    output_path = Path(args.output) if args.output else json_path.with_suffix(".html")

    render_html(
        json_path=json_path,
        template_path=template_path,
        output_path=output_path,
        away_label=args.away_label,
        home_label=args.home_label,
    )

    print(f"已生成 HTML: {output_path}")


if __name__ == "__main__":
    main()
