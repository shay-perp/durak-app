# requirements.txt:
# streamlit
# streamlit-gsheets-connection
# pandas

import streamlit as st
import pandas as pd
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection
import uuid

# ============================================================
# APP CONFIG
# ============================================================
st.set_page_config(
    page_title="DURAK System",
    page_icon="🃏",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# RTL + mobile-friendly styling
st.markdown(
    """
    <style>
        html, body, [class*="css"]  {
            direction: rtl;
            text-align: right;
        }
        .stButton > button {
            width: 100%;
            height: 3em;
            font-size: 1.1em;
            font-weight: bold;
        }
        .stTabs [data-baseweb="tab-list"] {
            justify-content: center;
            gap: 8px;
        }
        .stTabs [data-baseweb="tab"] {
            font-size: 1.1em;
            font-weight: bold;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.4em;
        }
        .danger-player {
            background-color: #ffcccc;
            padding: 8px;
            border-radius: 8px;
            font-weight: bold;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# ============================================================
# DATA LAYER
# ============================================================
conn = st.connection("gsheets", type=GSheetsConnection)

PLAYERS_WS = "players"
MATCHES_WS = "match_nights"
ROUNDS_WS = "rounds"
LOSSES_WS = "losses"
CHAMPIONS_WS = "champions"


def read_ws(worksheet: str) -> pd.DataFrame:
    """Read a worksheet fresh (no cache) and return a clean DataFrame."""
    try:
        df = conn.read(worksheet=worksheet, ttl=0)
        if df is None:
            return pd.DataFrame()
        # Drop fully empty rows (gsheets often returns NaN rows)
        df = df.dropna(how="all")
        return df.reset_index(drop=True)
    except Exception as e:
        st.error(f"שגיאה בקריאת הגיליון {worksheet}: {e}")
        return pd.DataFrame()


def write_ws(worksheet: str, df: pd.DataFrame):
    """Overwrite worksheet with given dataframe."""
    conn.update(worksheet=worksheet, data=df)


def append_row(worksheet: str, row: dict, expected_columns: list):
    """Append a single row to a worksheet preserving column order."""
    df = read_ws(worksheet)
    # Ensure columns exist
    if df.empty:
        df = pd.DataFrame(columns=expected_columns)
    new_df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    # Reorder columns
    for c in expected_columns:
        if c not in new_df.columns:
            new_df[c] = None
    new_df = new_df[expected_columns]
    write_ws(worksheet, new_df)


# Expected columns per worksheet
COLS = {
    PLAYERS_WS: ["player_name"],
    MATCHES_WS: ["match_id", "date", "start_time", "end_time", "status"],
    ROUNDS_WS: ["round_id", "match_id", "round_number", "status", "loser_name", "end_type"],
    LOSSES_WS: ["loss_id", "match_id", "round_number", "player_name", "loss_timestamp", "loss_count_in_round"],
    CHAMPIONS_WS: ["match_id", "player_name", "title"],
}


# ============================================================
# DOMAIN HELPERS
# ============================================================
def get_active_match():
    matches = read_ws(MATCHES_WS)
    if matches.empty or "status" not in matches.columns:
        return None
    active = matches[matches["status"] == "Active"]
    if active.empty:
        return None
    return active.iloc[-1].to_dict()


def get_match_participants(match_id: str) -> list:
    """Participants are everyone who has at least one loss row OR were initially set up.
    We persist participants implicitly: when starting a match we create a 'setup' round
    with status='Setup' that lists losers as None — but to keep schema strict, we instead
    store participants by inserting an initial loss-count=0 placeholder row in losses?
    A simpler approach: read participants from a dedicated session state at start
    AND also persist via the first round's existence; but to keep DB-only truth, we
    store participants as comma-separated in 'rounds' via a synthetic 'Setup' row.
    """
    rounds_df = read_ws(ROUNDS_WS)
    if rounds_df.empty:
        return []
    setup = rounds_df[(rounds_df["match_id"] == match_id) & (rounds_df["status"] == "Setup")]
    if setup.empty:
        return []
    # participants stored in loser_name column as CSV for the setup row
    raw = setup.iloc[0].get("loser_name", "")
    if not raw or pd.isna(raw):
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def get_current_round(match_id: str):
    """Return the active round dict or None."""
    rounds_df = read_ws(ROUNDS_WS)
    if rounds_df.empty:
        return None
    active = rounds_df[
        (rounds_df["match_id"] == match_id) & (rounds_df["status"] == "Active")
    ]
    if active.empty:
        return None
    return active.iloc[-1].to_dict()


def get_next_round_number(match_id: str) -> int:
    rounds_df = read_ws(ROUNDS_WS)
    if rounds_df.empty:
        return 1
    sub = rounds_df[(rounds_df["match_id"] == match_id) & (rounds_df["status"] != "Setup")]
    if sub.empty:
        return 1
    try:
        return int(pd.to_numeric(sub["round_number"], errors="coerce").max()) + 1
    except Exception:
        return 1


def get_loss_counts_in_round(match_id: str, round_number: int) -> dict:
    """Return {player_name: loss_count} for the given round."""
    losses_df = read_ws(LOSSES_WS)
    if losses_df.empty:
        return {}
    sub = losses_df[
        (losses_df["match_id"] == match_id)
        & (pd.to_numeric(losses_df["round_number"], errors="coerce") == round_number)
    ]
    if sub.empty:
        return {}
    return sub.groupby("player_name").size().to_dict()


def end_round(match_id: str, round_number: int, loser_name: str, end_type: str):
    """Mark the active round as Completed with the given loser."""
    rounds_df = read_ws(ROUNDS_WS)
    mask = (
        (rounds_df["match_id"] == match_id)
        & (pd.to_numeric(rounds_df["round_number"], errors="coerce") == round_number)
        & (rounds_df["status"] == "Active")
    )
    if not mask.any():
        return
    idx = rounds_df.index[mask][0]
    rounds_df.at[idx, "status"] = "Completed"
    rounds_df.at[idx, "loser_name"] = loser_name
    rounds_df.at[idx, "end_type"] = end_type
    write_ws(ROUNDS_WS, rounds_df)


def crown_champion_of_champions(match_id: str):
    """Find the very last loss in this match and crown that player."""
    losses_df = read_ws(LOSSES_WS)
    if losses_df.empty:
        return None
    sub = losses_df[losses_df["match_id"] == match_id].copy()
    if sub.empty:
        return None
    # Sort by loss_timestamp
    sub["loss_timestamp"] = pd.to_datetime(sub["loss_timestamp"], errors="coerce")
    sub = sub.sort_values("loss_timestamp")
    champ = sub.iloc[-1]["player_name"]
    append_row(
        CHAMPIONS_WS,
        {"match_id": match_id, "player_name": champ, "title": "Champion of Champions"},
        COLS[CHAMPIONS_WS],
    )
    return champ


# ============================================================
# HEADER
# ============================================================
st.title("🃏 DURAK System")
st.caption("מערכת ניהול טורנירי דורק")

tab_mgmt, tab_live, tab_analytics = st.tabs(["⚙️ ניהול", "🎮 משחק פעיל", "📊 אנליזה"])


# ============================================================
# TAB 1 — MANAGEMENT
# ============================================================
with tab_mgmt:
    st.header("ניהול שחקנים וערבי משחק")

    # --- Add player ---
    st.subheader("➕ הוספת שחקן")
    with st.form("add_player_form", clear_on_submit=True):
        new_player = st.text_input("שם שחקן חדש")
        submitted = st.form_submit_button("הוסף שחקן")
        if submitted:
            name = (new_player or "").strip()
            if not name:
                st.warning("יש להזין שם תקין.")
            else:
                players_df = read_ws(PLAYERS_WS)
                existing = (
                    players_df["player_name"].astype(str).str.strip().str.lower().tolist()
                    if not players_df.empty and "player_name" in players_df.columns
                    else []
                )
                if name.lower() in existing:
                    st.error(f"השחקן '{name}' כבר קיים.")
                else:
                    append_row(PLAYERS_WS, {"player_name": name}, COLS[PLAYERS_WS])
                    st.success(f"השחקן '{name}' נוסף בהצלחה!")
                    st.rerun()

    # --- Registered players ---
    st.subheader("👥 שחקנים רשומים")
    players_df = read_ws(PLAYERS_WS)
    if players_df.empty or "player_name" not in players_df.columns:
        st.info("עדיין אין שחקנים רשומים.")
        all_players = []
    else:
        all_players = (
            players_df["player_name"].dropna().astype(str).str.strip().tolist()
        )
        all_players = [p for p in all_players if p]
        st.dataframe(
            pd.DataFrame({"שם שחקן": all_players}),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()

    # --- Start / End match night ---
    active_match = get_active_match()
    st.subheader("🌙 ערב משחק")

    if active_match is None:
        st.info("אין ערב משחק פעיל כעת.")
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
                    now = datetime.now()
                    match_id = f"M-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
                    append_row(
                        MATCHES_WS,
                        {
                            "match_id": match_id,
                            "date": now.strftime("%Y-%m-%d"),
                            "start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "end_time": "",
                            "status": "Active",
                        },
                        COLS[MATCHES_WS],
                    )
                    # Persist participants as a 'Setup' round (round_number=0)
                    append_row(
                        ROUNDS_WS,
                        {
                            "round_id": f"R-{match_id}-SETUP",
                            "match_id": match_id,
                            "round_number": 0,
                            "status": "Setup",
                            "loser_name": ",".join(participants),
                            "end_type": "",
                        },
                        COLS[ROUNDS_WS],
                    )
                    st.success("ערב משחק חדש החל!")
                    st.rerun()
        else:
            st.warning("יש להוסיף שחקנים לפני שמתחילים ערב משחק.")
    else:
        st.success(
            f"ערב משחק פעיל: {active_match['match_id']} | התחיל ב-{active_match['start_time']}"
        )
        participants = get_match_participants(active_match["match_id"])
        st.write("**משתתפים:**", ", ".join(participants) if participants else "—")

        if st.button("🏁 סיים ערב משחק", type="primary"):
            # Make sure no active round is left open
            current_round = get_current_round(active_match["match_id"])
            if current_round is not None:
                st.error("יש לסיים את הסיבוב הפעיל לפני סיום הערב.")
            else:
                matches_df = read_ws(MATCHES_WS)
                mask = matches_df["match_id"] == active_match["match_id"]
                idx = matches_df.index[mask][0]
                matches_df.at[idx, "status"] = "Completed"
                matches_df.at[idx, "end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                write_ws(MATCHES_WS, matches_df)

                champ = crown_champion_of_champions(active_match["match_id"])
                if champ:
                    st.success(f"👑 אלוף האלופים של הערב: **{champ}**")
                else:
                    st.info("ערב המשחק נסגר ללא הפסדים רשומים.")
                st.rerun()


# ============================================================
# TAB 2 — LIVE GAME
# ============================================================
with tab_live:
    st.header("🎮 משחק פעיל")

    active_match = get_active_match()
    if active_match is None:
        st.info("אין ערב משחק פעיל. עבור ללשונית הניהול כדי להתחיל.")
    else:
        participants = get_match_participants(active_match["match_id"])
        if not participants:
            st.warning("לא נמצאו משתתפים לערב המשחק.")
        else:
            current_round = get_current_round(active_match["match_id"])

            if current_round is None:
                next_num = get_next_round_number(active_match["match_id"])
                st.info(f"הסיבוב הקודם הסתיים. מוכן להתחיל סיבוב #{next_num}.")
                if st.button("🆕 התחל סיבוב חדש", type="primary"):
                    rid = f"R-{active_match['match_id']}-{next_num}"
                    append_row(
                        ROUNDS_WS,
                        {
                            "round_id": rid,
                            "match_id": active_match["match_id"],
                            "round_number": next_num,
                            "status": "Active",
                            "loser_name": "",
                            "end_type": "",
                        },
                        COLS[ROUNDS_WS],
                    )
                    st.rerun()
            else:
                round_num = int(current_round["round_number"])
                st.subheader(f"סיבוב #{round_num}")

                loss_counts = get_loss_counts_in_round(
                    active_match["match_id"], round_num
                )

                # Display players + log-loss buttons
                st.write("### לוח הפסדים בסיבוב")
                for p in participants:
                    count = int(loss_counts.get(p, 0))
                    col1, col2 = st.columns([2, 1])
                    with col1:
                        if count >= 4:
                            st.markdown(
                                f"<div class='danger-player'>⚠️ {p} — {count}/5 הפסדים</div>",
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(f"**{p}** — {count}/5 הפסדים")
                    with col2:
                        if st.button(f"➖ רשום הפסד", key=f"loss_{p}_{round_num}"):
                            new_count = count + 1
                            loss_id = (
                                f"L-{active_match['match_id']}-R{round_num}-{uuid.uuid4().hex[:6]}"
                            )
                            append_row(
                                LOSSES_WS,
                                {
                                    "loss_id": loss_id,
                                    "match_id": active_match["match_id"],
                                    "round_number": round_num,
                                    "player_name": p,
                                    "loss_timestamp": datetime.now().strftime(
                                        "%Y-%m-%d %H:%M:%S"
                                    ),
                                    "loss_count_in_round": new_count,
                                },
                                COLS[LOSSES_WS],
                            )
                            # Auto-end if reached 5
                            if new_count >= 5:
                                end_round(
                                    active_match["match_id"],
                                    round_num,
                                    p,
                                    "Automatic",
                                )
                                st.success(f"🏆 {p} סיים את הסיבוב עם 5 הפסדים!")
                            st.rerun()

                st.divider()

                # Manual end button
                st.write("### סיום ידני של הסיבוב")
                if not loss_counts:
                    st.caption("עדיין לא נרשמו הפסדים בסיבוב זה.")
                else:
                    max_count = max(loss_counts.values())
                    leaders = [p for p, c in loss_counts.items() if c == max_count]

                    if len(leaders) == 1:
                        if st.button(
                            f"🛑 סיים סיבוב ידנית (מפסיד: {leaders[0]})",
                            type="secondary",
                        ):
                            end_round(
                                active_match["match_id"],
                                round_num,
                                leaders[0],
                                "Manual",
                            )
                            st.success(f"הסיבוב הסתיים. המפסיד: {leaders[0]}")
                            st.rerun()
                    else:
                        st.warning(
                            f"⚖️ תיקו עם {max_count} הפסדים בין: {', '.join(leaders)}"
                        )
                        tie_choice = st.selectbox(
                            "בחר את מפסיד הסיבוב:",
                            options=leaders,
                            key=f"tie_{round_num}",
                        )
                        if st.button("🛑 סיים סיבוב ידנית", type="secondary"):
                            end_round(
                                active_match["match_id"],
                                round_num,
                                tie_choice,
                                "Manual",
                            )
                            st.success(f"הסיבוב הסתיים. המפסיד: {tie_choice}")
                            st.rerun()


# ============================================================
# TAB 3 — ANALYTICS
# ============================================================
with tab_analytics:
    st.header("📊 אנליזה")

    matches_df = read_ws(MATCHES_WS)
    rounds_df = read_ws(ROUNDS_WS)
    losses_df = read_ws(LOSSES_WS)
    champions_df = read_ws(CHAMPIONS_WS)

    # --- Nightly Analytics ---
    st.subheader("🌙 אנליזה לפי ערב משחק")
    if matches_df.empty:
        st.info("עדיין אין נתונים זמינים.")
    else:
        # Build label per match
        matches_df_disp = matches_df.copy()
        matches_df_disp["label"] = (
            matches_df_disp["date"].astype(str)
            + " — "
            + matches_df_disp["match_id"].astype(str)
            + " ("
            + matches_df_disp["status"].astype(str)
            + ")"
        )
        selected_label = st.selectbox(
            "בחר ערב משחק:",
            options=matches_df_disp["label"].tolist()[::-1],
        )
        selected_match_id = matches_df_disp[
            matches_df_disp["label"] == selected_label
        ]["match_id"].iloc[0]

        # Rounds for that night (exclude Setup row)
        night_rounds = (
            rounds_df[
                (rounds_df["match_id"] == selected_match_id)
                & (rounds_df["status"] != "Setup")
            ]
            if not rounds_df.empty
            else pd.DataFrame()
        )
        night_losses = (
            losses_df[losses_df["match_id"] == selected_match_id]
            if not losses_df.empty
            else pd.DataFrame()
        )

        total_rounds = len(night_rounds)

        col1, col2 = st.columns(2)
        col1.metric("סה״כ סיבובים", total_rounds)
        col2.metric("סה״כ הפסדים", len(night_losses))

        # Losses per player
        if not night_losses.empty:
            losses_per_player = (
                night_losses.groupby("player_name").size().sort_values(ascending=False)
            )
            st.write("**הפסדים לפי שחקן (הערב):**")
            st.bar_chart(losses_per_player)

        # Crowns per player (round losers)
        if not night_rounds.empty:
            crowns = night_rounds[night_rounds["loser_name"].notna()]
            crowns = crowns[crowns["loser_name"] != ""]
            if not crowns.empty:
                crowns_per_player = (
                    crowns.groupby("loser_name").size().sort_values(ascending=False)
                )
                st.write("**כתרים לפי שחקן (הערב):**")
                st.bar_chart(crowns_per_player)
                top_crown = crowns_per_player.idxmax()
                st.success(
                    f"👑 הכי הרבה כתרים הערב: **{top_crown}** ({int(crowns_per_player.max())} כתרים)"
                )

        # Champion of Champions for this night
        if not champions_df.empty:
            night_champ = champions_df[champions_df["match_id"] == selected_match_id]
            if not night_champ.empty:
                champ_name = night_champ.iloc[0]["player_name"]
                st.info(f"🏆 אלוף האלופים של הערב: **{champ_name}**")

    st.divider()

    # --- Historical Analytics ---
    st.subheader("📈 אנליזה היסטורית")

    if losses_df.empty and (rounds_df is None or rounds_df.empty):
        st.info("עדיין אין נתונים היסטוריים זמינים.")
    else:
        # Lifetime losses
        if not losses_df.empty:
            life_losses = (
                losses_df.groupby("player_name").size().sort_values(ascending=False)
            )
            st.write("### 🥀 הפסדים מצטברים (לכל הזמנים)")
            st.bar_chart(life_losses)

        # Lifetime crowns
        if not rounds_df.empty:
            real_rounds = rounds_df[rounds_df["status"] == "Completed"]
            if not real_rounds.empty:
                real_rounds = real_rounds[real_rounds["loser_name"].notna()]
                real_rounds = real_rounds[real_rounds["loser_name"] != ""]
                if not real_rounds.empty:
                    life_crowns = (
                        real_rounds.groupby("loser_name")
                        .size()
                        .sort_values(ascending=False)
                    )
                    st.write("### 👑 כתרים מצטברים (לכל הזמנים)")
                    st.bar_chart(life_crowns)

        # Champion of Champions titles
        if not champions_df.empty:
            champ_counts = (
                champions_df.groupby("player_name").size().sort_values(ascending=False)
            )
            st.write("### 🏆 תארי 'אלוף האלופים'")
            st.bar_chart(champ_counts)

        # Averages per night
        if not matches_df.empty:
            completed_matches = matches_df[matches_df["status"] == "Completed"]
            num_nights = len(completed_matches)
            if num_nights > 0 and not losses_df.empty:
                # Only count losses from completed nights
                completed_ids = completed_matches["match_id"].tolist()
                comp_losses = losses_df[losses_df["match_id"].isin(completed_ids)]
                if not comp_losses.empty:
                    avg_losses = (
                        comp_losses.groupby("player_name").size() / num_nights
                    ).sort_values(ascending=False)
                    st.write("### 📉 ממוצע הפסדים לערב לפי שחקן")
                    st.dataframe(
                        avg_losses.round(2).reset_index().rename(
                            columns={"player_name": "שחקן", 0: "ממוצע הפסדים לערב"}
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

                if not rounds_df.empty:
                    comp_rounds = rounds_df[
                        (rounds_df["match_id"].isin(completed_ids))
                        & (rounds_df["status"] == "Completed")
                    ]
                    comp_rounds = comp_rounds[
                        comp_rounds["loser_name"].notna()
                        & (comp_rounds["loser_name"] != "")
                    ]
                    if not comp_rounds.empty:
                        avg_crowns = (
                            comp_rounds.groupby("loser_name").size() / num_nights
                        ).sort_values(ascending=False)
                        st.write("### 📈 ממוצע כתרים לערב לפי שחקן")
                        st.dataframe(
                            avg_crowns.round(2).reset_index().rename(
                                columns={"loser_name": "שחקן", 0: "ממוצע כתרים לערב"}
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )
