import os
import sqlite3
from datetime import datetime, timedelta

MASTER_DB = "sporttery_initial_final_odds.db"
CLOUD_DB = "sporttery_cloud_sync.db"

# 1. 动态计算日期：前天 到 今天 (覆盖48H)
end_date = datetime.now()
start_date = end_date - timedelta(days=2)
start_str = start_date.strftime("%Y-%m-%d")
end_str = end_date.strftime("%Y-%m-%d")

print(f"========== 1. 开始抓取并落盘全量库 ({start_str} 到 {end_str}) ==========")
# 调用爬虫，数据会稳稳地存入本地全量库 (MASTER_DB)
cmd_crawl = f"C:/Python314/python.exe sporttery_crawler.py --start {start_str} --end {end_str}"
os.system(cmd_crawl)

print("\n========== 2. 构建云端专属瘦身库 (仅保留近 180 天) ==========")
# 如果存在旧的云端库，直接删掉重建，保证体积最小化
if os.path.exists(CLOUD_DB):
    os.remove(CLOUD_DB)

# 初始化瘦身库的表结构
conn_cloud = sqlite3.connect(CLOUD_DB)
conn_cloud.executescript("""
    CREATE TABLE matches_base (match_key TEXT PRIMARY KEY, match_date TEXT, match_time TEXT, match_datetime TEXT, match_num TEXT, league TEXT, home_team TEXT, away_team TEXT, full_score TEXT, half_score TEXT, goal_line TEXT, result_spf TEXT, result_rqspf TEXT, spf_win_odds TEXT, spf_draw_odds TEXT, spf_lose_odds TEXT, mid TEXT, source_json TEXT, updated_at TEXT);
    CREATE TABLE match_odds_summary (match_key TEXT PRIMARY KEY, mid TEXT, has_spf INTEGER, has_rqspf INTEGER, has_bf INTEGER, has_zjq INTEGER, has_bqc INTEGER, spf_result TEXT, spf_initial_odds TEXT, spf_final_odds TEXT, rqspf_result TEXT, rqspf_initial_odds TEXT, rqspf_final_odds TEXT, rqspf_goal_line_latest TEXT, bf_result TEXT, bf_initial_odds TEXT, bf_final_odds TEXT, zjq_result TEXT, zjq_initial_odds TEXT, zjq_final_odds TEXT, bqc_result TEXT, bqc_initial_odds TEXT, bqc_final_odds TEXT, fixed_lists_present TEXT, head_json TEXT, fixed_json TEXT, updated_at TEXT);
""")
conn_cloud.close()

# 核心切片逻辑：连接主库，并将瘦身库作为附加库挂载
conn_master = sqlite3.connect(MASTER_DB)
conn_master.execute(f"ATTACH DATABASE '{CLOUD_DB}' AS cloud")
six_months_ago = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')

# 只将近半年的基础赛果复制过去
conn_master.execute("INSERT INTO cloud.matches_base SELECT * FROM main.matches_base WHERE match_date >= ?", (six_months_ago,))
# 只将这半年赛果对应的详细赔率复制过去
conn_master.execute("INSERT INTO cloud.match_odds_summary SELECT s.* FROM main.match_odds_summary s JOIN cloud.matches_base b ON s.match_key = b.match_key")

conn_master.commit()
conn_master.close()
print("🎯 瘦身完成！")

print("\n========== 3. 同步至 GitHub 云端 ==========")
# 注意：这里我们只推送 CLOUD_DB
os.system(f"git add {CLOUD_DB}")
os.system('git commit -m "auto: 滚动推送近半年瘦身数据"')
os.system("git push origin main")

print("\n========== 🎉 全部完成！云端大屏即将自动更新 ==========")