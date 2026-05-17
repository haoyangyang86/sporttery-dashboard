#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================
# 常量配置
# =========================
BASE_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
HEAD_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchHeadV1.qry"
FIXED_BONUS_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry"
HOME_URL = "https://www.sporttery.cn/"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.sporttery.cn",
    "Referer": "https://www.sporttery.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0"
    ),
}

EXACT_SCORE_PATTERN = re.compile(r"^(\d+):(\d+)$")

PLAY_TYPE_LABELS = {
    "spf": "胜平负",
    "rqspf": "让球胜平负",
    "bf": "比分",
    "zjq": "总进球",
    "bqc": "半全场",
}

BQC_CODE_TO_NAME = {
    "hh": "胜胜", "hd": "胜平", "ha": "胜负",
    "dh": "平胜", "dd": "平平", "da": "平负",
    "ah": "负胜", "ad": "负平", "aa": "负负",
}

@dataclass
class CrawlConfig:
    start_date: str
    end_date: str
    db_path: str
    chunk_days: int = 2
    timeout: int = 20
    page_size: int = 100
    page_pause_min: float = 0.05
    page_pause_max: float = 0.20
    detail_pause_min: float = 0.12
    detail_pause_max: float = 0.35
    chunk_pause_min: float = 0.30
    chunk_pause_max: float = 0.80
    retry_total: int = 4
    retry_backoff: float = 0.8
    only_finished: bool = True
    save_raw_json: bool = True
    incremental_by_date: bool = True
    skip_existing_detail: bool = True

# =========================
# 工具函数
# =========================
def safe_get(obj: Any, *keys: str, default: Any = "") -> Any:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur

def normalize_team_name(name: str) -> str:
    if name is None:
        return ""
    return re.sub(r"\s+", "", str(name)).strip()

def match_identity_key(match_date: str, code: str, home: str, away: str) -> Tuple[str, str, str, str]:
    return (
        str(match_date).strip(),
        str(code).strip(),
        normalize_team_name(home),
        normalize_team_name(away),
    )

def parse_score(score_text: Any) -> Tuple[Optional[int], Optional[int]]:
    if score_text is None:
        return None, None
    s = str(score_text).strip()
    m = EXACT_SCORE_PATTERN.match(s)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))

def score_result(home: Optional[int], away: Optional[int]) -> str:
    if home is None or away is None:
        return ""
    if home > away: return "胜"
    if home == away: return "平"
    return "负"

def parse_goal_line(goal_line: Any) -> Optional[float]:
    if goal_line is None:
        return None
    s = str(goal_line).strip().replace("+", "")
    if s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None

def let_result(home: Optional[int], away: Optional[int], goal_line: Any) -> str:
    if home is None or away is None:
        return ""
    handicap = parse_goal_line(goal_line)
    if handicap is None:
        return ""
    let_home = home + handicap
    if let_home > away: return "让胜"
    if let_home == away: return "让平"
    return "让负"

def total_goals_result(home: Optional[int], away: Optional[int]) -> str:
    if home is None or away is None:
        return ""
    total = home + away
    return "7+" if total >= 7 else str(total)

def exact_score_key(home: Optional[int], away: Optional[int]) -> str:
    if home is None or away is None: return ""
    if home <= 5 and away <= 5: return f"s{home:02d}s{away:02d}"
    if home > away: return "s-1sh"
    if home == away: return "s-1sd"
    return "s-1sa"

def total_goals_key(home: Optional[int], away: Optional[int]) -> str:
    if home is None or away is None: return ""
    total = home + away
    return "s7" if total >= 7 else f"s{total}"

def half_full_key(half_score: str, full_score: str) -> Tuple[str, str]:
    hh, ha = parse_score(half_score)
    fh, fa = parse_score(full_score)
    if None in (hh, ha, fh, fa): return "", ""
    first = score_result(hh, ha)
    second = score_result(fh, fa)
    label = f"{first}{second}"
    mapping = {"胜": "h", "平": "d", "负": "a"}
    code = mapping.get(first, "") + mapping.get(second, "")
    return label, code

def parse_dt(rec: Dict[str, Any]) -> datetime:
    ds = str(rec.get("updateDate", "") or "").strip()
    ts = str(rec.get("updateTime", "") or "").strip()
    text = f"{ds} {ts}".strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try: return datetime.strptime(text, fmt)
        except Exception: pass
    return datetime.min

