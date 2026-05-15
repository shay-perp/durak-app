# requirements.txt:
# streamlit
# st-gsheets-connection
# gspread
# google-auth
# pandas

"""
DURAK System — Tournament Manager
==================================
Architecture (v3):
- Game state lives in st.session_state for the scorekeeper.
- The sheet is a backup log, not the source of truth during play.
- Writes use gspread.append_row (single row, no full-sheet rewrite).
- Reads only happen on entry, "resume match", or analytics tab open.
- match_summary worksheet holds pre-aggregated per-night JSON so
  analytics stay fast even after years of play.
"""

import json
import uuid
from datetime import datetime

import gspread
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials
from streamlit_gsheets import GSheetsConnection

# ============================================================
# CONFIGURATION
# ============================================================
# 🔐 סיסמת הרשם — שנה אותה
SCOREKEEPER_PASSWORD = st.secrets["app"]["scorekeeper_password"]

# Worksheet names
PLAYERS_WS = "players"
MATCHES_WS = "match_nights"
ROUNDS_WS = "rounds"
LOSSES_WS = "losses"
CHAMPIONS_WS = "champions"
SUMMARY_WS = "match_summary"

# Column schemas
COLS = {
    PLAYERS_WS: ["player_name"],
    MATCHES_WS: ["match_id", "date", "start_time", "end_time", "status"],
    ROUNDS_WS: ["round_id", "match_id", "round_number", "status", "loser_name", "end_type"],
    LOSSES_WS: ["loss_id", "match_id", "round_number", "player_name", "loss_timestamp", "loss_count_in_round"],
    CHAMPIONS_WS: ["match_id", "player_name", "title"],
    SUMMARY_WS: ["match_id", "date", "summary_json"],
}

# Read cache lifetime (seconds). Long because reads are now rare.
READ_TTL = 30

# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(
    page_title="DURAK System",
    page_icon="🃏",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
        html, body, [class*="css"] { direction: rtl; text-align: right; }
        .stButton > button { width: 100%; height: 3em; font-size: 1.1em; font-weight: bold; }
        .stTabs [data-baseweb="tab-list"] { justify-content: center; gap: 8px; }
        .stTabs [data-baseweb="tab"] { font-size: 1.1em; font-weight: bold; }
        div[data-testid="stMetricValue"] { font-size: 1.4em; }
        .danger-player {
            background-color: #ffcccc; padding: 8px; border-radius: 8px; font-weight: bold;
        }
        .safe-player {
            background-color: #f0f0f0; padding: 8px; border-radius: 8px;
        }
        .viewer-badge {
            background-color: #e7f3ff; border: 1px solid #b3d9ff;
            padding: 6px 12px; border-radius: 6px; font-size: 0.9em; text-align: center;
        }
        .keeper-badge {
            background-color: #d4edda; border: 1px solid #a3d9a5;
            padding: 6px 12px; border-radius: 6px; font-size: 0.9em; text-align: center;
        }
        .save-indicator {
            font-size: 0.85em; color: #28a745; padding: 4px 8px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# ============================================================
# SESSION STATE INITIALIZATION
# ============================================================
def _init_state():
    defaults = {
        "is_scorekeeper": False,
        # Game state (None when no active match for this scorekeeper)
        "game": None,
        # Cached lookups (loaded once on demand)
        "cached_players": None,
        # Save indicator
        "last_save_msg": None,
        "last_save_time": None,
        # Analytics cache
        "analytics_summaries": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


def is_keeper() -> bool:
    return st.session_state.is_scorekeeper


def mark_saved(msg: str = "נשמר"):
    st.session_state.last_save_msg = msg
    st.session_state.last_save_time = datetime.now()


# ============================================================
# CONNECTIONS
# ============================================================
# Read connection (uses st.connection, cached, handles auth from secrets)
conn = st.connection("gsheets", type=GSheetsConnection)


@st.cache_resource(show_spinner=False)
def _get_gspread_client():
    """Build a gspread client from the same secrets used by st.connection."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    raw = dict(st.secrets["connections"]["gsheets"])
    # gspread expects standard service-account fields
    info = {
        "type": raw.get("type", "service_account"),
        "project_id": raw["project_id"],
        "private_key_id": raw["private_key_id"],
        "private_key": raw["private_key"],
        "client_email": raw["client_email"],
        "client_id": raw["client_id"],
        "auth_uri": raw.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
        "token_uri": raw.get("token_uri", "https://oauth2.googleapis.com/token"),
        "auth_provider_x509_cert_url": raw.get(
            "auth_provider_x509_cert_url",
            "https://www.googleapis.com/oauth2/v1/certs",
        ),
        "client_x509_cert_url": raw["client_x509_cert_url"],
        "universe_domain": raw.get("universe_domain", "googleapis.com"),
    }
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


@st.cache_resource(show_spinner=False)
def _get_spreadsheet():
    client = _get_gspread_client()
    url = st.secrets["connections"]["gsheets"]["spreadsheet"]
    return client.open_by_url(url)


def _get_worksheet(name: str):
    return _get_spreadsheet().worksheet(name)


# ============================================================
# WRITE LAYER — single-row appends, no full-sheet rewrites
# ============================================================
def append_row_gspread(worksheet_name: str, row_dict: dict):
    """Append one row to the given worksheet without reading first."""
    ws = _get_worksheet(worksheet_name)
    cols = COLS[worksheet_name]
    row = [str(row_dict.get(c, "")) for c in cols]
    ws.append_row(row, value_input_option="USER_ENTERED")


def update_cell_gspread(worksheet_name: str, match_id: str, target_col: str, new_value: str, key_col: str = "match_id"):
    """Find a row by key_col=match_id and update one cell.
    Used for end_time, status changes, etc.
    Reads the worksheet once — needed to find row index.
    """
    ws = _get_worksheet(worksheet_name)
    cols = COLS[worksheet_name]
    key_idx = cols.index(key_col) + 1  # 1-indexed
    target_idx = cols.index(target_col) + 1
    cell = ws.find(match_id, in_column=key_idx)
    if cell is None:
        raise ValueError(f"row with {key_col}={match_id} not found in {worksheet_name}")
    ws.update_cell(cell.row, target_idx, new_value)


def update_round_status_gspread(round_id: str, status: str, loser_name: str, end_type: str):
    """Find a round row by round_id and update status, loser_name, end_type."""
    ws = _get_worksheet(ROUNDS_WS)
    cols = COLS[ROUNDS_WS]
    rid_col = cols.index("round_id") + 1
    cell = ws.find(round_id, in_column=rid_col)
    if cell is None:
        raise ValueError(f"round_id {round_id} not found")
    row = cell.row
    status_col = cols.index("status") + 1
    loser_col = cols.index("loser_name") + 1
    end_type_col = cols.index("end_type") + 1
    # gspread batch update — three cells in one API call
    ws.batch_update([
        {"range": gspread.utils.rowcol_to_a1(row, status_col), "values": [[status]]},
        {"range": gspread.utils.rowcol_to_a1(row, loser_col), "values": [[loser_name]]},
        {"range": gspread.utils.rowcol_to_a1(row, end_type_col), "values": [[end_type]]},
    ])


# ============================================================
# READ LAYER — used sparingly
# ============================================================
def read_ws(worksheet: str) -> pd.DataFrame:
    """Read a worksheet via the cached connection."""
    try:
        df = conn.read(worksheet=worksheet, ttl=READ_TTL)
        if df is None:
            return pd.DataFrame()
        return df.dropna(how="all").reset_index(drop=True)
    except Exception as e:
        msg = str(e)
        if "429" in msg or "Quota" in msg or "RATE_LIMIT" in msg:
            st.error("⚠️ הגענו למכסת הקריאות. המתן דקה ונסה שוב.")
        else:
            st.error(f"שגיאה בקריאה מ-{worksheet}: {e}")
        return pd.DataFrame()


def force_refresh_reads():
    try:
        conn.reset()
    except Exception:
        pass
    st.session_state.cached_players = None
    st.session_state.analytics_summaries = None


# ============================================================
# GAME STATE — lives in session_state.game
# ============================================================
def new_game_state(match_id: str, start_time: str, participants: list) -> dict:
    return {
        "match_id": match_id,
        "start_time": start_time,
        "participants": participants,
        "round_num": 0,           # 0 = not started yet (post-setup)
        "round_id": None,         # current round's round_id
        "round_status": None,     # None | "Active" | "Completed"
        "losses_in_round": {p: 0 for p in participants},
        "all_losses": [],         # list of {player, round, timestamp}
        "crowns": [],             # list of {round, loser, end_type}
    }


def bootstrap_state_from_sheets(match_id: str) -> dict | None:
    """Load full game state from the sheet for an active match.
    Used once when scorekeeper resumes an active match."""
    matches = read_ws(MATCHES_WS)
    rounds = read_ws(ROUNDS_WS)
    losses = read_ws(LOSSES_WS)

    if matches.empty:
        return None
    match_row = matches[matches["match_id"] == match_id]
    if match_row.empty:
        return None
    match_row = match_row.iloc[0]

    # Participants from Setup row
    participants = []
    if not rounds.empty:
        setup = rounds[(rounds["match_id"] == match_id) & (rounds["status"] == "Setup")]
        if not setup.empty:
            raw = setup.iloc[0].get("loser_name", "")
            if raw and not pd.isna(raw):
                participants = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not participants:
        return None

    state = new_game_state(
        match_id=match_id,
        start_time=str(match_row.get("start_time", "")),
        participants=participants,
    )

    # Reconstruct crowns + current round
    if not rounds.empty:
        match_rounds = rounds[
            (rounds["match_id"] == match_id) & (rounds["status"] != "Setup")
        ].copy()
        match_rounds["round_number"] = pd.to_numeric(match_rounds["round_number"], errors="coerce")
        match_rounds = match_rounds.sort_values("round_number")
        for _, r in match_rounds.iterrows():
            rnum = int(r["round_number"])
            if r["status"] == "Completed":
                state["crowns"].append({
                    "round": rnum,
                    "loser": str(r.get("loser_name", "")),
                    "end_type": str(r.get("end_type", "")),
                })
            elif r["status"] == "Active":
                state["round_num"] = rnum
                state["round_id"] = str(r["round_id"])
                state["round_status"] = "Active"
        # If no active round, set round_num to the latest completed
        if state["round_status"] is None and len(match_rounds):
            completed = match_rounds[match_rounds["status"] == "Completed"]
            if not completed.empty:
                state["round_num"] = int(completed["round_number"].max())
                state["round_status"] = "Completed"

    # Reconstruct losses in current round (if active) + all_losses log
    if not losses.empty:
        match_losses = losses[losses["match_id"] == match_id].copy()
        if not match_losses.empty:
            match_losses["round_number"] = pd.to_numeric(match_losses["round_number"], errors="coerce")
            for _, l in match_losses.iterrows():
                state["all_losses"].append({
                    "player": str(l["player_name"]),
                    "round": int(l["round_number"]),
                    "timestamp": str(l.get("loss_timestamp", "")),
                })
            if state["round_status"] == "Active":
                current = match_losses[match_losses["round_number"] == state["round_num"]]
                counts = current.groupby("player_name").size().to_dict()
                for p in participants:
                    state["losses_in_round"][p] = int(counts.get(p, 0))

    return state


def find_active_match_id() -> str | None:
    matches = read_ws(MATCHES_WS)
    if matches.empty or "status" not in matches.columns:
        return None
    active = matches[matches["status"] == "Active"]
    if active.empty:
        return None
    return str(active.iloc[-1]["match_id"])


# ============================================================
# GAME ACTIONS — update memory THEN write one row
# ============================================================
def action_add_player(name: str) -> tuple[bool, str]:
    name = name.strip()
    if not name:
        return False, "שם ריק"
    # Use cached_players to avoid an extra read
    if st.session_state.cached_players is None:
        df = read_ws(PLAYERS_WS)
        st.session_state.cached_players = (
            df["player_name"].dropna().astype(str).str.strip().tolist()
            if not df.empty and "player_name" in df.columns
            else []
        )
    existing = [p.lower() for p in st.session_state.cached_players]
    if name.lower() in existing:
        return False, f"השחקן '{name}' כבר קיים"
    append_row_gspread(PLAYERS_WS, {"player_name": name})
    st.session_state.cached_players.append(name)
    return True, f"השחקן '{name}' נוסף"


def action_start_match(participants: list) -> str:
    now = datetime.now()
    match_id = f"M-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    start_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    append_row_gspread(MATCHES_WS, {
        "match_id": match_id,
        "date": now.strftime("%Y-%m-%d"),
        "start_time": start_time_str,
        "end_time": "",
        "status": "Active",
    })
    append_row_gspread(ROUNDS_WS, {
        "round_id": f"R-{match_id}-SETUP",
        "match_id": match_id,
        "round_number": 0,
        "status": "Setup",
        "loser_name": ",".join(participants),
        "end_type": "",
    })
    st.session_state.game = new_game_state(match_id, start_time_str, participants)
    return match_id


def action_start_round():
    g = st.session_state.game
    next_num = (
        max([c["round"] for c in g["crowns"]] + [g["round_num"]] + [0]) + 1
        if g["round_status"] != "Active" else g["round_num"]
    )
    round_id = f"R-{g['match_id']}-{next_num}"
    append_row_gspread(ROUNDS_WS, {
        "round_id": round_id,
        "match_id": g["match_id"],
        "round_number": next_num,
        "status": "Active",
        "loser_name": "",
        "end_type": "",
    })
    g["round_num"] = next_num
    g["round_id"] = round_id
    g["round_status"] = "Active"
    g["losses_in_round"] = {p: 0 for p in g["participants"]}


def action_record_loss(player: str) -> tuple[bool, str]:
    """Update memory and append one row. If 5 reached, also auto-close round."""
    g = st.session_state.game
    if g is None or g["round_status"] != "Active":
        return False, "אין סיבוב פעיל"
    new_count = g["losses_in_round"].get(player, 0) + 1
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    loss_id = f"L-{g['match_id']}-R{g['round_num']}-{uuid.uuid4().hex[:6]}"
    append_row_gspread(LOSSES_WS, {
        "loss_id": loss_id,
        "match_id": g["match_id"],
        "round_number": g["round_num"],
        "player_name": player,
        "loss_timestamp": timestamp,
        "loss_count_in_round": new_count,
    })
    g["losses_in_round"][player] = new_count
    g["all_losses"].append({"player": player, "round": g["round_num"], "timestamp": timestamp})

    if new_count >= 5:
        # Auto-end round
        action_end_round(player, end_type="Automatic")
        return True, f"🏆 {player} סיים את הסיבוב עם 5 הפסדים!"
    return True, "נשמר"


def action_end_round(loser: str, end_type: str = "Manual"):
    g = st.session_state.game
    update_round_status_gspread(g["round_id"], "Completed", loser, end_type)
    g["crowns"].append({"round": g["round_num"], "loser": loser, "end_type": end_type})
    g["round_status"] = "Completed"


def action_end_match():
    """Close the night, crown champion, write summary."""
    g = st.session_state.game
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Update match status + end_time
    ws = _get_worksheet(MATCHES_WS)
    cols = COLS[MATCHES_WS]
    mid_col = cols.index("match_id") + 1
    cell = ws.find(g["match_id"], in_column=mid_col)
    if cell:
        status_col = cols.index("status") + 1
        end_col = cols.index("end_time") + 1
        ws.batch_update([
            {"range": gspread.utils.rowcol_to_a1(cell.row, status_col), "values": [["Completed"]]},
            {"range": gspread.utils.rowcol_to_a1(cell.row, end_col), "values": [[end_time]]},
        ])

    # Champion of Champions = last loss
    champion = None
    if g["all_losses"]:
        champion = g["all_losses"][-1]["player"]
        append_row_gspread(CHAMPIONS_WS, {
            "match_id": g["match_id"],
            "player_name": champion,
            "title": "Champion of Champions",
        })

    # Build summary
    losses_per = {}
    for l in g["all_losses"]:
        losses_per[l["player"]] = losses_per.get(l["player"], 0) + 1
    crowns_per = {}
    for c in g["crowns"]:
        crowns_per[c["loser"]] = crowns_per.get(c["loser"], 0) + 1
    top_crown = max(crowns_per, key=crowns_per.get) if crowns_per else None
    summary = {
        "losses_per_player": losses_per,
        "crowns_per_player": crowns_per,
        "top_crown_player": top_crown,
        "champion": champion,
        "total_losses": len(g["all_losses"]),
        "total_rounds": len(g["crowns"]),
        "participants": g["participants"],
    }
    date_str = g["start_time"].split(" ")[0] if g["start_time"] else datetime.now().strftime("%Y-%m-%d")
    append_row_gspread(SUMMARY_WS, {
        "match_id": g["match_id"],
        "date": date_str,
        "summary_json": json.dumps(summary, ensure_ascii=False),
    })

    # Clear game state
    st.session_state.game = None
    st.session_state.analytics_summaries = None  # force reload next time
    return champion


# ============================================================
# ANALYTICS LOADING — reads only match_summary
# ============================================================
def load_analytics_summaries() -> pd.DataFrame:
    """Load match_summary into a DataFrame with parsed JSON column."""
    if st.session_state.analytics_summaries is not None:
        return st.session_state.analytics_summaries
    df = read_ws(SUMMARY_WS)
    if df.empty:
        st.session_state.analytics_summaries = pd.DataFrame()
        return st.session_state.analytics_summaries
    df = df.copy()
    df["summary"] = df["summary_json"].apply(
        lambda s: json.loads(s) if s and not pd.isna(s) else {}
    )
    st.session_state.analytics_summaries = df
    return df


def recompute_summaries_from_raw():
    """One-time migration: rebuild match_summary from raw losses/rounds/champions.
    Reads everything once and writes one summary row per completed match.
    Skips matches that already have a summary."""
    matches = read_ws(MATCHES_WS)
    rounds = read_ws(ROUNDS_WS)
    losses = read_ws(LOSSES_WS)
    champions = read_ws(CHAMPIONS_WS)
    existing_summary = read_ws(SUMMARY_WS)

    existing_ids = set(
        existing_summary["match_id"].astype(str).tolist()
        if not existing_summary.empty and "match_id" in existing_summary.columns
        else []
    )

    if matches.empty:
        return 0
    completed = matches[matches["status"] == "Completed"]
    added = 0
    for _, m in completed.iterrows():
        mid = str(m["match_id"])
        if mid in existing_ids:
            continue
        # Participants
        participants = []
        if not rounds.empty:
            setup = rounds[(rounds["match_id"] == mid) & (rounds["status"] == "Setup")]
            if not setup.empty:
                raw = setup.iloc[0].get("loser_name", "")
                if raw and not pd.isna(raw):
                    participants = [p.strip() for p in str(raw).split(",") if p.strip()]
        # Losses per player
        losses_per = {}
        if not losses.empty:
            ml = losses[losses["match_id"] == mid]
            if not ml.empty:
                losses_per = ml.groupby("player_name").size().to_dict()
                losses_per = {str(k): int(v) for k, v in losses_per.items()}
        # Crowns
        crowns_per = {}
        total_rounds_played = 0
        if not rounds.empty:
            mr = rounds[(rounds["match_id"] == mid) & (rounds["status"] == "Completed")]
            total_rounds_played = len(mr)
            if not mr.empty:
                mr2 = mr[mr["loser_name"].notna() & (mr["loser_name"] != "")]
                if not mr2.empty:
                    crowns_per = mr2.groupby("loser_name").size().to_dict()
                    crowns_per = {str(k): int(v) for k, v in crowns_per.items()}
        top_crown = max(crowns_per, key=crowns_per.get) if crowns_per else None
        # Champion
        champion = None
        if not champions.empty:
            cm = champions[champions["match_id"] == mid]
            if not cm.empty:
                champion = str(cm.iloc[0]["player_name"])
        summary = {
            "losses_per_player": losses_per,
            "crowns_per_player": crowns_per,
            "top_crown_player": top_crown,
            "champion": champion,
            "total_losses": int(sum(losses_per.values())),
            "total_rounds": int(total_rounds_played),
            "participants": participants,
        }
        append_row_gspread(SUMMARY_WS, {
            "match_id": mid,
            "date": str(m.get("date", "")),
            "summary_json": json.dumps(summary, ensure_ascii=False),
        })
        added += 1
    st.session_state.analytics_summaries = None
    return added


# ============================================================
# HEADER + AUTH BAR
# ============================================================
st.title("🃏 DURAK System")
st.caption("מערכת ניהול טורנירי דורק")

top_col1, top_col2, top_col3 = st.columns([2, 2, 2])

with top_col1:
    if st.button("🔄 רענן"):
        force_refresh_reads()
        st.rerun()

with top_col2:
    if is_keeper():
        st.markdown("<div class='keeper-badge'>✅ מחובר כרשם</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div class='viewer-badge'>👁️ מצב צפייה</div>", unsafe_allow_html=True)

with top_col3:
    if is_keeper():
        if st.button("🚪 התנתק"):
            st.session_state.is_scorekeeper = False
            st.session_state.game = None
            st.rerun()
    else:
        with st.popover("🔐 התחבר כרשם"):
            pw = st.text_input("סיסמה", type="password", key="pw_input", placeholder="סיסמת הרשם")
            if st.button("התחבר", key="login_btn"):
                if pw == SCOREKEEPER_PASSWORD:
                    st.session_state.is_scorekeeper = True
                    st.success("התחברת בהצלחה!")
                    st.rerun()
                else:
                    st.error("סיסמה שגויה")

# Save indicator (small, non-intrusive)
if st.session_state.last_save_msg and st.session_state.last_save_time:
    elapsed = (datetime.now() - st.session_state.last_save_time).total_seconds()
    if elapsed < 4:
        st.markdown(
            f"<div class='save-indicator'>✓ {st.session_state.last_save_msg}</div>",
            unsafe_allow_html=True,
        )

st.divider()

tab_mgmt, tab_live, tab_analytics = st.tabs(["⚙️ ניהול", "🎮 משחק פעיל", "📊 אנליזה"])


# ============================================================
# TAB 1 — MANAGEMENT
# ============================================================
with tab_mgmt:
    st.header("ניהול שחקנים וערבי משחק")

    if not is_keeper():
        st.info("🔒 מצב צפייה. רק רשם יכול לבצע פעולות.")

    # === Resume active match ===
    if is_keeper() and st.session_state.game is None:
        active_id = find_active_match_id()
        if active_id:
            st.warning(f"🌙 יש ערב משחק פעיל בגיליון: `{active_id}`")
            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("▶️ המשך ערב פעיל", type="primary"):
                    state = bootstrap_state_from_sheets(active_id)
                    if state:
                        st.session_state.game = state
                        mark_saved("ערב נטען")
                        st.rerun()
                    else:
                        st.error("לא ניתן לטעון את הערב.")
            with col_b:
                if st.button("🏁 סיים את הערב הפעיל (סגירה כפויה)", type="secondary"):
                    state = bootstrap_state_from_sheets(active_id)
                    if state:
                        st.session_state.game = state
                        champ = action_end_match()
                        if champ:
                            st.success(f"👑 אלוף האלופים: {champ}")
                        mark_saved("הערב נסגר")
                        st.rerun()

    # === Add player ===
    st.subheader("➕ הוספת שחקן")
    if is_keeper() and st.session_state.game is None:
        with st.form("add_player_form", clear_on_submit=True):
            new_player = st.text_input("שם שחקן חדש")
            submitted = st.form_submit_button("הוסף שחקן")
            if submitted:
                ok, msg = action_add_player(new_player or "")
                if ok:
                    mark_saved(msg)
                    st.rerun()
                else:
                    st.warning(msg)
    elif is_keeper() and st.session_state.game is not None:
        st.caption("(לא ניתן להוסיף שחקנים בזמן ערב משחק פעיל)")
    else:
        st.caption("(זמין לרשם בלבד)")

    # === Show players ===
    st.subheader("👥 שחקנים רשומים")
    if st.session_state.cached_players is None:
        df = read_ws(PLAYERS_WS)
        st.session_state.cached_players = (
            df["player_name"].dropna().astype(str).str.strip().tolist()
            if not df.empty and "player_name" in df.columns
            else []
        )
    all_players = st.session_state.cached_players
    if not all_players:
        st.info("עדיין אין שחקנים רשומים.")
    else:
        st.dataframe(
            pd.DataFrame({"שם שחקן": all_players}),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    # === Start new match ===
    st.subheader("🌙 ערב משחק")
    if st.session_state.game is None:
        if is_keeper():
            if all_players:
                participants = st.multiselect(
                    "בחר משתתפים לערב המשחק",
                    options=all_players,
                    key="participants_select",
                )
                if st.button("🚀 התחל ערב משחק חדש", type="primary"):
                    if len(participants) < 2:
                        st.error("יש לבחור לפחות 2 שחקנים.")
                    else:
                        try:
                            mid = action_start_match(participants)
                            mark_saved("ערב התחיל")
                            st.success("ערב משחק חדש החל! עבור ל'משחק פעיל'.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"שגיאה בהתחלת הערב: {e}")
            else:
                st.warning("הוסף שחקנים תחילה.")
        else:
            st.info("אין ערב משחק פעיל.")
    else:
        g = st.session_state.game
        st.success(f"ערב משחק פעיל: `{g['match_id']}` | התחיל ב-{g['start_time']}")
        st.write("**משתתפים:**", ", ".join(g["participants"]))
        st.write(f"**סיבובים שהושלמו:** {len(g['crowns'])}")

        if is_keeper():
            if st.button("🏁 סיים ערב משחק", type="primary"):
                if g["round_status"] == "Active":
                    st.error("יש לסיים את הסיבוב הפעיל לפני סיום הערב.")
                else:
                    try:
                        champ = action_end_match()
                        if champ:
                            st.success(f"👑 אלוף האלופים של הערב: **{champ}**")
                        else:
                            st.info("הערב נסגר ללא הפסדים רשומים.")
                        mark_saved("הערב נסגר")
                        st.rerun()
                    except Exception as e:
                        st.error(f"שגיאה בסיום הערב: {e}")


# ============================================================
# TAB 2 — LIVE GAME
# ============================================================
with tab_live:
    st.header("🎮 משחק פעיל")

    # Scorekeeper view — uses in-memory game state
    if is_keeper() and st.session_state.game is not None:
        g = st.session_state.game

        if g["round_status"] != "Active":
            next_num = max([c["round"] for c in g["crowns"]] + [0]) + 1
            st.info(f"מוכן לסיבוב #{next_num}")
            if st.button("🆕 התחל סיבוב חדש", type="primary"):
                try:
                    action_start_round()
                    mark_saved("הסיבוב התחיל")
                    st.rerun()
                except Exception as e:
                    st.error(f"שגיאה: {e}")
        else:
            st.subheader(f"סיבוב #{g['round_num']}")
            st.write("### לוח הפסדים")
            for p in g["participants"]:
                count = g["losses_in_round"].get(p, 0)
                col1, col2 = st.columns([2, 1])
                with col1:
                    css_class = "danger-player" if count >= 4 else "safe-player"
                    icon = "⚠️" if count >= 4 else "•"
                    st.markdown(
                        f"<div class='{css_class}'>{icon} {p} — {count}/5</div>",
                        unsafe_allow_html=True,
                    )
                with col2:
                    if st.button("➖ הפסד", key=f"loss_{p}_{g['round_num']}"):
                        try:
                            ok, msg = action_record_loss(p)
                            if ok:
                                mark_saved(msg if "🏆" in msg else "הפסד נרשם")
                                if "🏆" in msg:
                                    st.success(msg)
                            st.rerun()
                        except Exception as e:
                            st.error(f"שגיאה ברישום: {e}")

            st.divider()
            st.write("### סיום ידני של הסיבוב")
            counts = g["losses_in_round"]
            non_zero = {p: c for p, c in counts.items() if c > 0}
            if not non_zero:
                st.caption("עדיין לא נרשמו הפסדים בסיבוב.")
            else:
                max_count = max(non_zero.values())
                leaders = [p for p, c in non_zero.items() if c == max_count]
                if len(leaders) == 1:
                    if st.button(f"🛑 סיים סיבוב (מפסיד: {leaders[0]})"):
                        try:
                            action_end_round(leaders[0], "Manual")
                            mark_saved(f"סיבוב הסתיים — {leaders[0]}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"שגיאה: {e}")
                else:
                    st.warning(f"⚖️ תיקו ({max_count} הפסדים): {', '.join(leaders)}")
                    tie_choice = st.selectbox("בחר מפסיד:", options=leaders, key=f"tie_{g['round_num']}")
                    if st.button("🛑 סיים סיבוב ידנית"):
                        try:
                            action_end_round(tie_choice, "Manual")
                            mark_saved(f"סיבוב הסתיים — {tie_choice}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"שגיאה: {e}")

    # Viewer or scorekeeper without active game: read from sheet (one snapshot)
    else:
        active_id = find_active_match_id()
        if active_id is None:
            st.info("אין ערב משחק פעיל כעת.")
        else:
            snapshot = bootstrap_state_from_sheets(active_id)
            if snapshot is None:
                st.info("ערב פעיל אך לא ניתן לטעון מצב.")
            else:
                st.success(f"ערב משחק פעיל מ-{snapshot['start_time']}")
                st.write("**משתתפים:**", ", ".join(snapshot["participants"]))
                st.write(f"**סיבובים שהושלמו:** {len(snapshot['crowns'])}")
                if snapshot["round_status"] == "Active":
                    st.subheader(f"סיבוב #{snapshot['round_num']} — בתהליך")
                    for p in snapshot["participants"]:
                        count = snapshot["losses_in_round"].get(p, 0)
                        css = "danger-player" if count >= 4 else "safe-player"
                        icon = "⚠️" if count >= 4 else "•"
                        st.markdown(
                            f"<div class='{css}'>{icon} {p} — {count}/5</div>",
                            unsafe_allow_html=True,
                        )
                else:
                    st.info("ממתינים לסיבוב הבא...")
                if snapshot["crowns"]:
                    st.write("### כתרים עד כה")
                    for c in snapshot["crowns"]:
                        st.write(f"- סיבוב {c['round']}: 👑 {c['loser']}")


# ============================================================
# TAB 3 — ANALYTICS (lazy, reads only match_summary)
# ============================================================
with tab_analytics:
    st.header("📊 אנליזה")
    st.caption("נטען לפי דרישה. לחץ '🔄 רענן' מעלה לעדכון.")

    summaries_df = load_analytics_summaries()

    if is_keeper():
        with st.expander("⚙️ כלי תחזוקה"):
            st.caption("חשב מחדש סיכומים מהנתונים הגולמיים (לערבים שאין להם סיכום).")
            if st.button("🔧 חשב סיכומים חסרים"):
                with st.spinner("מחשב..."):
                    try:
                        added = recompute_summaries_from_raw()
                        st.success(f"נוספו {added} סיכומים.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"שגיאה: {e}")

    if summaries_df.empty:
        st.info("אין נתוני סיכום עדיין. סיים ערב משחק כדי לראות אנליזה.")
    else:
        # Aggregate across all summaries
        agg_losses = {}
        agg_crowns = {}
        champ_counts = {}
        nights_per_player = {}
        for _, row in summaries_df.iterrows():
            s = row["summary"]
            for p, c in (s.get("losses_per_player") or {}).items():
                agg_losses[p] = agg_losses.get(p, 0) + int(c)
            for p, c in (s.get("crowns_per_player") or {}).items():
                agg_crowns[p] = agg_crowns.get(p, 0) + int(c)
            champ = s.get("champion")
            if champ:
                champ_counts[champ] = champ_counts.get(champ, 0) + 1
            for p in (s.get("participants") or []):
                nights_per_player[p] = nights_per_player.get(p, 0) + 1

        # ── Section 1: Lifetime Summary Table ──────────────────────
        st.subheader("📋 טבלת סיכום לכל הזמנים")
        all_players_lifetime = sorted(
            set(agg_losses) | set(agg_crowns) | set(nights_per_player),
            key=lambda p: agg_losses.get(p, 0),
            reverse=True,
        )
        lifetime_rows = []
        for p in all_players_lifetime:
            lifetime_rows.append({
                "שחקן": p,
                "סה״כ הפסדים": agg_losses.get(p, 0),
                "סה״כ כתרים": agg_crowns.get(p, 0),
                "תארי אלוף האלופים": champ_counts.get(p, 0),
                "ערבים שהשתתף": nights_per_player.get(p, 0),
            })
        st.dataframe(
            pd.DataFrame(lifetime_rows),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()

        # ── Section 2: Per-Match-Night Bar Chart ───────────────────
        st.subheader("🌙 ניתוח ערב ספציפי")
        labels = (summaries_df["date"].astype(str) + " — " + summaries_df["match_id"].astype(str)).tolist()
        selected = st.selectbox("בחר ערב:", options=labels[::-1])
        idx = labels.index(selected)
        s = summaries_df.iloc[idx]["summary"]

        col1, col2, col3 = st.columns(3)
        col1.metric("סה״כ סיבובים", s.get("total_rounds", 0))
        col2.metric("סה״כ הפסדים", s.get("total_losses", 0))
        col3.metric("אלוף האלופים", s.get("champion") or "—")

        losses_map = s.get("losses_per_player") or {}
        crowns_map = s.get("crowns_per_player") or {}
        night_players = sorted(set(losses_map) | set(crowns_map))
        if night_players:
            fig_night = go.Figure(data=[
                go.Bar(
                    name="הפסדים",
                    x=night_players,
                    y=[losses_map.get(p, 0) for p in night_players],
                    marker_color="crimson",
                ),
                go.Bar(
                    name="כתרים",
                    x=night_players,
                    y=[crowns_map.get(p, 0) for p in night_players],
                    marker_color="gold",
                ),
            ])
            fig_night.update_layout(barmode="group", xaxis_title="שחקן", yaxis_title="כמות")
            st.plotly_chart(fig_night, use_container_width=True)

        st.divider()

        # ── Section 3: Two Pie Charts (Lifetime Distribution) ──────
        st.subheader("🥧 חלוקה לכל הזמנים")
        pie_col1, pie_col2 = st.columns(2)
        if agg_losses:
            with pie_col1:
                fig_losses_pie = go.Figure(data=[go.Pie(
                    labels=list(agg_losses.keys()),
                    values=list(agg_losses.values()),
                    hoverinfo="label+percent",
                    textinfo="label",
                )])
                fig_losses_pie.update_layout(title_text="חלוקת הפסדים (לכל הזמנים)")
                st.plotly_chart(fig_losses_pie, use_container_width=True)
        if agg_crowns:
            with pie_col2:
                fig_crowns_pie = go.Figure(data=[go.Pie(
                    labels=list(agg_crowns.keys()),
                    values=list(agg_crowns.values()),
                    hoverinfo="label+percent",
                    textinfo="label",
                )])
                fig_crowns_pie.update_layout(title_text="חלוקת כתרים (לכל הזמנים)")
                st.plotly_chart(fig_crowns_pie, use_container_width=True)
