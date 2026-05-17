import streamlit as st
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

# --- 核心配置 ---
DB_PATH = "sporttery_initial_final_odds.db"
st.set_page_config(page_title="体彩初终盘智能数据控制台", layout="wide", initial_sidebar_state="expanded")

# --- 全局极客暗黑主题 CSS 注入 ---
st.markdown("""
<style>
    .stApp { background-color: #0b101e; color: #c0ccda; font-family: 'Inter', sans-serif; }
    [data-testid="stSidebar"] { background-color: #121827; border-right: 1px solid #1f2937; }
    header { background-color: transparent !important; }
    h1, h2, h3, h4 { color: #ffffff !important; font-weight: 600; }
    [data-testid="stMetricValue"] { font-size: 40px !important; color: #00e5ff !important; font-weight: 700; text-shadow: 0 0 12px rgba(0, 229, 255, 0.4); }
    [data-testid="stMetricLabel"] { color: #8b9bb4 !important; font-size: 15px; }
    .stRadio p { color: #f8fafc !important; font-size: 15px !important; font-weight: 500 !important; }
    .stDateInput div div input { background-color: #161d30 !important; color: #ffffff !important; border: 1px solid #2d3748 !important; }

    /* 🔥 高保真定制化数据列表 CSS */
    .match-list-container { background-color: #121827; border-radius: 12px; padding: 20px; border: 1px solid #1f2937; margin-bottom: 25px; }
    .match-header { display: flex; color: #8b9bb4; font-size: 13px; padding: 10px 20px; border-bottom: 2px solid #1f2937; margin-bottom: 10px; font-weight: 600; }
    .match-row { display: flex; align-items: center; justify-content: space-between; padding: 15px 20px; border-bottom: 1px solid #1a2235; transition: background-color 0.2s; }
    .match-row:hover { background-color: #161d2e; }
    .match-row:last-child { border-bottom: none; }

    .col-league { width: 7%; display: flex; align-items: center; }
    .col-time { width: 11%; color: #8b9bb4; font-size: 13px; font-weight: 500; display: flex; align-items: center; }
    .col-code { width: 9%; color: #8b9bb4; font-size: 14px; font-family: monospace; display: flex; align-items: center; }
    .col-teams { width: 35%; display: flex; align-items: center; justify-content: center; gap: 15px; }
    .col-results { width: 10%; display: flex; flex-direction: column; gap: 4px; align-items: center; justify-content: center; }
    .col-odds { width: 28%; color: #c0ccda; font-size: 13px; text-align: right; letter-spacing: 0.5px; display: flex; align-items: center; justify-content: flex-end; }
    .col-anomaly { width: 38%; text-align: right; display: flex; align-items: center; justify-content: flex-end; }

    .league-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; color: white; text-align: center; width: fit-content; }
    .team-name { color: #ffffff; font-size: 15px; font-weight: 500; }
    .team-home { text-align: right; flex-grow: 1; }
    .team-away { text-align: left; flex-grow: 1; }
    .let-pill { font-size: 11px; padding: 1px 5px; border-radius: 3px; margin-left: 6px; font-weight: bold; }
    .let-positive { color: #ff4d4f; background: rgba(255, 77, 79, 0.15); }
    .let-negative { color: #10b981; background: rgba(16, 185, 129, 0.15); }
    .score-box { background-color: #060913; border: 1px solid #2d3748; color: #f6ad55; font-weight: 700; font-size: 16px; padding: 4px 14px; border-radius: 6px; min-width: 65px; text-align: center; box-shadow: inset 0 2px 4px rgba(0,0,0,0.6); }

    .res-badge { font-size: 11px; padding: 1px 6px; border-radius: 4px; border: 1px solid transparent; text-align: center; min-width: 45px; }
    .res-win { color: #ff4d4f; border-color: rgba(255,77,79,0.3); background: rgba(255,77,79,0.1); }
    .res-draw { color: #3b82f6; border-color: rgba(59,130,246,0.3); background: rgba(59,130,246,0.1); }
    .res-lose { color: #10b981; border-color: rgba(16,185,129,0.3); background: rgba(16,185,129,0.1); }
    .res-none { color: #6b7280; border-color: #374151; background: transparent; }

    .anomaly-badge { display: inline-block; font-size: 12px; padding: 4px 10px; border-radius: 6px; font-weight: 600; }
    .ano-pending { color: #f6ad55; background: rgba(246,173,85,0.15); border: 1px solid rgba(246,173,85,0.3); }
    .ano-no-mid { color: #ef4444; background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); }
    .ano-no-odds { color: #a855f7; background: rgba(168,85,247,0.15); border: 1px solid rgba(168,85,247,0.3); }
</style>
""", unsafe_allow_html=True)


def get_db_connection():
    # 云端只读模式，设置 timeout 防治并发拥堵
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

@st.cache_data(ttl=60)
def fetch_dashboard_metrics():
    total_base, start, end, total_complete = 0, "无", "无", 0
    if not Path(DB_PATH).exists():
        return total_base, start, end, total_complete
    try:
        with get_db_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM matches_base").fetchone()
            if row: total_base = row[0]
            
            row = conn.execute("SELECT MIN(match_date), MAX(match_date) FROM matches_base").fetchone()
            if row and row[0]: start, end = row[0], row[1]
                
            row = conn.execute("SELECT COUNT(DISTINCT b.match_key) FROM matches_base b JOIN match_odds_summary s ON b.match_key = s.match_key WHERE s.spf_initial_odds != '' AND s.spf_final_odds != ''").fetchone()
            if row: total_complete = row[0]
    except: pass
    return total_base, start, end, total_complete

def sort_matches_by_logical_day(df):
    if df.empty or "比赛日期" not in df.columns or "比赛编号" not in df.columns: return df
    prefix_map = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}
    
    def get_logical_date(row):
        date_str = str(row["比赛日期"]).split()[0]
        try: dt = pd.to_datetime(date_str)
        except: return date_str
        prefix = str(row["比赛编号"])[:2]
        if prefix in prefix_map:
            actual_weekday = dt.weekday()
            logical_weekday = prefix_map[prefix]
            diff = (actual_weekday - logical_weekday) % 7
            if 0 < diff < 4: return (dt - pd.Timedelta(days=diff)).strftime('%Y-%m-%d')
        return date_str

    temp_df = df.copy()
    temp_df['logical_date'] = temp_df.apply(get_logical_date, axis=1)
    temp_df = temp_df.sort_values(by=["logical_date", "比赛编号"], ascending=[False, False])
    return temp_df.drop(columns=["logical_date"]).reset_index(drop=True)

def fetch_matches_data(start_date=None, end_date=None, limit=None):
    if not Path(DB_PATH).exists(): return pd.DataFrame()
    sql = """
    SELECT b.match_date AS 比赛日期, b.match_time AS 比赛时间, b.match_num AS 比赛编号, b.league AS 联赛, 
           b.home_team AS 主队, b.away_team AS 客队, b.full_score AS 比分, b.goal_line AS 让球, 
           b.result_spf AS 胜平负, b.result_rqspf AS 让球结果, s.spf_initial_odds, s.spf_final_odds
    FROM matches_base b LEFT JOIN match_odds_summary s ON b.match_key = s.match_key
    """
    params = []
    if start_date and end_date:
        sql += " WHERE b.match_date BETWEEN ? AND ?"
        params.extend([str(start_date), str(end_date)])
    sql += " ORDER BY b.match_date DESC, b.match_num DESC"
    if limit: sql += f" LIMIT {int(limit)}"
    try:
        with get_db_connection() as conn: df = pd.read_sql_query(sql, conn, params=params)
        return sort_matches_by_logical_day(df)
    except:
        return pd.DataFrame()

def fetch_detailed_战报_data(days=2):
    if not Path(DB_PATH).exists(): return pd.DataFrame()
    sql = """
    SELECT b.match_date AS 比赛日期, b.match_time AS 比赛时间, b.match_num AS 比赛编号, b.league AS 联赛,
           b.home_team AS 主队, b.away_team AS 客队, b.full_score AS 比分, b.goal_line AS 让球,
           b.result_spf AS 胜平负, b.result_rqspf AS 让球结果, s.spf_initial_odds, s.spf_final_odds, s.zjq_final_odds
    FROM matches_base b LEFT JOIN match_odds_summary s ON b.match_key = s.match_key
    WHERE b.match_date >= ? AND b.full_score != ''
    """
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        with get_db_connection() as conn: df = pd.read_sql_query(sql, conn, params=[since])
        return sort_matches_by_logical_day(df)
    except:
        return pd.DataFrame()