def latest_record(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    recs = [r for r in records if isinstance(r, dict)]
    if not recs: return {}
    recs.sort(key=parse_dt)
    return recs[-1]

def earliest_record(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    recs = [r for r in records if isinstance(r, dict)]
    if not recs: return {}
    recs.sort(key=parse_dt)
    return recs[0]

def first_nonempty_list(d: Dict[str, Any], exact_keys: List[str], fuzzy_words: List[str]) -> List[Dict[str, Any]]:
    for k in exact_keys:
        v = d.get(k)
        if isinstance(v, list) and v: return v
    for k, v in d.items():
        lk = str(k).lower()
        if isinstance(v, list) and v and any(word in lk for word in fuzzy_words): return v
    return []

def recursive_find_mid(obj: Any) -> Optional[str]:
    preferred_keys = {"sportterymatchid", "mid", "matchid", "sportterymid", "uniformmatchid"}
    def walk(x: Any) -> Optional[str]:
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                if lk in preferred_keys and v is not None and str(v).strip().isdigit(): return str(v).strip()
            for _, v in x.items():
                ans = walk(v)
                if ans: return ans
        elif isinstance(x, list):
            for v in x:
                ans = walk(v)
                if ans: return ans
        return None
    return walk(obj)

def recursive_find_score(obj: Any) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    def walk(x: Any, path: str = ""):
        if isinstance(x, dict):
            for k, v in x.items():
                new_path = f"{path}.{k}" if path else str(k)
                if isinstance(v, str) and EXACT_SCORE_PATTERN.match(v): found.append((new_path, v))
                walk(v, new_path)
        elif isinstance(x, list):
            for i, v in enumerate(x): walk(v, f"{path}[{i}]")
    walk(obj)
    return found

def pick_half_score_from_objects(*objs: Any) -> str:
    preferred_words = ["halfcourtgoal", "halfscore", "halfcourt", "half_goal", "halfgoal", "sectionshalf", "firsthalf", "halftimescore"]
    candidates: List[Tuple[int, str]] = []
    for obj in objs:
        for path, score in recursive_find_score(obj):
            p = path.lower()
            score_value = score.strip()
            rank = 99
            for i, word in enumerate(preferred_words):
                if word in p:
                    rank = i
                    break
            if "full" in p: continue
            if rank < 99: candidates.append((rank, score_value))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    return ""

def sleep_jitter(min_s: float, max_s: float):
    if max_s <= 0: return
    if max_s < min_s: min_s, max_s = max_s, min_s
    time.sleep(random.uniform(min_s, max_s))

def is_finished_match(summary: Dict[str, Any]) -> bool:
    h, a = parse_score(summary.get("full_score", ""))
    return h is not None and a is not None

def _compress_dates_to_chunks(date_list: List[str], chunk_days: int) -> List[Tuple[str, str]]:
    if not date_list: return []
    chunk_days = max(1, int(chunk_days))
    days = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in set(date_list))
    chunks: List[Tuple[str, str]] = []
    start = days[0]
    prev = days[0]
    count = 1
    for cur in days[1:]:
        contiguous = (cur - prev).days == 1
        if contiguous and count < chunk_days:
            prev = cur
            count += 1
            continue
        chunks.append((str(start), str(prev)))
        start = cur
        prev = cur
        count = 1
    chunks.append((str(start), str(prev)))
    return chunks

def _date_list(start_s: str, end_s: str) -> List[str]:
    start_d = datetime.strptime(start_s, "%Y-%m-%d").date()
    end_d = datetime.strptime(end_s, "%Y-%m-%d").date()
    out: List[str] = []
    cur = start_d
    while cur <= end_d:
        out.append(str(cur))
        cur += timedelta(days=1)
    return out

# =========================
# HTTP Session
# =========================
def create_session(cfg: CrawlConfig) -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=cfg.retry_total,
        connect=cfg.retry_total,
        read=cfg.retry_total,
        backoff_factor=cfg.retry_backoff,
        status_forcelist=[429, 500, 502, 503, 504, 567],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    try: s.get(HOME_URL, timeout=cfg.timeout)
    except Exception: pass
    return s

def request_json_with_retry(
    session: requests.Session, url: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 20, total_retry: int = 5, backoff: float = 1.2,
) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(1, total_retry + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            status = resp.status_code
            if status == 567:
                raise requests.HTTPError(f"567 Server Error: Wind-control / temporary block for url: {resp.url}")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            try: session.get(HOME_URL, timeout=min(timeout, 10))
            except Exception: pass
            if attempt < total_retry: sleep_jitter(0.6 * attempt, backoff * attempt + 0.8)
            else: raise
    if last_err: raise last_err
    return {}

# =========================
# SQLite Database
# =========================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crawler_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,
    total_base_matches INTEGER DEFAULT 0,
    total_finished_matches INTEGER DEFAULT 0,
    total_detail_success INTEGER DEFAULT 0,
    total_option_rows INTEGER DEFAULT 0,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS matches_base (
    match_key TEXT PRIMARY KEY,
    match_date TEXT, match_time TEXT, match_datetime TEXT, match_num TEXT,
    league TEXT, home_team TEXT, away_team TEXT,
    full_score TEXT, half_score TEXT, goal_line TEXT,
    result_spf TEXT, result_rqspf TEXT,
    spf_win_odds TEXT, spf_draw_odds TEXT, spf_lose_odds TEXT,
    mid TEXT, source_json TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS match_odds_summary (
    match_key TEXT PRIMARY KEY, mid TEXT,
    has_spf INTEGER, has_rqspf INTEGER, has_bf INTEGER, has_zjq INTEGER, has_bqc INTEGER,
    spf_result TEXT, spf_initial_odds TEXT, spf_final_odds TEXT,
    rqspf_result TEXT, rqspf_initial_odds TEXT, rqspf_final_odds TEXT, rqspf_goal_line_latest TEXT,
    bf_result TEXT, bf_initial_odds TEXT, bf_final_odds TEXT,
    zjq_result TEXT, zjq_initial_odds TEXT, zjq_final_odds TEXT,
    bqc_result TEXT, bqc_initial_odds TEXT, bqc_final_odds TEXT,
    fixed_lists_present TEXT, head_json TEXT, fixed_json TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS match_odds_options (
    option_row_id TEXT PRIMARY KEY, match_key TEXT NOT NULL, mid TEXT,
    odds_type TEXT,
    play_type TEXT, play_name TEXT, option_code TEXT, option_name TEXT, odds TEXT,
    is_hit INTEGER DEFAULT 0, hit_result_text TEXT, goal_line TEXT,
    update_date TEXT, update_time TEXT, snapshot_rank INTEGER DEFAULT 1, updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_match_odds_options_match_key ON match_odds_options(match_key);
CREATE INDEX IF NOT EXISTS idx_match_odds_options_play_type ON match_odds_options(play_type);
CREATE INDEX IF NOT EXISTS idx_matches_base_date ON matches_base(match_date);
"""

@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

def existing_base_dates(db_path: str, start_s: str, end_s: str) -> set[str]:
    with get_conn(db_path) as conn:
        cur = conn.execute("SELECT DISTINCT match_date FROM matches_base WHERE match_date BETWEEN ? AND ?", (start_s, end_s))
        return {str(r[0]) for r in cur.fetchall() if r[0]}

def existing_complete_detail_keys(db_path: str, start_s: str, end_s: str) -> set[str]:
    with get_conn(db_path) as conn:
        cur = conn.execute("""
            SELECT DISTINCT b.match_key
            FROM matches_base b
            JOIN match_odds_summary s ON s.match_key = b.match_key
            WHERE b.match_date BETWEEN ? AND ?
              AND EXISTS (SELECT 1 FROM match_odds_options o WHERE o.match_key = b.match_key LIMIT 1)
        """, (start_s, end_s))
        return {str(r[0]) for r in cur.fetchall() if r[0]}

def load_base_records_for_range(db_path: str, start_s: str, end_s: str) -> List[Dict[str, Any]]:
    with get_conn(db_path) as conn:
        cur = conn.execute("""
            SELECT match_date, match_time, match_datetime, match_num, league, home_team, away_team,
                   full_score, half_score, goal_line, result_spf, result_rqspf,
                   spf_win_odds, spf_draw_odds, spf_lose_odds, mid
            FROM matches_base WHERE match_date BETWEEN ? AND ? ORDER BY match_date ASC, match_num ASC
        """, (start_s, end_s))
        rows = []
        for r in cur.fetchall():
            rows.append({
                "date": r[0] or "", "time": r[1] or "", "datetime": r[2] or "", "code": r[3] or "",
                "league": r[4] or "", "home_team": r[5] or "", "away_team": r[6] or "",
                "full_score": r[7] or "", "half_score": r[8] or "", "goal_line": r[9] or "",
                "result": r[10] or "", "let_result": r[11] or "", "spf_win_odds": r[12] or "",
                "spf_draw_odds": r[13] or "", "spf_lose_odds": r[14] or "", "mid": r[15] or "", "_raw": {},
            })
        return rows

def insert_run_start(conn: sqlite3.Connection, cfg: CrawlConfig) -> int:
    cur = conn.execute(
        "INSERT INTO crawler_runs (created_at, start_date, end_date, status) VALUES (?, ?, ?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), cfg.start_date, cfg.end_date, "running"),
    )
    return int(cur.lastrowid)

def finish_run(conn: sqlite3.Connection, run_id: int, status: str, total_base: int, total_finished: int, total_success: int, total_option_rows: int, note: str = ""):
    conn.execute(
        "UPDATE crawler_runs SET status=?, total_base_matches=?, total_finished_matches=?, total_detail_success=?, total_option_rows=?, note=? WHERE run_id=?",
        (status, total_base, total_finished, total_success, total_option_rows, note, run_id),
    )

def upsert_base(conn: sqlite3.Connection, match_key: str, rec: Dict[str, Any], save_raw: bool):
    conn.execute("""
        INSERT INTO matches_base (
            match_key, match_date, match_time, match_datetime, match_num, league, home_team, away_team, full_score, half_score, goal_line,
            result_spf, result_rqspf, spf_win_odds, spf_draw_odds, spf_lose_odds, mid, source_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_key) DO UPDATE SET
            match_date=excluded.match_date, match_time=excluded.match_time, match_datetime=excluded.match_datetime,
            match_num=excluded.match_num, league=excluded.league, home_team=excluded.home_team, away_team=excluded.away_team,
            full_score=excluded.full_score, half_score=excluded.half_score, goal_line=excluded.goal_line,
            result_spf=excluded.result_spf, result_rqspf=excluded.result_rqspf,
            spf_win_odds=excluded.spf_win_odds, spf_draw_odds=excluded.spf_draw_odds, spf_lose_odds=excluded.spf_lose_odds,
            mid=excluded.mid, source_json=excluded.source_json, updated_at=excluded.updated_at
        """,
        (
            match_key, rec["date"], rec["time"], rec["datetime"], rec["code"], rec["league"], rec["home_team"], rec["away_team"], rec["full_score"], rec["half_score"], rec["goal_line"],
            rec["result"], rec["let_result"], rec["spf_win_odds"], rec["spf_draw_odds"], rec["spf_lose_odds"],
            rec["mid"], json.dumps(rec.get("_raw", {}), ensure_ascii=False) if save_raw else "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )

def upsert_summary(conn: sqlite3.Connection, match_key: str, mid: str, odds_rec: Dict[str, Any], head_json: Dict[str, Any], fixed_json: Dict[str, Any], save_raw: bool):
    conn.execute("""
        INSERT INTO match_odds_summary (
            match_key, mid, has_spf, has_rqspf, has_bf, has_zjq, has_bqc, 
            spf_result, spf_initial_odds, spf_final_odds,
            rqspf_result, rqspf_initial_odds, rqspf_final_odds, rqspf_goal_line_latest, 
            bf_result, bf_initial_odds, bf_final_odds, 
            zjq_result, zjq_initial_odds, zjq_final_odds,
            bqc_result, bqc_initial_odds, bqc_final_odds, 
            fixed_lists_present, head_json, fixed_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_key) DO UPDATE SET
            mid=excluded.mid, has_spf=excluded.has_spf, has_rqspf=excluded.has_rqspf, has_bf=excluded.has_bf, has_zjq=excluded.has_zjq, has_bqc=excluded.has_bqc,
            spf_result=excluded.spf_result, spf_initial_odds=excluded.spf_initial_odds, spf_final_odds=excluded.spf_final_odds, 
            rqspf_result=excluded.rqspf_result, rqspf_initial_odds=excluded.rqspf_initial_odds, rqspf_final_odds=excluded.rqspf_final_odds, rqspf_goal_line_latest=excluded.rqspf_goal_line_latest,
            bf_result=excluded.bf_result, bf_initial_odds=excluded.bf_initial_odds, bf_final_odds=excluded.bf_final_odds, 
            zjq_result=excluded.zjq_result, zjq_initial_odds=excluded.zjq_initial_odds, zjq_final_odds=excluded.zjq_final_odds,
            bqc_result=excluded.bqc_result, bqc_initial_odds=excluded.bqc_initial_odds, bqc_final_odds=excluded.bqc_final_odds, 
            fixed_lists_present=excluded.fixed_lists_present,
            head_json=excluded.head_json, fixed_json=excluded.fixed_json, updated_at=excluded.updated_at
        """,
        (
            match_key, mid, int(bool(odds_rec.get("has_spf"))), int(bool(odds_rec.get("has_rqspf"))), int(bool(odds_rec.get("has_bf"))), int(bool(odds_rec.get("has_zjq"))), int(bool(odds_rec.get("has_bqc"))),
            odds_rec.get("spf_result", ""), odds_rec.get("spf_initial_odds", ""), odds_rec.get("spf_final_odds", ""), 
            odds_rec.get("rqspf_result", ""), odds_rec.get("rqspf_initial_odds", ""), odds_rec.get("rqspf_final_odds", ""), odds_rec.get("rqspf_goal_line_latest", ""),
            odds_rec.get("bf_result", ""), odds_rec.get("bf_initial_odds", ""), odds_rec.get("bf_final_odds", ""), 
            odds_rec.get("zjq_result", ""), odds_rec.get("zjq_initial_odds", ""), odds_rec.get("zjq_final_odds", ""),
            odds_rec.get("bqc_result", ""), odds_rec.get("bqc_initial_odds", ""), odds_rec.get("bqc_final_odds", ""), 
            odds_rec.get("fixed_lists_present", ""),
            json.dumps(head_json, ensure_ascii=False) if save_raw else "", json.dumps(fixed_json, ensure_ascii=False) if save_raw else "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )

def replace_option_rows(conn: sqlite3.Connection, match_key: str, rows: List[Dict[str, Any]]):
    conn.execute("DELETE FROM match_odds_options WHERE match_key = ?", (match_key,))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for row in rows:
        odds_type = row.get("odds_type", "final")
        option_row_id = f"{match_key}|{odds_type}|{row['play_type']}|{row['option_code']}"
        conn.execute("""
            INSERT INTO match_odds_options (
                option_row_id, match_key, mid, odds_type, play_type, play_name, option_code, option_name,
                odds, is_hit, hit_result_text, goal_line, update_date, update_time, snapshot_rank, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                option_row_id, match_key, row.get("mid", ""), odds_type, row.get("play_type", ""), row.get("play_name", ""),
                row.get("option_code", ""), row.get("option_name", ""), row.get("odds", ""), int(bool(row.get("is_hit"))),
                row.get("hit_result_text", ""), row.get("goal_line", ""), row.get("update_date", ""), row.get("update_time", ""),
                int(row.get("snapshot_rank", 1) or 1), now,
            ),
        )

# =========================
# 抓取与解析核心
# =========================
def fetch_uniform_results(session: requests.Session, start_date: str, end_date: str, cfg: CrawlConfig) -> List[Dict[str, Any]]:
    page_no = 1
    all_rows: List[Dict[str, Any]] = []

    while True:
        params = {
            "matchBeginDate": start_date, "matchEndDate": end_date, "leagueId": "",
            "pageSize": str(cfg.page_size), "pageNo": str(page_no), "matchPage": "1", "isFix": "0", "pcOrWap": "1",
        }
        data = request_json_with_retry(
            session, BASE_URL, params=params, timeout=cfg.timeout,
            total_retry=max(cfg.retry_total + 2, 5), backoff=max(cfg.retry_backoff, 1.2),
        )
        rows = safe_get(data, "value", "matchResult", default=[])
        if not isinstance(rows, list) or not rows:
            break
        all_rows.extend(rows)
        if len(rows) < cfg.page_size:
            break
        page_no += 1
        sleep_jitter(cfg.page_pause_min, cfg.page_pause_max)

    return all_rows

def build_base_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    match_date = str(row.get("matchDate", "")).strip()
    match_time = str(row.get("matchTime", "")).strip()
    code = str(row.get("matchNumStr") or row.get("matchNum") or "").strip()
    league = str(row.get("leagueNameAbbr") or row.get("leagueName") or "").strip()
    home = str(row.get("homeTeam", "")).strip()
    away = str(row.get("awayTeam", "")).strip()
    full_score = str(row.get("sectionsNo999", "")).strip()
    half_score = str(row.get("halfScore") or row.get("halfCourtGoal") or row.get("sectionsNo1") or "").strip()
    home_g, away_g = parse_score(full_score)
    result = score_result(home_g, away_g)
    goal_line = str(row.get("goalLine", "")).strip()
    let_res = let_result(home_g, away_g, goal_line)
    mid = recursive_find_mid(row) or ""

    return {
        "date": match_date, "time": match_time, "datetime": f"{match_date} {match_time}".strip(), "code": code,
        "league": league, "home_team": home, "away_team": away, "full_score": full_score, "half_score": half_score,
        "goal_line": goal_line, "result": result, "let_result": let_res,
        "spf_win_odds": str(row.get("h", "")).strip(), "spf_draw_odds": str(row.get("d", "")).strip(), "spf_lose_odds": str(row.get("a", "")).strip(),
        "mid": mid, "_raw": row,
    }

def fetch_match_head(session: requests.Session, mid: str, cfg: CrawlConfig) -> Dict[str, Any]:
    resp = session.get(HEAD_URL, params={"source": "web", "sportteryMatchId": mid}, timeout=cfg.timeout)
    resp.raise_for_status()
    return resp.json()

def fetch_fixed_bonus(session: requests.Session, mid: str, cfg: CrawlConfig) -> Dict[str, Any]:
    resp = session.get(FIXED_BONUS_URL, params={"clientCode": "3001", "matchId": mid}, timeout=cfg.timeout)
    resp.raise_for_status()
    return resp.json()

def code_to_score_name(code: str) -> str:
    if code == "s-1sh": return "胜其他"
    if code == "s-1sd": return "平其他"
    if code == "s-1sa": return "负其他"
    m = re.match(r"^s(\d{2})s(\d{2})$", code)
    if m: return f"{int(m.group(1))}:{int(m.group(2))}"
    return code

def code_to_goals_name(code: str) -> str:
    m = re.match(r"^s(\d)$", code)
    if not m: return code
    num = int(m.group(1))
    return "7+" if num == 7 else f"{num}球"

def normalize_spf_result_to_code(text: str) -> str:
    return {"胜": "h", "平": "d", "负": "a"}.get(text, "")

def normalize_rqspf_result_to_code(text: str) -> str:
    return {"让胜": "h", "让平": "d", "让负": "a"}.get(text, "")

def list_to_options(play_type: str, record: Dict[str, Any], hit_code: str, hit_text: str, mid: str, odds_type: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not isinstance(record, dict) or not record: return rows

    update_date = str(record.get("updateDate", "")).strip()
    update_time = str(record.get("updateTime", "")).strip()
    goal_line = str(record.get("goalLine", "")).strip()

    def push(code: str, name: str, odds: Any):
        odd_s = "" if odds is None else str(odds).strip()
        if odd_s == "": return
        rows.append({
            "mid": mid, "odds_type": odds_type, "play_type": play_type, "play_name": PLAY_TYPE_LABELS[play_type], 
            "option_code": code, "option_name": name,
            "odds": odd_s, "is_hit": code == hit_code and hit_code != "", "hit_result_text": hit_text,
            "goal_line": goal_line, "update_date": update_date, "update_time": update_time, "snapshot_rank": 1,
        })

    if play_type == "spf":
        for code, name in [("h", "胜"), ("d", "平"), ("a", "负")]: push(code, name, record.get(code))
    elif play_type == "rqspf":
        for code, name in [("h", "让胜"), ("d", "让平"), ("a", "让负")]: push(code, name, record.get(code))
    elif play_type == "bf":
        for key, value in record.items():
            if key in {"updateDate", "updateTime", "goalLine"} or key.endswith("f"): continue
            if re.match(r"^s-1s[had]$", key) or re.match(r"^s\d{2}s\d{2}$", key): push(key, code_to_score_name(key), value)
    elif play_type == "zjq":
        for i in range(0, 8):
            key = f"s{i}"
            push(key, code_to_goals_name(key), record.get(key))
    elif play_type == "bqc":
        for code, name in BQC_CODE_TO_NAME.items(): push(code, name, record.get(code))
    return rows

def extract_summary_and_options(fixed_json: Dict[str, Any], full_score: str, half_score: str, goal_line: str, mid: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    value = safe_get(fixed_json, "value", default={})
    odds_history = safe_get(value, "oddsHistory", default={})
    if not isinstance(odds_history, dict): odds_history = {}

    had_list = first_nonempty_list(odds_history, ["hadList"], ["had"])
    hhad_list = first_nonempty_list(odds_history, ["hhadList"], ["hhad", "rqspf", "handicap"])
    crs_list = first_nonempty_list(odds_history, ["crsList"], ["crs", "score"])
    ttg_list = first_nonempty_list(odds_history, ["ttgList"], ["ttg", "goal"])
    hafu_list = first_nonempty_list(odds_history, ["hafuList", "hfList"], ["hafu", "half", "full"])

    # 提取终盘与初盘赔率记录
    latest_had = latest_record(had_list)
    earliest_had = earliest_record(had_list)
    latest_hhad = latest_record(hhad_list)
    earliest_hhad = earliest_record(hhad_list)
    latest_crs = latest_record(crs_list)
    earliest_crs = earliest_record(crs_list)
    latest_ttg = latest_record(ttg_list)
    earliest_ttg = earliest_record(ttg_list)
    latest_hafu = latest_record(hafu_list)
    earliest_hafu = earliest_record(hafu_list)

    home_g, away_g = parse_score(full_score)
    result = score_result(home_g, away_g)
    result_code = normalize_spf_result_to_code(result)

    let_res = let_result(home_g, away_g, goal_line)
    let_code = normalize_rqspf_result_to_code(let_res)

    bf_res = full_score if full_score else ""
    bf_code = exact_score_key(home_g, away_g)

    zjq_res = total_goals_result(home_g, away_g)
    zjq_code = total_goals_key(home_g, away_g)

    bqc_label, bqc_code = half_full_key(half_score, full_score)

    def lookup(rec: Dict[str, Any], key: str) -> str:
        return str(rec.get(key, "")).strip() if rec and key else ""

    summary = {
        "has_spf": bool(had_list), "has_rqspf": bool(hhad_list), "has_bf": bool(crs_list), "has_zjq": bool(ttg_list), "has_bqc": bool(hafu_list),
        "spf_result": result, "spf_initial_odds": lookup(earliest_had, result_code), "spf_final_odds": lookup(latest_had, result_code),
        "rqspf_result": let_res, "rqspf_initial_odds": lookup(earliest_hhad, let_code), "rqspf_final_odds": lookup(latest_hhad, let_code),
        "bf_result": bf_res, "bf_initial_odds": lookup(earliest_crs, bf_code), "bf_final_odds": lookup(latest_crs, bf_code),
        "zjq_result": zjq_res, "zjq_initial_odds": lookup(earliest_ttg, zjq_code), "zjq_final_odds": lookup(latest_ttg, zjq_code),
        "bqc_result": bqc_label, "bqc_initial_odds": lookup(earliest_hafu, bqc_code), "bqc_final_odds": lookup(latest_hafu, bqc_code),
        "rqspf_goal_line_latest": str(latest_hhad.get("goalLine", "")).strip() if latest_hhad else "",
        "fixed_lists_present": ",".join([name for name, lst in [("hadList", had_list), ("hhadList", hhad_list), ("crsList", crs_list), ("ttgList", ttg_list), ("hafuList", hafu_list)] if lst]),
    }

    option_rows: List[Dict[str, Any]] = []
    option_rows.extend(list_to_options("spf", earliest_had, result_code, result, mid, "initial"))
    option_rows.extend(list_to_options("spf", latest_had, result_code, result, mid, "final"))
    option_rows.extend(list_to_options("rqspf", earliest_hhad, let_code, let_res, mid, "initial"))
    option_rows.extend(list_to_options("rqspf", latest_hhad, let_code, let_res, mid, "final"))
    option_rows.extend(list_to_options("bf", earliest_crs, bf_code, bf_res, mid, "initial"))
    option_rows.extend(list_to_options("bf", latest_crs, bf_code, bf_res, mid, "final"))
    option_rows.extend(list_to_options("zjq", earliest_ttg, zjq_code, zjq_res, mid, "initial"))
    option_rows.extend(list_to_options("zjq", latest_ttg, zjq_code, zjq_res, mid, "final"))
    option_rows.extend(list_to_options("bqc", earliest_hafu, bqc_code, bqc_label, mid, "initial"))
    option_rows.extend(list_to_options("bqc", latest_hafu, bqc_code, bqc_label, mid, "final"))

    return summary, option_rows


def crawl_to_db(cfg: CrawlConfig) -> Dict[str, Any]:
    def log(msg: str):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    init_db(cfg.db_path)
    session = create_session(cfg)
    all_base: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    
    with get_conn(cfg.db_path) as conn:
        run_id = insert_run_start(conn, cfg)
        try:
            requested_dates = _date_list(cfg.start_date, cfg.end_date)
            fetch_dates = requested_dates
            if cfg.incremental_by_date:
                existing_dates = existing_base_dates(cfg.db_path, cfg.start_date, cfg.end_date)
                fetch_dates = [d for d in requested_dates if d not in existing_dates]
                log(f"按日期增量更新：目标 {len(requested_dates)} 天，数据库已有 {len(existing_dates)} 天，本次新抓 {len(fetch_dates)} 天")
            else:
                log(f"按全量模式抓取：目标 {len(requested_dates)} 天")

            chunks = _compress_dates_to_chunks(fetch_dates, max(1, cfg.chunk_days))
            total_chunks = len(chunks)
            if not chunks:
                log("基础赛果：没有需要新抓取的日期，直接使用数据库中已有比赛做后续处理")

            for idx, (chunk_start, chunk_end) in enumerate(chunks, start=1):
                log(f"抓取基础赛果分段 {idx}/{total_chunks}: {chunk_start} ~ {chunk_end}")
                rows = fetch_uniform_results(session, chunk_start, chunk_end, cfg)
                for row in rows:
                    base = build_base_summary(row)
                    key = match_identity_key(base["date"], base["code"], base["home_team"], base["away_team"])
                    old = all_base.get(key)
                    if old is None:
                        all_base[key] = base
                    else:
                        if (not old.get("mid") and base.get("mid")) or (not is_finished_match(old) and is_finished_match(base)):
                            all_base[key] = base
                sleep_jitter(cfg.chunk_pause_min, cfg.chunk_pause_max)

            fetched_base = sorted(all_base.values(), key=lambda x: (x["date"], x["code"]))
            for rec in fetched_base:
                key = match_identity_key(rec["date"], rec["code"], rec["home_team"], rec["away_team"])
                upsert_base(conn, "|".join(key), rec, cfg.save_raw_json)
            # ======== 在这里插入一行：立刻提交事务 ========
            conn.commit()
            # ============================================
            ordered_base = load_base_records_for_range(cfg.db_path, cfg.start_date, cfg.end_date)
            finished_rows = [r for r in ordered_base if is_finished_match(r)] if cfg.only_finished else ordered_base
            log(f"范围内基础比赛数: {len(ordered_base)}；进入详细抓取的比赛数: {len(finished_rows)}")

            existing_detail_keys = existing_complete_detail_keys(cfg.db_path, cfg.start_date, cfg.end_date) if cfg.skip_existing_detail else set()
            if cfg.skip_existing_detail:
                log(f"已抓比赛跳过：数据库中已有完整详细赔率的比赛 {len(existing_detail_keys)} 场")

            total_finished = len(finished_rows)
            success = 0
            skipped_existing = 0
            total_option_rows = 0
            
            for idx, rec in enumerate(finished_rows, start=1):
                key_tuple = match_identity_key(rec["date"], rec["code"], rec["home_team"], rec["away_team"])
                match_key = "|".join(key_tuple)
                mid = rec.get("mid", "")
                
                if not mid:
                    log(f"跳过：{rec['date']} {rec['code']} {rec['home_team']} vs {rec['away_team']}，缺少 mid")
                    continue

                if match_key in existing_detail_keys:
                    skipped_existing += 1
                else:
                    try:
                        head_json = fetch_match_head(session, mid, cfg)
                        fixed_json = fetch_fixed_bonus(session, mid, cfg)
                        head_value = safe_get(head_json, "value", default={})
                        
                        if not rec.get("full_score"):
                            rec["full_score"] = str(head_value.get("fullCourtGoal", "")).strip()
                        if not rec.get("half_score"):
                            rec["half_score"] = pick_half_score_from_objects(head_json, fixed_json)

                        summary, option_rows = extract_summary_and_options(
                            fixed_json=fixed_json,
                            full_score=rec.get("full_score", ""),
                            half_score=rec.get("half_score", ""),
                            goal_line=rec.get("goal_line", ""),
                            mid=mid,
                        )
                        upsert_summary(conn, match_key, mid, summary, head_json, fixed_json, cfg.save_raw_json)
                        replace_option_rows(conn, match_key, option_rows)
                        
                        success += 1
                        total_option_rows += len(option_rows)
                    except Exception as e:
                        log(f"详细抓取失败：{rec['date']} {rec['code']} {rec['home_team']} vs {rec['away_team']} | {e}")

                if idx % 10 == 0 or idx == total_finished:
                    log(f"详细抓取进度: {idx}/{total_finished}（跳过已抓 {skipped_existing}）")
                if match_key not in existing_detail_keys:
                    sleep_jitter(cfg.detail_pause_min, cfg.detail_pause_max)

            finish_run(conn, run_id, "success", len(ordered_base), total_finished, success, total_option_rows, f"skipped_existing={skipped_existing}")
            return {
                "run_id": run_id,
                "base_count": len(ordered_base),
                "finished_count": total_finished,
                "detail_success": success,
                "detail_skipped": skipped_existing,
                "option_rows": total_option_rows,
            }
        except Exception as e:
            finish_run(conn, run_id, "failed", 0, 0, 0, 0, str(e))
            raise

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sporttery Results & Odds Crawler")
    parser.add_argument("--start", type=str, default=(date.today() - timedelta(days=2)).strftime("%Y-%m-%d"), help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=date.today().strftime("%Y-%m-%d"), help="End date (YYYY-MM-DD)")
    
    # 【已修改】默认输出新的数据库名称
    parser.add_argument("--db", type=str, default="sporttery_initial_final_odds.db", help="SQLite database path")
    args = parser.parse_args()

    crawler_cfg = CrawlConfig(
        start_date=args.start,
        end_date=args.end,
        db_path=args.db
    )

    print(f"开始爬取体彩数据: {args.start} 到 {args.end}")
    try:
        summary_result = crawl_to_db(crawler_cfg)
        print("\n抓取任务完成！")
        print(f"获取基础比赛：{summary_result['base_count']} 场")
        print(f"进入详细抓取（已完场）：{summary_result['finished_count']} 场")
        print(f"新增详细赔率：{summary_result['detail_success']} 场")
        print(f"跳过已存在：{summary_result['detail_skipped']} 场")
        print(f"赔率表总新增行数：{summary_result['option_rows']} 行")
    except Exception as err:
        print(f"\n抓取过程中发生错误: {err}")