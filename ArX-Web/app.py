"""
ArX V2 — Streamlit Web Dashboard
Scraper + Sender + Storage backend'ini kullanır.
Transfer işlemi arka plan thread'inde çalışır.
"""

import gc
import json
import os
import threading
import time
from collections import deque
from datetime import datetime

import streamlit as st

from scraper import Scraper
from sender import Sender
from storage import Storage

# ─── Sayfa Ayarları ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ArX Dashboard",
    page_icon="🟣",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .stApp { background-color: #0D0D12; color: white; }
    .block-container { padding-top: 1.5rem; }
    .stat-card {
        background: #16161F;
        border: 1px solid #8A2BE240;
        border-radius: 8px;
        padding: 12px 16px;
        margin: 4px 0;
    }
    .stat-val { font-size: 22px; font-weight: bold; color: #8A2BE2; }
    .stat-lbl { font-size: 11px; color: #888; }
    .log-box {
        background: #080810;
        border: 1px solid #222;
        border-radius: 6px;
        padding: 10px;
        height: 300px;
        overflow-y: auto;
        font-family: monospace;
        font-size: 11px;
    }
    .log-info    { color: #CCC; }
    .log-success { color: #2ECC71; }
    .log-warning { color: #FFA040; }
    .log-error   { color: #FF6B6B; }
    .arx-logo { font-size: 28px; font-weight: bold; }
    .arx-logo span { color: #8A2BE2; }
</style>
""", unsafe_allow_html=True)

# ─── Global State (Streamlit'te session_state kullanılır) ─────────────────────
DB_PATH = "/tmp/arx_data.db"   # Streamlit Cloud'da kalıcı /tmp

def _init_state():
    defaults = {
        "storage":      None,
        "scraper":      None,
        "sender":       None,
        "running":      False,
        "paused":       False,
        "logs":         deque(maxlen=300),
        "stats":        {"found": 0, "scanned": 0, "sent": 0, "active": "—"},
        "progress":     0.0,
        "transfer_thread": None,
        "mapping":      {},   # {src_id: tgt_id}
        "src_channels": [],
        "tgt_channels": [],
        "user_guilds":  [],
        "bot_guilds":   [],
        "token_status": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# Storage her zaman hazır olsun
if st.session_state.storage is None:
    st.session_state.storage = Storage(DB_PATH)

storage: Storage = st.session_state.storage

# ─── Yardımcı Fonksiyonlar ────────────────────────────────────────────────────

def add_log(msg: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.logs.appendleft({"ts": ts, "msg": msg, "level": level})

def _transfer_worker(mapping: dict, delay: float, media_limit,
                     inc_photos: bool, inc_gifs: bool, inc_videos: bool,
                     round_robin: bool):
    """Arka plan thread — transfer mantığı."""
    scraper: Scraper = st.session_state.scraper
    sender:  Sender  = st.session_state.sender
    pairs = list(mapping.items())
    total = len(pairs)

    try:
        if round_robin:
            _run_round_robin(pairs, delay, media_limit, inc_photos, inc_gifs, inc_videos)
            return

        saved_idx = int(storage.get_session_value("active_pair_index", "0"))
        if saved_idx > 0:
            add_log(f"⏩ {saved_idx}. kanaldan devam ediliyor...", "info")

        for idx, (src_id, tgt_id) in enumerate(pairs):
            if not st.session_state.running:
                break
            if idx < saved_idx:
                continue

            storage.save_session_value("active_pair_index", str(idx))
            st.session_state.stats["active"] = f"{src_id} → {tgt_id}"
            add_log(f"📂 İşleniyor: {src_id} → {tgt_id}", "info")

            gen = scraper.scrape_channel(
                channel_id=src_id,
                delay=delay,
                media_limit=media_limit,
                include_photos=inc_photos,
                include_gifs=inc_gifs,
                include_videos=inc_videos,
            )

            for msg_id, link in gen:
                while st.session_state.paused and st.session_state.running:
                    time.sleep(0.3)
                if not st.session_state.running:
                    break

                st.session_state.stats["found"] += 1

                if sender.send_media_link(tgt_id, link, delay):
                    st.session_state.stats["sent"] += 1
                    add_log(f"✅ ({st.session_state.stats['sent']}) {link[:60]}...", "success")
                else:
                    add_log(f"⚠️ Gönderilemedi: {link[:60]}...", "warning")

                pct = (idx + 1) / max(total, 1)
                st.session_state.progress = min(pct, 1.0)

            gc.collect()
            if st.session_state.running:
                storage.save_session_value("active_pair_index", str(idx + 1))
                add_log(f"✅ Kanal tamamlandı: {src_id}", "success")

        if st.session_state.running:
            storage.clear_session_value("active_pair_index")
            st.session_state.progress = 1.0
            add_log("🎉 Tüm kanallar tamamlandı!", "success")

    except Exception as e:
        add_log(f"❌ Transfer hatası: {e}", "error")
    finally:
        st.session_state.running = False


def _run_round_robin(pairs, delay, media_limit, inc_photos, inc_gifs, inc_videos):
    scraper: Scraper = st.session_state.scraper
    sender:  Sender  = st.session_state.sender
    generators = {}
    finished = set()

    for src_id, tgt_id in pairs:
        generators[src_id] = scraper.scrape_channel(
            channel_id=src_id, delay=delay, media_limit=media_limit,
            include_photos=inc_photos, include_gifs=inc_gifs, include_videos=inc_videos,
        )

    while st.session_state.running and len(finished) < len(pairs):
        made = False
        for src_id, tgt_id in pairs:
            if src_id in finished or not st.session_state.running:
                continue
            while st.session_state.paused and st.session_state.running:
                time.sleep(0.3)
            try:
                msg_id, link = next(generators[src_id])
                st.session_state.stats["found"] += 1
                st.session_state.stats["active"] = f"RR {src_id} → {tgt_id}"
                if sender.send_media_link(tgt_id, link, delay):
                    st.session_state.stats["sent"] += 1
                    add_log(f"✅ RR: {link[:55]}...", "success")
                made = True
            except StopIteration:
                finished.add(src_id)
                add_log(f"✅ Bitti: {src_id}", "success")
        if not made:
            break

    if st.session_state.running:
        add_log("🎉 Round-robin tamamlandı!", "success")
    st.session_state.running = False


def check_tokens(user_token: str, bot_token: str):
    st.session_state.scraper = Scraper(user_token, storage, add_log)
    st.session_state.sender  = Sender(bot_token,  storage, add_log)
    u_ok, u_msg = st.session_state.scraper.check_user_token()
    b_ok, b_msg = st.session_state.sender.check_bot_token()
    st.session_state.token_status = f"{u_msg}\n{b_msg}"
    if u_ok and b_ok:
        st.session_state.user_guilds = st.session_state.scraper.get_user_guilds()
        st.session_state.bot_guilds  = st.session_state.sender.get_bot_guilds()
        add_log("✅ Tokenler doğrulandı, sunucular yüklendi.", "success")
    else:
        add_log("❌ Token hatası.", "error")


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown('<div class="arx-logo">Ar<span>X</span></div>', unsafe_allow_html=True)
    st.caption("Developer areoxzx_")
    st.divider()

    # ── Tokenler ──────────────────────────────────────────────────────────────
    st.subheader("🔑 Tokenler")
    user_token = st.text_input("Kullanıcı Tokeni", type="password", key="inp_user_token")
    bot_token  = st.text_input("Bot Tokeni",       type="password", key="inp_bot_token")

    if st.button("✅ Tokenleri Kontrol Et", use_container_width=True):
        if user_token and bot_token:
            with st.spinner("Kontrol ediliyor..."):
                check_tokens(user_token, bot_token)
        else:
            st.warning("Her iki tokeni de gir.")

    if st.session_state.token_status:
        color = "green" if "✅" in st.session_state.token_status else "red"
        st.markdown(f":{color}[{st.session_state.token_status}]")

    st.divider()

    # ── Ayarlar ───────────────────────────────────────────────────────────────
    st.subheader("⚙️ Ayarlar")
    delay = st.select_slider("Gönderim Gecikmesi", options=[1, 2, 5, 10], value=1, format_func=lambda x: f"{x}s")
    limit_str = st.text_input("Medya Limiti (boş=sınırsız)", value="", key="inp_limit")
    media_limit = int(limit_str) if limit_str.strip().isdigit() else None

    st.caption("Medya Filtreleri")
    inc_photos  = st.checkbox("Fotoğraflar", value=True)
    inc_gifs    = st.checkbox("GIF'ler",     value=True)
    inc_videos  = st.checkbox("Videolar",    value=True)
    round_robin = st.checkbox("Tane Tane Gönder (Round-Robin)", value=False)

    st.divider()

    # ── Kontrol ───────────────────────────────────────────────────────────────
    st.subheader("🎮 Kontrol")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("▶ Başlat", use_container_width=True, type="primary",
                     disabled=st.session_state.running):
            if not st.session_state.scraper or not st.session_state.sender:
                st.error("Önce tokenleri kontrol et!")
            elif not st.session_state.mapping:
                mapping_db = storage.get_channel_mapping()
                if mapping_db:
                    st.session_state.mapping = mapping_db
                    add_log("📂 DB'den mapping yüklendi.", "info")
                else:
                    st.error("Kanal eşlemesi yok!")
                    st.stop()

            if st.session_state.mapping:
                st.session_state.running = True
                st.session_state.paused  = False
                st.session_state.stats   = {"found": 0, "scanned": 0, "sent": 0, "active": "—"}
                st.session_state.progress = 0.0
                st.session_state.scraper.set_running(True)
                st.session_state.sender.set_running(True)
                storage.save_channel_mapping(st.session_state.mapping)

                t = threading.Thread(
                    target=_transfer_worker,
                    args=(st.session_state.mapping, delay, media_limit,
                          inc_photos, inc_gifs, inc_videos, round_robin),
                    daemon=True
                )
                t.start()
                st.session_state.transfer_thread = t
                add_log("🚀 Transfer başladı!", "success")
                st.rerun()

    with col2:
        if st.button("■ Durdur", use_container_width=True,
                     disabled=not st.session_state.running):
            st.session_state.running = False
            if st.session_state.scraper:
                st.session_state.scraper.set_running(False)
            if st.session_state.sender:
                st.session_state.sender.set_running(False)
            add_log("⏹ Durduruldu.", "warning")
            st.rerun()

    pause_label = "▶ Devam" if st.session_state.paused else "⏸ Duraklat"
    if st.button(pause_label, use_container_width=True,
                 disabled=not st.session_state.running):
        st.session_state.paused = not st.session_state.paused
        add_log("⏸ Duraklatıldı." if st.session_state.paused else "▶ Devam.", "info")
        st.rerun()

    st.divider()

    # ── Config ────────────────────────────────────────────────────────────────
    st.subheader("💾 Config")
    col3, col4 = st.columns(2)
    with col3:
        if st.button("Kaydet", use_container_width=True):
            cfg = {
                "tokens":   {"user_token": user_token, "bot_token": bot_token},
                "settings": {"delay": delay, "media_limit": limit_str,
                             "include_photos": inc_photos, "include_gifs": inc_gifs,
                             "include_videos": inc_videos, "round_robin": round_robin},
                "mapping":  st.session_state.mapping,
            }
            st.download_button(
                "⬇ İndir",
                data=json.dumps(cfg, ensure_ascii=False, indent=2),
                file_name="arx_config.json",
                mime="application/json",
                use_container_width=True
            )

    with col4:
        uploaded = st.file_uploader("Yükle", type="json", label_visibility="collapsed")
        if uploaded:
            try:
                cfg = json.load(uploaded)
                t = cfg.get("tokens", {})
                s = cfg.get("settings", {})
                st.session_state.mapping = cfg.get("mapping", {})
                add_log(f"✅ Config yüklendi ({len(st.session_state.mapping)} eşleme)", "success")
                st.rerun()
            except Exception as e:
                st.error(f"Config hatası: {e}")

    if st.button("🗑 DB Sıfırla", use_container_width=True):
        storage.reset_database()
        st.session_state.stats = {"found": 0, "scanned": 0, "sent": 0, "active": "—"}
        st.session_state.mapping = {}
        add_log("🗑 Veritabanı sıfırlandı.", "warning")
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# ANA İÇERİK — İKİ SEKME
# ══════════════════════════════════════════════════════════════════════════════

tab_channels, tab_status = st.tabs(["📡 Kanal Eşleme", "📊 İlerleme & Log"])

# ──────────────────────────────────────────────────────────────────────────────
# SEKME 1: KANAL EŞLEMESİ
# ──────────────────────────────────────────────────────────────────────────────
with tab_channels:
    col_src, col_tgt = st.columns(2)

    # ── Sol: Kaynak Kanallar ───────────────────────────────────────────────────
    with col_src:
        st.markdown("### 📥 Kaynak Kanallar")

        user_guild_names = [g["name"] for g in st.session_state.user_guilds]
        src_guild = st.selectbox("Sunucu Seç (Kullanıcı)", ["— Seç —"] + user_guild_names, key="src_guild_sel")

        if src_guild != "— Seç —" and st.session_state.scraper:
            guild_id = next((g["id"] for g in st.session_state.user_guilds if g["name"] == src_guild), None)
            if guild_id:
                if st.button("🔄 Kanalları Yükle", key="load_src"):
                    with st.spinner("Yükleniyor..."):
                        channels = st.session_state.scraper.get_guild_channels(guild_id)
                        st.session_state.src_channels = sorted(
                            [c for c in channels if c.get("type") == 0],
                            key=lambda c: c.get("position", 0)
                        )
                        add_log(f"✅ {len(st.session_state.src_channels)} kaynak kanal yüklendi.", "success")
                        st.rerun()

        # Manuel ID
        with st.expander("➕ Manuel Kanal ID Ekle"):
            manual_src_id = st.text_input("Kanal ID", key="manual_src_id")
            if st.button("Ekle", key="add_src_manual"):
                if manual_src_id.strip() and st.session_state.scraper:
                    info = st.session_state.scraper.get_channel_info(manual_src_id.strip())
                    if info and "id" in info:
                        st.session_state.src_channels.append(info)
                        add_log(f"✅ Eklendi: #{info.get('name')}", "success")
                        st.rerun()
                    else:
                        st.error("Kanal bulunamadı.")

        # Kanal listesi + seçim
        if st.session_state.src_channels:
            st.caption(f"{len(st.session_state.src_channels)} kanal — seçmek için tıkla")

            src_search = st.text_input("🔍 Ara", key="src_search", placeholder="kanal adı...")
            filtered_src = [
                c for c in st.session_state.src_channels
                if src_search.lower() in c["name"].lower()
            ] if src_search else st.session_state.src_channels

            # Tümünü seç / kaldır
            col_sa, col_da = st.columns(2)
            selected_src_ids = set(st.session_state.mapping.keys())

            with col_sa:
                if st.button("✅ Tümünü Seç", use_container_width=True):
                    for c in filtered_src:
                        if c["id"] not in st.session_state.mapping:
                            st.session_state.mapping[c["id"]] = ""
                    st.rerun()
            with col_da:
                if st.button("❌ Tümünü Kaldır", use_container_width=True):
                    for c in filtered_src:
                        st.session_state.mapping.pop(c["id"], None)
                    st.rerun()

            # Kanal checkbox listesi
            for ch in filtered_src:
                is_sel   = ch["id"] in st.session_state.mapping
                is_mapped = bool(st.session_state.mapping.get(ch["id"]))
                icon     = "🟢" if is_mapped else ("🔵" if is_sel else "⚪")
                checked  = st.checkbox(
                    f"{icon} # {ch['name']}",
                    value=is_sel,
                    key=f"src_chk_{ch['id']}"
                )
                if checked and ch["id"] not in st.session_state.mapping:
                    st.session_state.mapping[ch["id"]] = ""
                elif not checked and ch["id"] in st.session_state.mapping:
                    del st.session_state.mapping[ch["id"]]

    # ── Sağ: Hedef Kanallar ───────────────────────────────────────────────────
    with col_tgt:
        st.markdown("### 📤 Hedef Kanallar")

        bot_guild_names = [g["name"] for g in st.session_state.bot_guilds]
        tgt_guild = st.selectbox("Sunucu Seç (Bot)", ["— Seç —"] + bot_guild_names, key="tgt_guild_sel")

        if tgt_guild != "— Seç —" and st.session_state.sender:
            guild_id = next((g["id"] for g in st.session_state.bot_guilds if g["name"] == tgt_guild), None)
            if guild_id:
                if st.button("🔄 Kanalları Yükle", key="load_tgt"):
                    with st.spinner("Yükleniyor..."):
                        channels = st.session_state.sender.get_guild_channels(guild_id)
                        st.session_state.tgt_channels = sorted(
                            [c for c in channels if c.get("type") == 0],
                            key=lambda c: c.get("position", 0)
                        )
                        add_log(f"✅ {len(st.session_state.tgt_channels)} hedef kanal yüklendi.", "success")
                        st.rerun()

        # Manuel sunucu ID
        with st.expander("➕ Manuel Sunucu ID Ekle"):
            manual_tgt_id = st.text_input("Sunucu ID", key="manual_tgt_id")
            if st.button("Ekle", key="add_tgt_manual"):
                if manual_tgt_id.strip() and st.session_state.sender:
                    channels = st.session_state.sender.get_guild_channels(manual_tgt_id.strip())
                    if channels:
                        st.session_state.tgt_channels = sorted(
                            [c for c in channels if c.get("type") == 0],
                            key=lambda c: c.get("position", 0)
                        )
                        add_log(f"✅ {len(st.session_state.tgt_channels)} hedef kanal yüklendi.", "success")
                        st.rerun()
                    else:
                        st.error("Sunucu bulunamadı veya bot davet edilmedi.")

        # Eşleme arayüzü
        if st.session_state.tgt_channels:
            tgt_search = st.text_input("🔍 Hedef Ara", key="tgt_search", placeholder="kanal adı...")

            tgt_opts = {f"#{c['name']}": c["id"] for c in st.session_state.tgt_channels}
            tgt_names_list = list(tgt_opts.keys())

            unmatched_src = [
                src_id for src_id, tgt_id in st.session_state.mapping.items()
                if not tgt_id
            ]

            if unmatched_src:
                st.info(f"⬇ {len(unmatched_src)} kaynak kanal hedef bekliyor. Bir hedef seçince otomatik ilerler.")

            # Her seçili kaynak için hedef seçimi
            st.caption("Kaynak → Hedef Eşleme")
            for src_id in list(st.session_state.mapping.keys()):
                # Kaynak adını bul
                src_name = next(
                    (c["name"] for c in st.session_state.src_channels if c["id"] == src_id),
                    src_id
                )
                # Mevcut hedef
                current_tgt_id = st.session_state.mapping.get(src_id, "")
                current_tgt_name = next(
                    (f"#{c['name']}" for c in st.session_state.tgt_channels if c["id"] == current_tgt_id),
                    "— Seç —"
                )

                # Arama filtresi
                filtered_tgts = [n for n in tgt_names_list if tgt_search.lower() in n.lower()] \
                    if tgt_search else tgt_names_list

                sel = st.selectbox(
                    f"# {src_name}",
                    options=["— Seç —"] + filtered_tgts,
                    index=(["— Seç —"] + filtered_tgts).index(current_tgt_name)
                          if current_tgt_name in ["— Seç —"] + filtered_tgts else 0,
                    key=f"tgt_sel_{src_id}"
                )
                if sel != "— Seç —":
                    st.session_state.mapping[src_id] = tgt_opts[sel]
                else:
                    st.session_state.mapping[src_id] = ""

        # Eşleme özeti
        mapped_count = sum(1 for v in st.session_state.mapping.values() if v)
        total_count  = len(st.session_state.mapping)
        if total_count > 0:
            st.progress(mapped_count / total_count,
                        text=f"{mapped_count}/{total_count} eşlendi")


# ──────────────────────────────────────────────────────────────────────────────
# SEKME 2: İLERLEME & LOG
# ──────────────────────────────────────────────────────────────────────────────
with tab_status:

    # İstatistik kartları
    s = st.session_state.stats
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f"""<div class="stat-card">
            <div class="stat-val">{s['found']}</div>
            <div class="stat-lbl">Bulunan Medya</div>
        </div>""", unsafe_allow_html=True)
    with c2:
        st.markdown(f"""<div class="stat-card">
            <div class="stat-val">{s['sent']}</div>
            <div class="stat-lbl">Gönderilen</div>
        </div>""", unsafe_allow_html=True)
    with c3:
        st.markdown(f"""<div class="stat-card">
            <div class="stat-val">{storage.get_sent_count()}</div>
            <div class="stat-lbl">Toplam Gönderilmiş</div>
        </div>""", unsafe_allow_html=True)
    with c4:
        status_text = "🟢 Çalışıyor" if st.session_state.running else \
                      ("⏸ Duraklatıldı" if st.session_state.paused else "⚫ Bekliyor")
        st.markdown(f"""<div class="stat-card">
            <div class="stat-val" style="font-size:16px">{status_text}</div>
            <div class="stat-lbl">Durum</div>
        </div>""", unsafe_allow_html=True)

    st.caption(f"Aktif: {s['active']}")
    st.progress(st.session_state.progress, text=f"{st.session_state.progress*100:.1f}%")

    st.divider()

    # Log
    st.markdown("#### 📋 Log")

    col_log1, col_log2 = st.columns([4, 1])
    with col_log2:
        if st.button("🔄 Yenile"):
            st.rerun()
        auto_refresh = st.checkbox("Oto-yenile (3s)", value=False)

    log_html = '<div class="log-box">'
    for entry in list(st.session_state.logs):
        css = f"log-{entry['level']}"
        msg = entry['msg'].replace("<", "&lt;").replace(">", "&gt;")
        log_html += f'<div class="{css}">[{entry["ts"]}] {msg}</div>'
    log_html += "</div>"

    st.markdown(log_html, unsafe_allow_html=True)

    # Otomatik yenileme
    if auto_refresh and st.session_state.running:
        time.sleep(3)
        st.rerun()