def fetch_anomaly_matches_data():
    if not Path(DB_PATH).exists(): return pd.DataFrame()
    sql = """
    SELECT b.match_date AS 比赛日期, b.match_time AS 比赛时间, b.match_num AS 比赛编号, b.league AS 联赛, 
           b.home_team AS 主队, b.away_team AS 客队, b.full_score AS 比分, b.mid, s.match_key AS has_summary
    FROM matches_base b LEFT JOIN match_odds_summary s ON b.match_key = s.match_key
    WHERE s.match_key IS NULL OR b.full_score = '' OR s.spf_initial_odds = ''
    """
    try:
        with get_db_connection() as conn: df = pd.read_sql_query(sql, conn)
        reasons, css_classes = [], []
        for _, row in df.iterrows():
            if not str(row['mid']).strip(): reasons.append("缺失官方 MID"); css_classes.append("ano-no-mid")
            elif not str(row['比分']).strip(): reasons.append("尚未完场结算"); css_classes.append("ano-pending")
            elif not row['has_summary']: reasons.append("官方无详细赔率"); css_classes.append("ano-no-odds")
            else: reasons.append("初终盘数据不全"); css_classes.append("ano-no-odds")
        df['缺失原因'] = reasons
        df['CSS_CLASS'] = css_classes
        return sort_matches_by_logical_day(df)
    except:
        return pd.DataFrame()

def get_league_style(league_name):
    league = str(league_name).strip()
    palette = { "英超": "#7c3aed", "西甲": "#ea580c", "德甲": "#dc2626", "意甲": "#2563eb", "法甲": "#65a30d", "欧冠": "#0891b2", "中超": "#059669", "日职": "#db2777" }
    fallback_colors = ["#e11d48", "#c026d3", "#9333ea", "#4f46e5", "#0284c7", "#0891b2", "#0d9488", "#16a34a", "#65a30d", "#ca8a04", "#ea580c", "#dc2626"]
    bg_color = palette.get(league, fallback_colors[(sum(ord(c) for c in league) if league else 0) % len(fallback_colors)])
    try: shadow = f"rgba({int(bg_color[1:3],16)}, {int(bg_color[3:5],16)}, {int(bg_color[5:7],16)}, 0.4)"
    except: shadow = "rgba(255, 255, 255, 0.2)"
    return f"background-color: {bg_color}; box-shadow: 0 2px 6px {shadow};"

def get_res_cls(res):
    if '胜' in str(res): return 'res-win'
    if '平' in str(res): return 'res-draw'
    if '负' in str(res): return 'res-lose'
    return 'res-none'

def render_html_dashboard_list(df, view_type="basic"):
    if df.empty: return '<div style="color:#8b9bb4; text-align:center; padding: 40px;">⚠️ 数据底库未就绪，或该时间段内无数据</div>'
    
    html = '<div class="match-list-container">\n<div class="match-header">\n<div style="width: 7%;">赛事</div>\n<div style="width: 11%;">时间</div>\n<div style="width: 9%;">编号</div>\n<div style="width: 35%; text-align: center;">主队 (让) &nbsp;&nbsp;&nbsp; 比分 &nbsp;&nbsp;&nbsp; 客队</div>\n'
    if view_type == "anomaly": html += '<div style="width: 38%; text-align: right;">未能获取详细赔率的原因诊断</div>\n'
    else:
        html += '<div style="width: 10%; text-align: center;">赛果</div>\n'
        html += '<div style="width: 28%; text-align: right;">终盘总进球 / 赛果初终盘异动</div>\n' if view_type == "detailed" else '<div style="width: 28%; text-align: right;">赛果初盘 ➔ 终盘异动测算</div>\n'
    html += '</div>\n'
    
    for _, row in df.iterrows():
        let_html = ""
        if "让球" in row and str(row['让球']) not in ['0', 'None', 'nan', '']:
            try:
                f_val = float(row['让球'])
                let_cls, let_formatted = ('let-positive', f"+{row['让球']}" if not str(row['让球']).startswith('+') else row['让球']) if f_val > 0 else ('let-negative', row['让球'])
                let_html = f"<span class='let-pill {let_cls}'>{let_formatted}</span>"
            except: pass

        date_str = str(row["比赛日期"])[5:] if str(row["比赛日期"]) != 'nan' else ""
        time_str = str(row["比赛时间"])[:5] if str(row["比赛时间"]) != 'nan' else ""
        
        html += f'<div class="match-row">\n<div class="col-league"><span class="league-badge" style="{get_league_style(row["联赛"])}">{row["联赛"]}</span></div>\n<div class="col-time">{f"{date_str} {time_str}".strip()}</div>\n<div class="col-code">{row["比赛编号"]}</div>\n'
        score_display = row["比分"] if str(row["比分"]).strip() != "" else "vs"
        html += f'<div class="col-teams"><div class="team-name team-home">{row["主队"]}{let_html}</div><div class="score-box">{score_display}</div><div class="team-name team-away">{row["客队"]}</div></div>\n'
        
        if view_type == "anomaly": html += f'<div class="col-anomaly"><span class="anomaly-badge {row["CSS_CLASS"]}">{row["缺失原因"]}</span></div>\n'
        else:
            spf_res, init_str, final_str = str(row.get('胜平负', '')), str(row.get('spf_initial_odds', '')), str(row.get('spf_final_odds', ''))
            trend_html = ""
            if spf_res and init_str and final_str and init_str != 'None' and final_str != 'None' and init_str != '' and final_str != '':
                try:
                    init_f, final_f = float(init_str), float(final_str)
                    if final_f < init_f: trend_html = f"<span style='color: #10b981; font-weight: bold; margin-left: 5px;'>📉降水</span>"
                    elif final_f > init_f: trend_html = f"<span style='color: #ef4444; font-weight: bold; margin-left: 5px;'>📈升水</span>"
                    else: trend_html = f"<span style='color: #6b7280; margin-left: 5px;'>➖走平</span>"
                except: pass

            odds_str = f"🎂{row.get('zjq_final_odds', '-')} | <span style='color:#8b9bb4;'>[{spf_res}]</span> {init_str} ➔ {final_str} {trend_html}" if view_type == "detailed" and spf_res and spf_res != 'None' and spf_res != '' else (f"<span style='color:#8b9bb4;'>[{spf_res}]</span> {init_str} ➔ {final_str} {trend_html}" if spf_res and spf_res != 'None' and spf_res != '' else "- ➔ -")
            html += f'<div class="col-results"><span class="res-badge {get_res_cls(row["胜平负"])}">{row["胜平负"]}</span><span class="res-badge {get_res_cls(row["让球结果"])}">{row["让球结果"]}</span></div>\n<div class="col-odds">{odds_str}</div>\n'
        html += '</div>\n'
    
    html += '</div>'
    return html

def main():
    st.sidebar.title("⚡ 智能数据终端 (Web端)")
    page = st.sidebar.radio("核心分析视图", [
        "🔥 48H 终盘战报看板",
        "📊 数据核心池概览", 
        "🔍 历史全维度回溯", 
        "🕳️ 数据断层与异常排查"
    ])

    if not Path(DB_PATH).exists():
        st.warning(f"⚠️ 尚未连接到底层数据库 ({DB_PATH})。等待数据库同步中...")
        return

    if page == "🔥 48H 终盘战报看板":
        st.title("48H 终盘复盘战报")
        st.caption("展示近48小时内完赛的高阶变盘数据，支持对总进球终盘SP与胜平负初终盘异动进行同屏对齐。")
        detailed_data = fetch_detailed_战报_data(days=2)
        html_content = render_html_dashboard_list(detailed_data, view_type="detailed")
        st.markdown(html_content, unsafe_allow_html=True)

    elif page == "📊 数据核心池概览":
        st.title("全局数据核心池")
        total_base, start, end, total_complete = fetch_dashboard_metrics()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("抓取排表总计", f"{total_base} 场")
        c2.metric("完整赔率提取", f"{total_complete} 场")
        c3.metric("时间线始点", start)
        c4.metric("最新落盘", end)
        st.divider()
        recent_data = fetch_matches_data(limit=30)
        html_content = render_html_dashboard_list(recent_data, view_type="basic")
        st.markdown(html_content, unsafe_allow_html=True)

    elif page == "🔍 历史全维度回溯":
        st.title("历史区间自定义检索")
        col_d1, col_d2 = st.columns(2)
        with col_d1: s_date = st.date_input("检索起始日期", datetime.now() - timedelta(days=3))
        with col_d2: e_date = st.date_input("检索终止日期", datetime.now())
        if s_date <= e_date:
            history_data = fetch_matches_data(start_date=s_date, end_date=e_date)
            html_content = render_html_dashboard_list(history_data, view_type="basic")
            st.markdown(html_content, unsafe_allow_html=True)

    elif page == "🕳️ 数据断层与异常排查":
        st.title("数据断层与黑洞排查")
        anomaly_data = fetch_anomaly_matches_data()
        html_content = render_html_dashboard_list(anomaly_data, view_type="anomaly")
        st.markdown(html_content, unsafe_allow_html=True)

    st.sidebar.divider()
    st.sidebar.markdown("""
    <div style='color: #51637d; font-size: 12px; line-height: 1.6;'>
        <b>📡 终端运行状态：</b><br/>
        • 数据源: 独立持久化 DB 云直连<br/>
        • 同步机制: Git Webhook 触发式热更<br/>
        • 读写锁: 仅只读，免除 I/O 风控阻断
    </div>
    """, unsafe_allow_html=True)

if __name__ == "__main__":
    main()